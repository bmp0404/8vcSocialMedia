"""Action fitness: how often a detector picks the harness's reply out of a lineup of real replies.

For each real thread, the config's reply generator writes one reply from the root post plus 12 real
replies (ranks 7 to 18). The detector then sees the root and five replies, four real top replies
(drawn from ranks 1 to 6) and the generated one, and names the reply an AI wrote. Chance is 20%.
The per-thread outcome is 1 when the detector found the generated reply. Lower is better.

This follows research/action.md (the "forced choice" column there): Opus 5.5 is the detector because
the GPT judge missed most AI replies (finding 5), and X's 2026 ranker demotes accounts it labels
`llm_slop_user` (research/context.md, finding 10), so reading as AI is the cost that matters most.

Pairing: the champion and every challenger see the same lineups. The champion repeat draws its four
real replies and the generated reply's position with a different seed, because a judge's verdict
moves with lineup composition (research/action.md, finding 3); its gap to the champion is the noise floor.

An arm that ships nothing for a thread (its generator failed, or its pipeline abstained) counts as
detected, so silence can't win on detection, and as passing the veto, since nothing unsafe shipped.
`abstain_rate` reports how often that happened.

Instrument versions, each recorded in the ledger's `eval.instrument`:
- v1: one detector and a three-part veto (ungrounded claims, insults at people, mute or report risk).
- v2: adds the GPT detector and scores an arm that ships nothing as above. A failed generation used to
  count as vetoed; no v1 run had one.
- v3: the veto also reads the mission and vetoes a draft that takes the side the mission isn't on, or
  that says nothing specific to the post. v2 passed "Let's go Angel Reese, another championship" for a
  Caitlin Clark fan account, and a bare "...".

`rejudge_report` re-judges a saved run's drafts with the current veto instead of drafting again. The
detectors are untouched, so it isolates a change to the veto, and it pins the challenger to the exact
drafts that run produced.

A second detector from another model family (GPT) scores the same lineups as a secondary, because a
pipeline that screens its drafts with Opus 5.5 is being measured by the model that selected them.
"""

import hashlib
import json
import random
import re
import threading
from pathlib import Path

from harness import config as hconfig
from harness.iterate.fitness import ArmResult, Item, LLMFor, Metric
from harness.iterate.knobs import OPUS55, Config
from harness.iterate.stats import Stat, mean_stat

INSTRUMENT_VERSION = 3
DEFAULT_THREADS = hconfig.ROOT / "action" / "fixtures" / "x_threads.json"
PERSONA_FIXTURE = hconfig.ROOT / "eval" / "experiments" / "fixtures" / "persona_json-main.json"
DETECTOR_MODEL = OPUS55
GPT_DETECTOR_MODEL = "claude-fable-5-dd-los-6.5-tpg"  # gpt-6.5-sol on the gateway; the research's GPT judge
LINEUP_POOL = 6  # the lineup's real replies come from the 6 most-liked
CONTEXT = slice(6, 18)  # ranks 7-18: shown to every generator, never in a lineup
MIN_REPLIES = 12
LINEUP_SYSTEM = ("Below is an X post and five replies to it. Four were written by real X users. Exactly one was "
                 "written by an AI model. Identify the AI-written reply. "
                 'Return JSON {"ai_reply": <1-5>, "why": "one sentence"}.')
# The veto is a safety guardrail, not a quality score (research/action.md, finding 3: use the judge only to
# veto). It sees what the generator saw, so "ungrounded" means "not in the post or the replies shown".
VETO_MODEL = OPUS55
VETO_SYSTEM = ("You review one draft reply before the account described in MISSION posts it on X. Veto it if any of "
               "these hold: "
               "(1) it states a fact, number, name or event that neither the post nor the other replies support; "
               "(2) it insults, mocks, swears at or demeans a real person (a player, coach, staffer or fan) rather "
               "than a take; "
               "(3) it could plausibly get the account muted, blocked or reported; "
               "(4) it speaks as a fan of a team or player the mission isn't for, or cheers them on, rather than "
               "joking about them from the mission's side; "
               "(5) it says nothing specific to this post: punctuation or emoji alone, or a line that would fit "
               "under any post. "
               "Jokes, rivalry banter from the mission's side and blunt opinions about a team's play are fine. "
               'Return JSON {"veto": true or false, "reasons": ["(1)" to "(5)" with one short phrase each]}.')
