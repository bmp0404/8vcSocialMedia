"""The two lanes of the decide stage. Every opportunity ends in one Decision: an action that ships,
an abstention with a reason code from `schema.ABSTAIN_REASONS`, or a question for a person.

Reply lane, the main lane for a small account (replies are where a 2-follower account gets seen):
  fresh reply targets only -> a planning call picks which to answer (it writes no text) ->
  the writer continues the real thread as a document -> schema -> slop gate with the thread as
  context -> Opus veto in two orders -> a random pick among the drafts that pass.
Post lane, in the research report's order: a person's own take, carried verbatim; else a short
caption or quote of a real source with credit. The persona chorus never ships text, only follows
and angles attached to the question for a person.

The measurements behind each step are in action/DESIGN.md.
"""

import json
import random
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from harness import config
from harness.llm import LLM, MockLLM
from harness.minds import corpus, gate, thread, veto, writer
from harness.minds.schema import POST_LANE, REPLY_LANE, ActionSchemaError, Offer, parse_call, parse_plan
from harness.minds.settings import ActionConfig, GateLimits
from harness.models import Action, Narrative

TAKES_PATH = config.ROOT / "action" / "takes.json"

PLAN_SYSTEM = """You run a small account on X. The mission below sets its subjects and tone. For each candidate post, decide whether the account should reply. Reply only where a person who follows these subjects would plausibly jump in. Abstain where any reply risks reading as cruel, opportunistic or off-mission (injury news needs care), with reason off_mission or mute_risk. In "intent", say in a few words what a good reply would do. You don't write the reply.

MISSION:
{mission}"""

# Decisions from every decide() call in this process, until the caller writes them with a run id.
PENDING: list["Decision"] = []
# The provider behind PENDING. Logs from mock runs don't count against the real budget.
PENDING_PROVIDER = {"name": ""}


@dataclass
class Decision:
    lane: str
    opportunity: str
    tool: str
    reason: str = ""
    detail: str = ""
    question: str = ""
    action_id: str | None = None
    candidates: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- budget


class Budget:
    """Per-run and per-day caps on recommended actions, plus spacing between posts."""

    def __init__(self, cfg: ActionConfig | None = None, used_today: Counter | None = None,
                 last_post_at: datetime | None = None, now: datetime | None = None):
        self.cfg = cfg or ActionConfig()
        self.used_today = Counter(used_today or {})
        self.used_run: Counter = Counter()
        self.posts_blocked = bool(last_post_at and (now or datetime.now()) - last_post_at
                                  < timedelta(minutes=self.cfg.post_spacing_minutes))

    def left(self, kind: str) -> int:
        if kind in ("post", "quote", "mention") and self.posts_blocked:
            return 0
        return max(0, min(self.cfg.run_caps.get(kind, 0) - self.used_run[kind],
                          self.cfg.daily_caps.get(kind, 0) - self.used_today[kind] - self.used_run[kind]))

    def take(self, kind: str) -> bool:
        if self.left(kind) <= 0:
            return False
        self.used_run[kind] += 1
        return True

    @classmethod
    def from_logs(cls, cfg: ActionConfig, out_dir: Path | None = None, now: datetime | None = None) -> "Budget":
        """Count today's recommended actions, and the latest post, from earlier decision logs."""
        out_dir, now = out_dir or config.OUT_DIR, now or datetime.now()
        used, last_post = Counter(), None
        for path in out_dir.glob("decisions-run*.json"):
            log = json.loads(path.read_text(encoding="utf-8"))
            at = datetime.fromisoformat(log.get("at", "1970-01-01T00:00:00"))
            if at.date() != now.date() or log.get("provider") == "mock":
                continue
            for d in log.get("decisions", []):
                if d.get("action_id"):
                    used[d["tool"]] += 1
                    if d["tool"] in ("post", "quote", "mention") and (last_post is None or at > last_post):
                        last_post = at
        return cls(cfg, used, last_post, now)


def shipped_takes(out_dir: Path | None = None) -> set[str]:
    """Human takes already recommended in an earlier run, so the same words don't go out twice."""
    out = set()
    for path in (out_dir or config.OUT_DIR).glob("decisions-run*.json"):
        for d in json.loads(path.read_text(encoding="utf-8")).get("decisions", []):
            if d.get("action_id") and d.get("detail") == "human take, verbatim":
                out.add(d.get("opportunity", ""))
    return out


