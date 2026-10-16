"""The two lanes of the decide stage. Every opportunity ends in exactly one Decision: an action
that ships, or an abstention with a reason code from `schema.ABSTAIN_REASONS`.

Reply lane, per live X post that Observe offers in `narrative.reply_targets`:
  stale check -> 5 independent drafts as reply tool calls -> schema check -> slop gate ->
  rank in two orders -> ship the top draft only if both orders agree and call it Strong.
Post lane: the persona chorus drafts posts, mentions and follows; each goes through the same
schema check and gate, at most one post ships per run, and when none does the lane asks a person
for the take instead (8VC's Create step: humans bring the stories, the harness times and formats).

Why this shape, with the measurements, is in action/DESIGN.md.
"""

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from harness import config
from harness.llm import LLM
from harness.minds import corpus, gate, judge
from harness.minds.schema import POST_LANE, REPLY_LANE, ActionSchemaError, Offer, json_schema, parse_call
from harness.models import Action, Narrative

REPLY_PROMPT = (Path(__file__).parent / "lane_prompts" / "reply.md").read_text(encoding="utf-8")
REPLY_SAMPLES = 5  # the bestof arm in action/experiments/reply_experiment.json used 5
# 61 of the 72 top-3 replies in action/fixtures/x_threads.json landed within 120 minutes of the
# root (53 within 60). Past that, a new reply mostly lands below the visible page.
STALE_MINUTES = 120
# No measurement behind these caps. They keep Grok's queue short and stay clear of X's burst
# filters, which flag fast reply bursts and several replies under one root (observe research).
RUN_CAPS = {"reply": 3, "quote": 1, "post": 1, "mention": 1, "follow": 3}
DAILY_CAPS = {"reply": 12, "quote": 3, "post": 2, "mention": 2, "follow": 10}
X_STATUS = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/(\w{1,15})/status/\d+")


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


class Budget:
    """Per-run and per-day caps on recommended actions, by type."""

    def __init__(self, run_caps: dict | None = None, daily_caps: dict | None = None, used_today: Counter | None = None):
        self.run_caps = dict(RUN_CAPS if run_caps is None else run_caps)
        self.daily_caps = dict(DAILY_CAPS if daily_caps is None else daily_caps)
        self.used_today = Counter(used_today or {})
        self.used_run: Counter = Counter()

    def left(self, kind: str) -> int:
        return max(0, min(self.run_caps.get(kind, 0) - self.used_run[kind],
                          self.daily_caps.get(kind, 0) - self.used_today[kind] - self.used_run[kind]))

    def take(self, kind: str) -> bool:
        if self.left(kind) <= 0:
            return False
        self.used_run[kind] += 1
        return True

    @classmethod
    def from_logs(cls, out_dir: Path | None = None, today: str | None = None) -> "Budget":
        """Count today's recommended actions from earlier decision logs in out/."""
        out_dir = out_dir or config.OUT_DIR
        today = today or datetime.now().strftime("%Y%m%d")
        used: Counter = Counter()
        for path in sorted(out_dir.glob(f"decisions-{today}-*.json")):
            for d in json.loads(path.read_text(encoding="utf-8")).get("decisions", []):
                if d.get("action_id"):
                    used[d["tool"]] += 1
        return cls(used_today=used)