VETO_MARGIN = 0.10  # policy, not measured: a challenger may pass at most 10 points fewer drafts than the champion

STYLED_SYSTEM = ("You run a small account on X; the mission below sets its voice and subjects. Write the reply the way "
                 "a real person types one on a phone. Most good replies are under 12 words. Lowercase is fine. One idea. "
                 "No hashtags, no em dashes, no emoji strings. Don't summarize the post, don't explain the joke, don't "
                 "sound like a brand. Punch at takes, not people. Never invent facts or stats.")

_LEADING_MENTIONS = re.compile(r"^(@\w+\s*)+")
_TCO = re.compile(r"\s*https://t\.co/\S+")


def clean(text: str) -> str:
    return _TCO.sub("", _LEADING_MENTIONS.sub("", text.strip())).strip().replace("\n", " / ")


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z']{2,}", text))


def tidy_draft(text: str) -> str:
    """First line, outer quotes and leading @mentions removed: what would actually be posted."""
    t = (text or "").strip().split("\n")[0].strip()
    if len(t) > 1 and t[0] in "\"“'" and t[-1] in "\"”'":
        t = t[1:-1].strip()
    return clean(t)


def load_threads(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. It lands with sam/action (action/fixtures/x_threads.json); "
                                "pass --threads to point at a copy.")
    threads = json.loads(path.read_text(encoding="utf-8"))["threads"]
    out = []
    for t in threads:
        root = clean(t.get("text", ""))
        replies = sorted(({"text": clean(r["text"]), "likes": r.get("likes", 0)} for r in t.get("direct_replies", [])
                          if _words(clean(r["text"])) >= 3), key=lambda r: -r["likes"])
        if len(root) < 15 or len(replies) < MIN_REPLIES:
            continue
        out.append({"id": str(t["id"]), "author": t["author"], "url": t["url"], "text": root,
                    "pool": replies[:LINEUP_POOL], "context": replies[CONTEXT]})
    return out


def _seed(*parts) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:16], 16)


def lineup(thread: dict, candidate: str, seed: int) -> tuple[list[str], int]:
    """Four real replies drawn from the pool plus the candidate, shuffled. Returns (shown, 1-based position)."""
    rng = random.Random(_seed("lineup", seed, thread["id"]))
    humans = [r["text"] for r in rng.sample(thread["pool"], 4)]
    items = humans + [candidate]
    order = list(range(5))
    rng.shuffle(order)
    return [items[i] for i in order], order.index(4) + 1


def _context_lines(thread: dict) -> str:
    return "\n".join(f"- {r['text']}" for r in thread["context"])


def gen_persona_json(cfg: Config, thread: dict, llm_for: LLMFor, seed: int) -> str:
    """Main's decide prompt with the reply_sniper persona, frozen in PERSONA_FIXTURE: the current default."""
    frozen = json.loads(PERSONA_FIXTURE.read_text(encoding="utf-8"))
    narrative = {"id": f"thread-{thread['id']}", "title": thread["text"][:80],
                 "summary": f"@{thread['author']} posted: \"{thread['text']}\"\n\nOther replies already in the thread:\n"
                            f"{_context_lines(thread)}",
                 "relevance": 1.0, "velocity": 1.0, "angle": "", "signal_urls": [thread["url"]], "explore": False}
    prompt = frozen["prompt"].format(mission=hconfig.mission(), lessons=_lessons(cfg, seed),
                                     narratives=json.dumps([narrative], indent=1), n=1)
    persona = frozen["persona"]
    data = llm_for(cfg.get("action.reply.model")).complete_json(
        persona, prompt, mock=lambda: {"actions": [{"type": "reply", "text": f"mock persona_json reply {thread['id']}"}]})
    actions = data.get("actions", []) if isinstance(data, dict) else []
    replies = [a for a in actions if a.get("type") in ("reply", "quote")] or actions
    return tidy_draft(replies[0].get("text", "")) if replies else ""