# ---------------------------------------------------------------- reply lane


def reply_targets(narratives: list[Narrative]) -> list[dict]:
    """Every reply target across all narratives, not only the ones the chorus saw, fastest first.
    One per conversation root, because several replies under one root trip X's spam filters."""
    seen, out = set(), []
    for n in narratives:
        for rt in getattr(n, "reply_targets", None) or []:
            root = rt.get("conversation_id") or rt.get("url")
            if not rt.get("url") or root in seen:
                continue
            seen.add(root)
            out.append({**rt, "author": str(rt.get("author") or "").lstrip("@"), "narrative": n})
    return sorted(out, key=lambda t: (-(t.get("engagement_per_hour") or 0), t.get("age_minutes") or 0))


def facts_block(mission: str, narratives: list[Narrative]) -> str:
    return mission + "\n\nWHAT'S GOING ON:\n" + "\n".join(f"- {n.title}: {n.summary}" for n in narratives)


def target_meta(t: dict) -> dict:
    """The reply target as the UI wants it: everything Observe sent except the narrative object
    and the attached thread, which is kept as a count."""
    meta = {k: v for k, v in t.items() if k not in ("narrative", "thread")}
    meta["thread_attached"] = len(t.get("thread") or [])
    return meta


def plan_targets(llm: LLM, mission: str, lessons: str, targets: list[dict]) -> tuple[dict, str]:
    """One JSON call that picks which fresh targets to answer and with what intent. Returns
    ({url: plan}, error). On a malformed answer every target is skipped and the error is kept."""
    offer = Offer(target_urls=frozenset(t["url"] for t in targets))
    listing = "\n".join(f"- {t['url']} @{t['author']}: {t.get('text', '')}" for t in targets)
    user = (f"LESSONS FROM PAST ROUNDS:\n{lessons}\n\nCANDIDATE POSTS:\n{listing}\n\n"
            'Return JSON {"plans": [{"target_url": "...", "tool": "reply" or "abstain", "intent": "...", '
            '"reason": "off_mission|mute_risk|no_angle (abstain only)", "detail": "..."}]} with one entry per post.')
    try:
        out = llm.complete_json(PLAN_SYSTEM.format(mission=mission), user,
                                mock=lambda: {"plans": [{"target_url": t["url"], "tool": "reply", "intent": "mock"} for t in targets]})
        return parse_plan(out, offer), ""
    except (ActionSchemaError, ValueError) as e:
        return {}, f"planning call failed: {e}"


def reply_lane(llm: LLM, mission: str, lessons: str, narratives: list[Narrative], cfg: ActionConfig,
               budget: Budget, fetch=thread.fetch_replies, grounding=None) -> tuple[list[Action], list[Decision]]:
    targets = reply_targets(narratives)
    if not targets:
        return [], [Decision(REPLY_LANE, "", "abstain", "no_target", "no narrative carried reply_targets")]
    decisions, fresh = [], []
    for t in targets:
        age = t.get("age_minutes")
        if age is None or age > cfg.reply_max_age_minutes:
            shown = "unknown age" if age is None else f"{age:.0f} min old"
            decisions.append(Decision(REPLY_LANE, t["url"], "abstain", "stale_target",
                                      f"{shown}; cutoff {cfg.reply_max_age_minutes:.0f}"))
        else:
            fresh.append(t)
    if not fresh:
        return [], decisions
    plans, plan_error = plan_targets(llm, mission, lessons, fresh)
    facts = facts_block(mission, narratives)
    actions = []
    for t in fresh:
        plan = plans.get(t["url"])
        if plan is None:
            decisions.append(Decision(REPLY_LANE, t["url"], "abstain", "no_angle", plan_error or "the plan skipped this post"))
            continue
        if plan["tool"] == "abstain":
            decisions.append(Decision(REPLY_LANE, t["url"], "abstain", plan["reason"], plan.get("detail", "")))
            continue
        if budget.left("reply") <= 0:
            decisions.append(Decision(REPLY_LANE, t["url"], "abstain", "budget_exhausted", "reply cap reached"))
            continue
        d, a = answer_target(llm, mission, facts, t, cfg, fetch, plan.get("intent", ""), grounding)
        if a and budget.take("reply"):
            d.action_id = a.id
            actions.append(a)
        decisions.append(d)
    return actions, decisions