def reply_targets(narratives: list[Narrative]) -> list[dict]:
    """Every reply target across all narratives, not only the ones the chorus was shown, fastest first.
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


def _post_block(t: dict) -> str:
    age = t.get("age_minutes")
    return f"@{t['author']} posted{f' {age:.0f} minutes ago' if age is not None else ''}:\n{t.get('text', '')}"


def _draft_reply(llm: LLM, mission: str, lessons: str, t: dict, i: int) -> dict:
    n: Narrative = t["narrative"]
    ex = "\n".join(f"[{r['likes']}] {r['text']}" for r in corpus.exemplars(exclude_url=t["url"]))
    user = (
        f"EXAMPLES:\n{ex or '(no reply corpus found; see harness/minds/corpus.py)'}\n\n"
        f"LESSONS FROM PAST ROUNDS:\n{lessons}\n\n"
        f"WHAT'S GOING ON:\n{n.title}: {n.summary}\n\n"
        f"POST TO ANSWER:\n{_post_block(t)}\n\n"
        f"Answer with exactly one tool call as JSON, matching this schema:\n{json.dumps(json_schema(REPLY_LANE))}\n"
        f'Use "reply" with target_url "{t["url"]}" and your move in "rationale", or "abstain" with reason "no_angle".'
    )
    return llm.complete_json(
        f"{REPLY_PROMPT}\nMISSION:\n{mission}", user,
        mock=lambda: {"tool": "reply", "target_url": t["url"], "text": f"mock reply {i + 1} to @{t['author']}",
                      "rationale": "mock"},
    )


def reply_lane(llm: LLM, mission: str, lessons: str, narratives: list[Narrative], ranker, budget: Budget) -> tuple[list[Action], list[Decision]]:
    targets = reply_targets(narratives)
    if not targets:
        return [], [Decision(REPLY_LANE, "", "abstain", "no_target", "no narrative carried reply_targets")]
    actions, decisions = [], []
    for t in targets:
        if budget.left("reply") <= 0:
            decisions.append(Decision(REPLY_LANE, t["url"], "abstain", "budget_exhausted", f"reply cap {budget.run_caps['reply']}/run"))
            continue
        if (t.get("age_minutes") or 0) > STALE_MINUTES:
            decisions.append(Decision(REPLY_LANE, t["url"], "abstain", "stale_target",
                                      f"{t['age_minutes']:.0f} min old; cutoff {STALE_MINUTES}"))
            continue
        d, a = _answer_target(llm, mission, lessons, t, ranker)
        if a and budget.take(a.type):
            actions.append(a)
            d.action_id = a.id
        elif a:
            d = Decision(REPLY_LANE, t["url"], "abstain", "budget_exhausted", f"{a.type} cap reached", candidates=d.candidates)
        decisions.append(d)
    return actions, decisions


def _answer_target(llm: LLM, mission: str, lessons: str, t: dict, ranker) -> tuple[Decision, Action | None]:
    offer = Offer(target_urls=frozenset({t["url"]}), target_handles=frozenset({t["author"]}))
    with ThreadPoolExecutor(REPLY_SAMPLES) as pool:
        raw = list(pool.map(lambda i: _safe(lambda: _draft_reply(llm, mission, lessons, t, i)), range(REPLY_SAMPLES)))
    cands, passing, abstains = [], [], []
    for obj in raw:
        if isinstance(obj, Exception):
            cands.append({"error": f"{type(obj).__name__}: {obj}"})
            continue
        try:
            call = parse_call(obj, REPLY_LANE, offer)
        except ActionSchemaError as e:
            cands.append({"raw": obj, "schema_error": str(e)})
            continue
        if call.tool == "abstain":
            abstains.append(call.args.get("detail", call.args["reason"]))
            cands.append({"abstain": call.args})
            continue
        if call.tool not in ("reply", "quote"):
            cands.append({"raw": obj, "schema_error": f"{call.tool} is not a reply draft"})
            continue
        res = gate.check(call.args["text"], call.tool)
        cands.append({"text": call.args["text"], "move": call.args.get("rationale", ""), "gate": res.reasons()})
        if res.passed:
            passing.append(call)
    if not passing:
        reason = "no_angle" if abstains and len(abstains) >= len(raw) / 2 else "gate_rejected"
        return Decision(REPLY_LANE, t["url"], "abstain", reason, f"{len(passing)}/{len(raw)} drafts usable", candidates=cands), None

    n: Narrative = t["narrative"]
    sel = judge.select(ranker, [c.args["text"] for c in passing], lane="reply", mission=mission,
                       context=_post_block(t), references=corpus.references(exclude_url=t["url"]))
    for c in cands:
        for r in sel.ranked:
            if c.get("text") == r.text:
                c.update(score=r.score, verdict=r.verdict, confidence=r.confidence, judge_reason=r.reason)
    if not sel.ships:
        return Decision(REPLY_LANE, t["url"], "abstain", "judge_rejected", sel.why_not, candidates=cands), None
    call = next(c for c in passing if c.args["text"] == sel.best.text)
    action = Action(
        type=call.tool, text=call.args["text"], mind="reply_lane", narrative_id=n.id,
        rationale=call.args.get("rationale", ""), target_url=t["url"], target_handle=t["author"],
        lane=REPLY_LANE, target_text=t.get("text", ""),
        draft_meta={"method": "exemplar remix, best of 5, gate, two-order selection", "samples": len(raw),
                    "gate_passed": len(passing), "ranker": sel.ranker, "verdict": sel.best.verdict,
                    "confidence": sel.best.confidence, "judge_reason": sel.best.reason,
                    "runner_up": sel.ranked[1].text if len(sel.ranked) > 1 else None,
                    "target_age_minutes": t.get("age_minutes"), "target_engagement_per_hour": t.get("engagement_per_hour"),
                    "corpus": corpus.label()},
    )
    return Decision(REPLY_LANE, t["url"], call.tool, candidates=cands), action


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - one failed sample shouldn't sink the target
        return e


def offered_handles(narratives: list[Narrative]) -> frozenset[str]:
    """Handles the post lane may follow or mention: authors of X posts the narratives cite."""
    handles = set()
    for n in narratives:
        for url in n.signal_urls:
            if m := X_STATUS.match(url or ""):
                handles.add(m.group(1))
        for rt in getattr(n, "reply_targets", None) or []:
            if rt.get("author"):
                handles.add(str(rt["author"]).lstrip("@"))
    return frozenset(handles)


def post_lane(mission: str, narratives: list[Narrative], chorus: list[Action], ranker, budget: Budget) -> tuple[list[Action], list[Decision]]:
    offer = Offer(target_handles=offered_handles(narratives))
    decisions, actions, writing, rejected = [], [], [], []
    for a in chorus:
        obj = {"tool": a.type, "text": a.text, "rationale": a.rationale, "target_url": a.target_url, "target_handle": a.target_handle}
        if a.type == "follow":
            obj.pop("text")
        try:
            call = parse_call({k: v for k, v in obj.items() if v not in (None, "")}, POST_LANE, offer)
        except ActionSchemaError as e:
            rejected.append({"mind": a.mind, "type": a.type, "text": a.text, "schema_error": str(e)})
            continue
        if call.tool == "follow":
            if budget.take("follow"):
                a.target_handle, a.lane, a.draft_meta = call.args["target_handle"], POST_LANE, {"method": "chorus follow"}
                actions.append(a)
                decisions.append(Decision(POST_LANE, call.args["target_handle"], "follow", action_id=a.id))
            continue
        res = gate.check(call.args["text"], call.tool)
        if res.passed:
            writing.append(a)
        else:
            rejected.append({"mind": a.mind, "type": a.type, "text": a.text, "gate": res.reasons()})
    if rejected:
        decisions.append(Decision(POST_LANE, "chorus", "abstain", "gate_rejected",
                                  f"{len(rejected)} chorus drafts failed the schema or the gate", candidates=rejected))

    shipped_post = False
    if writing and budget.left("post") > 0:
        sel = judge.select(ranker, [a.text for a in writing], lane="post", mission=mission,
                           context="\n".join(f"- {n.title}: {n.summary}" for n in narratives), references=[])
        cands = [{"mind": a.mind, "text": a.text} for a in writing]
        if sel.ships:
            best = next(a for a in writing if a.text == sel.best.text)
            if budget.take(best.type):
                best.lane = POST_LANE
                best.draft_meta = {"method": "chorus, gate, two-order selection", "drafts": len(writing), "ranker": sel.ranker,
                                   "verdict": sel.best.verdict, "confidence": sel.best.confidence, "judge_reason": sel.best.reason}
                actions.append(best)
                decisions.append(Decision(POST_LANE, best.narrative_id, best.type, action_id=best.id, candidates=cands))
                shipped_post = True
        else:
            decisions.append(Decision(POST_LANE, "chorus", "abstain", "judge_rejected", sel.why_not, candidates=cands))
    if not shipped_post and narratives:
        top = max(narratives, key=lambda n: n.relevance * n.velocity)
        decisions.append(Decision(
            POST_LANE, top.id, "ask_human",
            question=(f"{top.title}. What's your one-line take, joke, or detail only a real fan would know? "
                      "The harness will pick the timing and format and won't rewrite your words."),
        ))
    return actions, decisions


def write_log(decisions: list[Decision], out_dir: Path | None = None) -> Path:
    out_dir = out_dir or config.OUT_DIR
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"decisions-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"decisions": [asdict(d) for d in decisions]}, indent=1, ensure_ascii=False, default=str),
                    encoding="utf-8")
    return path


def summarize(decisions: list[Decision]) -> str:
    shipped = Counter(d.tool for d in decisions if d.action_id)
    abstained = Counter(d.reason for d in decisions if d.tool == "abstain")
    asks = sum(d.tool == "ask_human" for d in decisions)
    return (f"shipped {dict(shipped) or 'nothing'}; abstained {dict(abstained) or 'never'}"
            + (f"; {asks} question(s) for a person" if asks else ""))


def run(llm: LLM, mission: str, lessons: str, narratives: list[Narrative], chorus: list[Action],
        revising: bool = False, budget: Budget | None = None) -> list[Action]:
    """Both lanes for one decide() call. Returns only actions that ship; the decision log in
    out/decisions-<ts>.json records every abstention, rejected draft and question for a person."""
    ranker = judge.get_ranker(llm)
    budget = budget or Budget.from_logs()
    actions, decisions = [], []
    if not revising:  # a veto retry revises chorus drafts; replies aren't redrafted
        a, d = reply_lane(llm, mission, lessons, narratives, ranker, budget)
        actions += a
        decisions += d
    a, d = post_lane(mission, narratives, chorus, ranker, budget)
    actions += a
    decisions += d
    path = write_log(decisions)
    print(f"[decide] {summarize(decisions)} (ranker: {getattr(ranker, 'name', '?')}) -> {path.name}")
    for dec in decisions:
        if dec.tool == "ask_human":
            print(f"[decide] question for a person: {dec.question}")
    return actions