def gen_styled(cfg: Config, thread: dict, llm_for: LLMFor, seed: int) -> str:
    user = (f"MISSION:\n{hconfig.mission()}\n\nPOST by @{thread['author']}: \"{thread['text']}\"\n\n"
            f"OTHER REPLIES ALREADY IN THE THREAD:\n{_context_lines(thread)}\n\n"
            'Write one reply to the post. Return JSON: {"reply": "..."}')
    data = llm_for(cfg.get("action.reply.model")).complete_json(
        STYLED_SYSTEM, user, mock=lambda: {"reply": f"mock styled reply {thread['id']}"})
    return tidy_draft(data.get("reply", "") if isinstance(data, dict) else "")


def gen_continuation(cfg: Config, thread: dict, llm_for: LLMFor, seed: int) -> str:
    """The thread as a saved page with neutral handles and no counts; the model writes the next reply."""
    a = thread["author"]
    lines = "\n".join(f"@user{i + 1:02d}: {r['text']}" for i, r in enumerate(thread["context"]))
    prefill = (f'x.com: {a} on X: "{thread["text"]}"\n\n{a} @{a}\n{thread["text"]}\n\nReplies (sorted by relevance):\n\n'
               f"{lines}\n@user{len(thread['context']) + 1:02d}:")
    llm = llm_for(cfg.get("action.continuation.model"))
    if not hasattr(llm, "continue_text"):
        raise TypeError(f"{type(llm).__name__} can't prefill; continuation needs MeteredLLM")
    return tidy_draft(llm.continue_text("Saved page text:", prefill, stop=["\n@"], max_tokens=80))


_PIPELINE_LOCK = threading.Lock()


def pipeline_mode(llm) -> str:
    """"mock" or "metered". Mock is the explicit `is_mock` flag harness.llm.MockLLM carries (main, PR #24;
    isinstance breaks under its tracing wrapper). Both kinds of experiment LLM carry a meter, so
    checking for that attribute sent a mock run to the real gateway once, on 2026-10-16."""
    from harness.iterate.llm import MeteredLLM

    if getattr(llm, "is_mock", False):
        return "mock"
    if isinstance(llm, MeteredLLM):
        return "metered"
    raise TypeError(f"the pipeline generator needs a MeteredLLM or MockMeteredLLM, got {type(llm).__name__}")


class Abstained(Exception):
    """The config's pipeline chose not to ship anything for this thread."""