def _gate_candidates(texts: list[str], kind: str, context: str, limits: GateLimits, offer: Offer,
                     lane: str, target_url: str | None) -> tuple[list[dict], list[str]]:
    cands, passing = [], []
    for text in dict.fromkeys(t for t in texts if t):
        call = {"tool": kind, "text": text} | ({"target_url": target_url} if target_url else {})
        try:
            parse_call(call, lane, offer)
        except ActionSchemaError as e:
            cands.append({"text": text, "schema_error": str(e)})
            continue
        res = gate.check(text, kind, context=context, limits=limits)
        cands.append({"text": text, "gate": [asdict(f) for f in res.findings]})
        if res.passed:
            passing.append(text)
    return cands, passing


def _veto_and_pick(llm: LLM, cfg: ActionConfig, mission: str, cands: list[dict], passing: list[str], *,
                   lane: str, context: str, seed: str, grounding) -> tuple[veto.VetoResult | None, dict | None, str]:
    """Veto every gate-passing draft, then pick at random among survivors, checking grounding when installed."""
    results = veto.veto(llm, cfg, passing, lane=lane, context=context)
    by_text = {r.text: r for r in results}
    for c in cands:
        if c.get("text") in by_text:
            c["veto"] = by_text[c["text"]].evidence()
    ok = [r for r in results if r.passed]
    if not ok:
        whys = Counter(r.why for r in results)
        return None, None, "veto: " + "; ".join(f"{n}x {w}" for w, n in whys.most_common())
    random.Random(seed).shuffle(ok)  # the judge can't rank good from great, so it doesn't get to
    if grounding is None:
        return ok[0], None, ""
    for r in ok[:2]:
        try:
            score, reason = grounding.score(r.text, mission=mission, context=context, lane=lane,
                                            references=corpus.references())
        except Exception as e:  # noqa: BLE001 - a grounding failure vetoes this draft, not the run
            score, reason = -1, f"grounding check failed: {e}"
        g = {"score": score, "min": cfg.post_judge_min_grounding, "reason": reason}
        next(c for c in cands if c.get("text") == r.text)["grounding"] = g
        if score >= cfg.post_judge_min_grounding:
            return r, g, ""
    return None, None, "grounding veto on the first two survivors"


def judge_meta(cfg: ActionConfig, picked: veto.VetoResult, cands: list[dict], grounding: dict | None) -> dict:
    """Veto evidence for the UI: the picked draft's verdicts in both orders and the batch totals."""
    vetted = [c for c in cands if "veto" in c]
    return {"veto_model": cfg.veto_model, "max_p_ai": cfg.veto_max_p_ai, **picked.evidence(),
            "batch": {"drafts": len(cands), "gate_passed": len(vetted),
                      "veto_passed": sum(c["veto"]["passed"] for c in vetted)},
            "pick": "random among drafts that passed", "grounding": grounding}


def answer_target(llm: LLM, mission: str, facts: str, t: dict, cfg: ActionConfig, fetch, intent: str = "",
                  grounding=None) -> tuple[Decision, Action | None]:
    url, author, post = t["url"], t["author"], t.get("text", "")
    note = ""
    try:
        replies = thread.replies_for(t, (lambda _u: []) if isinstance(llm, MockLLM) else fetch)
    except thread.ThreadUnavailable as e:
        replies, note = [], str(e)
    page = [r["text"] for r in replies][: cfg.page_replies]
    dropped = []
    if cfg.filter_page_bots and page and not isinstance(llm, MockLLM):
        try:
            scores = veto.p_ai_scores(llm, cfg, page, context=f"POST by @{author}:\n{post}")
            dropped = [r for r, s in zip(page, scores) if s >= cfg.page_max_p_ai]
            page = [r for r, s in zip(page, scores) if s < cfg.page_max_p_ai]
        except veto.VetoError as e:
            note = f"page bot filter skipped: {e}"
    method = "continuation" if len(page) >= cfg.min_page_replies else "styled"
    texts = []
    if method == "continuation":
        try:
            texts = writer.continuations(llm, author, post, page, cfg, seed=url)
        except writer.WriterUnavailable as e:
            method, note = "styled", str(e)
    if method == "styled":
        texts = writer.styled_replies(llm, mission, author, post, page, cfg)
    # The page hides handles; the gate's context keeps the real ones, so a draft may @ a real replier.
    known = " ".join(f"@{r['author']}" for r in replies if r.get("author"))
    context = (f"POST by @{author}:\n{post}\n\nTHREAD:\n" + "\n".join(f"- {r}" for r in page)
               + f"\n\nHANDLES IN THREAD: {known}\n\nFACTS:\n{facts}")
    offer = Offer(target_urls=frozenset({url}), target_handles=frozenset({author}))
    cands, passing = _gate_candidates(texts, "reply", context, cfg.gate, offer, REPLY_LANE, url)
    detail = f"{method}: {len(passing)}/{len(cands)} drafts passed the gate" + (f" ({note})" if note else "")
    if not passing:
        return Decision(REPLY_LANE, url, "abstain", "gate_rejected", detail, candidates=cands), None
    picked, ground, why = _veto_and_pick(llm, cfg, mission, cands, passing, lane="reply", context=context,
                                         seed=url, grounding=grounding)
    if picked is None:
        return Decision(REPLY_LANE, url, "abstain", "judge_rejected", why, candidates=cands), None
    n: Narrative = t["narrative"]
    action = Action(
        type="reply", text=picked.text, mind="reply_lane", narrative_id=n.id, rationale=intent,
        target_url=url, target_handle=author, lane=REPLY_LANE, target_text=post,
        draft_meta={"method": method, "intent": intent, "samples": len(texts), "page_replies": len(page),
                    "page_dropped_as_bots": dropped,
                    "writer_model": cfg.continuation_model if method == "continuation" else cfg.styled_model,
                    "target": target_meta(t), "judge": judge_meta(cfg, picked, cands, ground)},
    )
    return Decision(REPLY_LANE, url, "reply", detail=detail, candidates=cands), action


# ---------------------------------------------------------------- post lane


def offered(narratives: list[Narrative]) -> tuple[frozenset[str], frozenset[str]]:
    """X post URLs and handles the post lane may quote, follow or mention: the ones the narratives cite."""
    urls, handles = set(), set()
    for n in narratives:
        for u in n.signal_urls:
            host = urlparse(u or "").netloc.removeprefix("www.")
            if host in ("x.com", "twitter.com") and thread.STATUS_ID.search(u):
                urls.add(u)
                handles.add(urlparse(u).path.strip("/").split("/")[0])
        for rt in getattr(n, "reply_targets", None) or []:
            if rt.get("url"):
                urls.add(rt["url"])
            if rt.get("author"):
                handles.add(str(rt["author"]).lstrip("@"))
    return frozenset(urls), frozenset(h for h in handles if h)


def load_takes(path: Path | None = None) -> list[dict]:
    """A person's own takes: action/takes.json, a list of {"text", "quote_url"?}. Never rewritten."""
    path = path or TAKES_PATH
    if not path.exists():
        return []
    takes = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(takes, list) or not all(isinstance(t, dict) and isinstance(t.get("text"), str) for t in takes):
        raise ValueError(f"{path}: expected a JSON list of objects with a 'text' string")
    return takes