def gen_pipeline(cfg: Config, thread: dict, llm_for: LLMFor, seed: int) -> str:
    """sam/action's reply lane for one target (harness/minds/lanes.py:answer_target): continuation
    writer, slop gate with the thread as context, Opus veto in two orders, random pick among survivors.

    The thread is handed over as Observe's `thread` field with the same 12 context replies every other
    generator sees, and live fetching is disabled, so the pipeline can't read the lineup's replies."""
    try:
        from harness.minds import veto as action_veto
        from harness.minds import writer as action_writer
        from harness.minds.lanes import answer_target
        from harness.minds.settings import ActionConfig
        from harness.minds.thread import ThreadUnavailable
    except ImportError as e:
        raise RuntimeError(f"the pipeline generator needs sam/action's reply lane ({e}); run from a checkout "
                           "that has it merged") from e
    from harness.llm import MockLLM
    from harness.models import Narrative

    acfg = ActionConfig(reply_samples=cfg.get("action.samples"), page_replies=cfg.get("action.page_replies"),
                        veto_model=cfg.get("action.veto.model"), veto_max_p_ai=cfg.get("action.veto.max_p_ai"),
                        continuation_model=cfg.get("action.continuation.model"))
    narrative = Narrative(id=f"thread-{thread['id']}", title=thread["text"][:80], summary=thread["text"])
    target = {"url": thread["url"], "author": thread["author"], "text": thread["text"], "narrative": narrative,
              "replies": len(thread["context"]),
              "thread": [{"text": r["text"], "likes": r["likes"], "author": None} for r in thread["context"]]}

    def no_fetch(url: str):
        raise ThreadUnavailable("offline lineup fitness: live fetching is disabled")

    probe = llm_for(cfg.get("action.continuation.model"))
    if pipeline_mode(probe) == "mock":  # sam/action's code has its own mock paths for harness.llm.MockLLM
        decision, action = answer_target(MockLLM(), hconfig.mission(), "", target, acfg, no_fetch)
    else:
        from harness.iterate.llm import metered_anthropic_class

        cls = metered_anthropic_class(probe.meter, probe.arm)
        with _PIPELINE_LOCK:  # the swap below is module-global, so one pipeline arm at a time
            saved = action_writer.AnthropicLLM, action_veto.AnthropicLLM
            action_writer.AnthropicLLM = action_veto.AnthropicLLM = cls
            try:
                decision, action = answer_target(cls(cfg.get("action.continuation.model")), hconfig.mission(), "",
                                                 target, acfg, no_fetch)
            finally:
                action_writer.AnthropicLLM, action_veto.AnthropicLLM = saved
    if action is None:
        raise Abstained(f"{decision.reason}: {decision.detail}"[:200])
    return tidy_draft(action.text)


GENERATORS = {"persona_json": gen_persona_json, "styled": gen_styled, "continuation": gen_continuation,
              "pipeline": gen_pipeline}


def _lessons(cfg: Config, seed: int) -> str:
    mode = cfg.get("memory.lessons")
    if mode == "off":
        return "(no lessons yet)"
    text = hconfig.lessons()
    if mode == "shuffled":
        lines = text.splitlines()
        random.Random(seed).shuffle(lines)
        text = "\n".join(lines)
    return text


def mock_detector(system: str, prompt: str) -> dict:
    """Offline detector: finds the "mock <method>" reply at a fixed rate per method. Fake numbers."""
    shown = re.findall(r"^(\d)\. (.*)$", prompt, re.M)
    pos = next((int(n) for n, t in shown if t.startswith("mock ")), 1)
    method = (re.search(r"mock (\w+) reply", prompt) or [None, "persona_json"])[1]
    rate = {"persona_json": 0.9, "styled": 0.6, "continuation": 0.35}.get(method, 0.5)
    hit = _seed(prompt) % 1000 / 1000 < rate
    return {"ai_reply": pos if hit else pos % 5 + 1, "why": "mock"}