def _human_takes(takes: list[dict], offer: Offer, narratives: list[Narrative],
                 budget: Budget) -> tuple[list[Action], list[Decision]]:
    actions, decisions, done = [], [], shipped_takes()
    for take in takes:
        text = take["text"]
        if text in done:
            continue
        kind = "quote" if take.get("quote_url") else "post"
        call = {"tool": kind, "text": text} | ({"target_url": take["quote_url"]} if kind == "quote" else {})
        try:
            parse_call(call, POST_LANE, offer)
        except ActionSchemaError as e:
            decisions.append(Decision(POST_LANE, text, "abstain", "no_source", f"human take: {e}"))
            continue
        # The person's words ship as written. The gate only reports, except X's own length limit.
        findings = gate.check(text, kind).findings
        if any(f.rule in ("length", "empty") for f in findings):
            decisions.append(Decision(POST_LANE, text, "abstain", "gate_rejected", "human take: over X's limit or empty"))
            continue
        if not budget.take(kind):
            decisions.append(Decision(POST_LANE, text, "abstain", "budget_exhausted", f"human take: {kind} cap or spacing"))
            continue
        a = Action(type=kind, text=text, mind="human", narrative_id=narratives[0].id if narratives else "",
                   target_url=take.get("quote_url"), lane=POST_LANE, rationale="a person's take, carried verbatim",
                   draft_meta={"method": "human take, verbatim", "gate_warnings": [asdict(f) for f in findings]})
        actions.append(a)
        decisions.append(Decision(POST_LANE, text, kind, detail="human take, verbatim", action_id=a.id))
    return actions, decisions


def _caption(llm: LLM, mission: str, narratives: list[Narrative], cfg: ActionConfig, budget: Budget,
             offer: Offer, grounding) -> tuple[list[Action], list[Decision]]:
    """Quote a real X post, or link a news story, with a short caption and the source credited."""
    ranked = sorted(narratives, key=lambda n: -n.relevance * n.velocity)
    targets = reply_targets(narratives)
    # X posts the narratives cite but Observe didn't offer as reply targets can still be quoted.
    cited = [(n, u) for n in ranked for u in n.signal_urls if u in offer.target_urls]
    news = [(n, u) for n in ranked for u in n.signal_urls if u and not thread.STATUS_ID.search(u)]
    if targets and budget.left("quote") > 0:
        t = targets[0]
        kind, n, url, source = "quote", t["narrative"], t["url"], f"@{t['author']} posted:\n{t.get('text', '')}"
    elif cited and budget.left("quote") > 0:
        kind, (n, url) = "quote", cited[0]
        source = f"{n.title}\n{n.summary}\n(quoting {url})"
    elif news and budget.left("post") > 0:
        kind, (n, url) = "post", news[0]
        source = f"{n.title}\n{n.summary}\n(source: {urlparse(url).netloc})"
    elif not (targets or cited or news):
        return [], [Decision(POST_LANE, "", "abstain", "no_source", "nothing real to quote or caption")]
    else:
        return [], [Decision(POST_LANE, "", "abstain", "budget_exhausted", "post and quote caps or spacing")]
    texts = writer.captions(llm, mission, source, cfg)
    limits = GateLimits(**{**asdict(cfg.gate), "max_chars": {**cfg.gate.max_chars, kind: cfg.caption_max_chars}})
    context = f"SOURCE:\n{source}\n\nFACTS:\n{facts_block(mission, narratives)}"
    cands, passing = _gate_candidates(texts, kind, context, limits, offer, POST_LANE, url if kind == "quote" else None)
    if not passing:
        return [], [Decision(POST_LANE, url, "abstain", "gate_rejected", f"caption: 0/{len(cands)} passed the gate",
                             candidates=cands)]
    picked, ground, why = _veto_and_pick(llm, cfg, mission, cands, passing, lane=kind, context=context, seed=url,
                                         grounding=grounding)
    if picked is None:
        return [], [Decision(POST_LANE, url, "abstain", "judge_rejected", why, candidates=cands)]
    domain = urlparse(url).netloc.removeprefix("www.")
    text = picked.text if kind == "quote" else f"{picked.text} (via {domain}) {url}"
    budget.take(kind)
    a = Action(type=kind, text=text, mind="caption", narrative_id=n.id, target_url=url if kind == "quote" else None,
               lane=POST_LANE, target_text=source, rationale=f"caption of a real source ({domain})",
               draft_meta={"method": "caption", "writer_model": cfg.styled_model, "source_url": url,
                           "judge": judge_meta(cfg, picked, cands, ground)})
    return [a], [Decision(POST_LANE, url, kind, detail="caption of a real source", action_id=a.id, candidates=cands)]