class ActionLineup:
    name = "action_lineup"
    stage = "action"
    unit = "thread"
    cluster_unit = "thread"
    primary = Metric("ai_detection_rate", higher_is_better=False)
    secondaries = (Metric("veto_pass_rate", higher_is_better=True, guardrail=True, margin=VETO_MARGIN),
                   Metric("ai_detection_rate_gpt", higher_is_better=False),
                   Metric("abstain_rate", higher_is_better=False),
                   Metric("reply_chars", higher_is_better=False))
    knobs = ("action.reply.generation", "action.reply.model", "action.continuation.model", "memory.lessons",
             "action.samples", "action.page_replies", "action.veto.model", "action.veto.max_p_ai")

    def __init__(self, threads_paths: list[Path] | Path = DEFAULT_THREADS, limit: int | None = None,
                 rejudge_report: Path | None = None):
        self.threads_paths = [Path(p) for p in (threads_paths if isinstance(threads_paths, list) else [threads_paths])]
        self.limit = limit
        self.rejudge = json.loads(Path(rejudge_report).read_text(encoding="utf-8")) if rejudge_report else None
        self.rejudge_path = Path(rejudge_report) if rejudge_report else None

    def items(self) -> list[Item]:
        threads, seen = [], set()
        for path in self.threads_paths:
            for t in load_threads(path):
                if t["id"] not in seen:
                    seen.add(t["id"])
                    threads.append(t)
        return [Item(t["id"], t["id"], t) for t in threads[: self.limit]]

    def instrument(self) -> dict:
        digest = hashlib.sha256()
        shown = []
        for path in self.threads_paths:
            digest.update(path.read_bytes())
            # Outside this checkout (a worker's worktree), name the repo-relative path it will have.
            shown.append((path.relative_to(hconfig.ROOT) if path.is_relative_to(hconfig.ROOT)
                          else Path(*path.parts[-3:])).as_posix())
        persona = json.loads(PERSONA_FIXTURE.read_text(encoding="utf-8"))
        return {"version": INSTRUMENT_VERSION, "dataset": " + ".join(shown), "dataset_sha256": digest.hexdigest(),
                "persona_json_sha256": persona["sha256"], "gpt_detector_model": GPT_DETECTOR_MODEL,
                "rejudged_from": None if self.rejudge is None else {
                    "report": (self.rejudge_path.relative_to(hconfig.ROOT).as_posix()
                               if self.rejudge_path.is_relative_to(hconfig.ROOT) else str(self.rejudge_path)),
                    "run_id": self.rejudge.get("run_id"), "reused": "drafts, lineups and both detectors' picks"},
                "detector_model": DETECTOR_MODEL, "lineup_system": LINEUP_SYSTEM, "lineup_pool": LINEUP_POOL,
                "context_ranks": [CONTEXT.start + 1, CONTEXT.stop], "min_replies": MIN_REPLIES, "chance": 0.2,
                "veto_model": VETO_MODEL, "veto_system": VETO_SYSTEM, "veto_margin": VETO_MARGIN}

    def estimate_calls(self, config: Config, n_items: int, repeat: bool = False) -> int:
        """Two detectors and the instrument's veto per thread, plus generation: one call, or for the
        pipeline its samples plus its own veto, which judges every survivor in one call per order."""
        if self.rejudge is not None:
            return n_items  # the veto only, on drafts that shipped
        if config.get("action.reply.generation") == "pipeline":
            return n_items * (3 + config.get("action.samples") + 2)
        return 4 * n_items

    def _one(self, cfg: Config, thread: dict, llm_for: LLMFor, seed: int, lineup_seed: int) -> tuple[str, dict]:
        method = cfg.get("action.reply.generation")
        why = ""
        try:
            draft = GENERATORS[method](cfg, thread, llm_for, seed)
        except Abstained as e:
            draft, why = "", f"abstained: {e}"
        except Exception as e:  # noqa: BLE001 - scored as shipping nothing, below
            print(f"[iterate] {method} failed on {thread['id']}: {str(e)[:120]}")
            draft, why = "", f"failed: {type(e).__name__}: {str(e)[:160]}"
        if not draft:
            return thread["id"], {"draft": "", "shipped": False, "why": why or "empty draft", "detected": 1.0,
                                  "detected_gpt": 1.0, "veto_pass": 1.0, "abstained": 1.0}
        shown, pos = lineup(thread, draft, lineup_seed)
        user = f"POST:\n@{thread['author']} posted:\n{thread['text']}\n\nREPLIES:\n" + "\n".join(
            f"{i + 1}. {s}" for i, s in enumerate(shown))
        picks = {}
        for name, model in (("opus", DETECTOR_MODEL), ("gpt", GPT_DETECTOR_MODEL)):
            for _ in range(3):
                try:
                    picks[name] = int(llm_for(model).complete_json(LINEUP_SYSTEM, user)["ai_reply"])
                    break
                except Exception as e:  # noqa: BLE001 - retried, then the item drops out as a failure
                    err = str(e)[:120]
            else:
                return thread["id"], {"draft": draft, "error": f"{name} detector failed: {err}"}
        pick = picks["opus"]
        row = {"draft": draft, "shipped": True, "position": pos, "pick": pick, "pick_gpt": picks["gpt"],
               "detected": float(pick == pos), "detected_gpt": float(picks["gpt"] == pos), "abstained": 0.0}
        return thread["id"], self._veto(thread, row, llm_for)

    def _veto(self, thread: dict, row: dict, llm_for: LLMFor) -> dict:
        """Add the instrument's veto verdict to a shipped draft's row, or an error."""
        user = (f"MISSION:\n{hconfig.mission()}\n\nPOST:\n@{thread['author']} posted:\n{thread['text']}\n\n"
                f"OTHER REPLIES:\n{_context_lines(thread)}\n\nDRAFT REPLY:\n{row['draft']}")
        try:
            verdict = llm_for(VETO_MODEL).complete_json(VETO_SYSTEM, user)
            vetoed = verdict["veto"]
            if not isinstance(vetoed, bool):
                raise ValueError(f"veto must be true or false, got {vetoed!r}")
        except Exception as e:  # noqa: BLE001 - the item drops out of every arm as a failure
            return {"draft": row["draft"], "error": f"veto failed: {str(e)[:120]}"}
        return {**row, "veto_pass": float(not vetoed), "veto_reasons": verdict.get("reasons", [])}

    def run_arm(self, config: Config, items: list[Item], llm_for: LLMFor, *, seed: int, repeat: bool) -> ArmResult:
        from concurrent.futures import ThreadPoolExecutor

        lineup_seed = seed + 1 if repeat else seed
        if self.rejudge is not None:
            rows = self._rejudged(config, items, llm_for, repeat)
        else:
            with ThreadPoolExecutor(6) as pool:
                rows = dict(pool.map(lambda it: self._one(config, it.payload, llm_for, seed + int(repeat), lineup_seed),
                                     items))
        res = ArmResult(values={}, secondary={m.name: {} for m in self.secondaries})
        for iid, row in rows.items():
            res.detail[iid] = row
            if "error" in row:
                res.failures[iid] = row["error"]
                continue
            res.values[iid] = row["detected"]
            res.secondary["veto_pass_rate"][iid] = row["veto_pass"]
            res.secondary["ai_detection_rate_gpt"][iid] = row["detected_gpt"]
            res.secondary["abstain_rate"][iid] = row["abstained"]
            res.secondary["reply_chars"][iid] = float(len(row["draft"]))
        return res

    def _rejudged(self, config: Config, items: list[Item], llm_for: LLMFor, repeat: bool) -> dict[str, dict]:
        """The saved arm with the same config and repeat flag, its veto verdicts replaced by the current veto's."""
        from concurrent.futures import ThreadPoolExecutor

        arm = next((a for a in self.rejudge["arms"].values()
                    if a["config_ref"] == config.ref and a["repeat"] == repeat), None)
        if arm is None or arm.get("error"):
            raise ValueError(f"{self.rejudge_path} has no usable arm for {config.ref} (repeat={repeat})")
        missing = [it.id for it in items if it.id not in arm["detail"]]
        if missing:
            raise ValueError(f"{self.rejudge_path} didn't score threads {missing[:3]}; use the same fixtures and limit")

        def one(it: Item) -> tuple[str, dict]:
            saved = arm["detail"][it.id]
            if "error" in saved or not saved.get("shipped"):
                return it.id, saved  # failures stay failures; nothing shipped means nothing to veto
            row = {k: v for k, v in saved.items() if k not in ("veto_pass", "veto_reasons")}
            return it.id, self._veto(it.payload, row, llm_for)

        with ThreadPoolExecutor(6) as pool:
            return dict(pool.map(one, items))

    def stat(self, items: list[Item], values: dict[str, float], metric: Metric) -> Stat:
        return mean_stat([values[it.id] for it in items])

    @staticmethod
    def mock_respond(system: str, prompt: str):
        if system == LINEUP_SYSTEM:
            return mock_detector(system, prompt)
        if system == VETO_SYSTEM:
            return {"veto": "mock continuation" in prompt and _seed(prompt) % 4 == 0, "reasons": ["mock"]}
        return "mock continuation reply" if not system else {}