def post_lane(llm: LLM, mission: str, narratives: list[Narrative], chorus: list[Action], cfg: ActionConfig,
              budget: Budget, takes: list[dict], grounding=None) -> tuple[list[Action], list[Decision]]:
    urls, handles = offered(narratives)
    offer = Offer(target_urls=urls, target_handles=handles)
    actions, decisions, angles = [], [], []
    for a in chorus:  # the chorus ships follows; its text becomes angles for the person
        if a.type != "follow":
            if a.text.strip():
                kind = a.type if a.type in gate.KINDS else "post"
                angles.append({"mind": a.mind, "type": a.type, "text": a.text,
                               "gate": [asdict(f) for f in gate.check(a.text, kind).findings]})
            continue
        try:
            call = parse_call({"tool": "follow", "target_handle": a.target_handle or ""}, POST_LANE, offer)
        except ActionSchemaError as e:
            decisions.append(Decision(POST_LANE, a.target_handle or "", "abstain", "no_target", str(e)))
            continue
        if budget.take("follow"):
            a.target_handle, a.lane, a.draft_meta = call.args["target_handle"], POST_LANE, {"method": "chorus follow"}
            actions.append(a)
            decisions.append(Decision(POST_LANE, a.target_handle, "follow", action_id=a.id))

    took, d = _human_takes(takes, offer, narratives, budget)
    actions += took
    decisions += d
    if not took and narratives:
        cap, d = _caption(llm, mission, narratives, cfg, budget, offer, grounding)
        actions += cap
        decisions += d
        top = max(narratives, key=lambda n: n.relevance * n.velocity)
        decisions.append(Decision(
            POST_LANE, top.id, "ask_human",
            question=(f"{top.title}. Got a take, a clip or a detail only a real fan would know? Add it to "
                      "action/takes.json; the harness picks the timing and posts your words as written."),
            candidates=angles[:3],
        ))
    return actions, decisions


# ---------------------------------------------------------------- run


def run(llm: LLM, mission: str, lessons: str, narratives: list[Narrative], chorus: list[Action], *,
        revising: bool = False, cfg: ActionConfig | None = None, budget: Budget | None = None,
        takes: list[dict] | None = None, fetch=thread.fetch_replies) -> tuple[list[Action], list[Decision]]:
    """Both lanes for one decide() call. Returns the actions that ship and every decision."""
    cfg = cfg or ActionConfig()
    mock = isinstance(llm, MockLLM)
    PENDING_PROVIDER["name"] = "mock" if mock else type(llm).__name__
    budget = budget or (Budget(cfg) if mock else Budget.from_logs(cfg))
    grounding = None if mock else veto.grounding_checker()
    actions, decisions = [], []
    if not revising:  # a veto retry revises chorus drafts; replies aren't redrafted
        a, d = reply_lane(llm, mission, lessons, narratives, cfg, budget, fetch, grounding)
        actions += a
        decisions += d
    a, d = post_lane(llm, mission, narratives, chorus, cfg, budget, load_takes() if takes is None else takes, grounding)
    actions += a
    decisions += d
    print(f"[decide] {summarize(decisions)}")
    for dec in decisions:
        if dec.tool == "ask_human":
            print(f"[decide] question for a person: {dec.question}")
    return actions, decisions


def write_decision_log(run_id: int, out_dir: Path | None = None) -> Path | None:
    """Write PENDING to out/decisions-run<id>.json and clear it. Call after memory.save_run."""
    if not PENDING:
        return None
    out_dir = out_dir or config.OUT_DIR
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"decisions-run{run_id}.json"
    path.write_text(json.dumps({"run_id": run_id, "at": datetime.now().isoformat(timespec="seconds"),
                                "provider": PENDING_PROVIDER["name"], "decisions": [asdict(d) for d in PENDING]}, indent=1, ensure_ascii=False, default=str),
                    encoding="utf-8")
    PENDING.clear()
    return path


def summarize(decisions: list[Decision]) -> str:
    shipped = Counter(d.tool for d in decisions if d.action_id)
    abstained = Counter(d.reason for d in decisions if d.tool == "abstain")
    asks = sum(d.tool == "ask_human" for d in decisions)
    return (f"shipped {dict(shipped) or 'nothing'}; abstained {dict(abstained) or 'never'}"
            + (f"; {asks} question(s) for a person" if asks else ""))
