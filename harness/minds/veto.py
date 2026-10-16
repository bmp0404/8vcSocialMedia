"""The judge only vetoes. It never ranks drafts by expected likes.

Why: on real top replies, judge scores tracked actual likes at Spearman 0.15 (Opus) and about 0
(claude-fable-5-dd-los-6.5-tpg), and selecting "the draft most likely to get likes" made
continuation drafts more detectable, AUC 0.44 to 0.70 (8vc-context/research/action.md, finding 3). What a judge can do is
catch what's wrong with a draft, so it answers four questions per draft: does it read as AI,
does it state something the context doesn't support, does it work against the account's side or
voice in mission/mission.md (sincerely cheering a rival, saying nothing), and could it get the
account muted or reported (insults at a person, which X's FosnrAbuseInsults filter also demotes).
Iterate's 27-thread run shipped "Let's go Angel Reese, another championship 🏆" and "talking shit
about a rookie" before the mission check existed.

Opus 5.5 is the judge because it caught 90-100% of generated replies where
claude-fable-5-dd-los-6.5-tpg (gateway reports gpt-5.6-sol) caught 40-77% (finding 5). Listwise
judgments shift with order, so every batch is judged forward and reversed, and a draft passes only
when both passes clear it; a split counts as a veto.

Dhilan's post_judge (PR #5) contributes one more veto when it's installed: its grounding
criterion, 0-5. Its overall score isn't used, because its rubric rewards the explained register
and its default references are dev-tool posts.
"""

from dataclasses import asdict, dataclass

from harness.llm import LLM
from harness.minds import routing
from harness.minds.settings import ActionConfig

VETO_SYSTEM = """You screen candidate {lane}s before a small account posts them on X. The mission below says whose side the account is on and how it talks. Judge each candidate on its own, against the mission, the post, the thread and the facts given.

For each candidate give:
- p_ai: 0-100, the probability that an AI model wrote it rather than a person scrolling X
- unsupported: true if it states a fact, number, name or event that the post, thread and facts don't support
- off_mission: true if it works against the account's side or voice: it sincerely cheers or congratulates a rival (banter or a jab at a rival is fine), says nothing at all, or is off the mission's subjects
- risk: true if it could get the account muted, blocked or reported: insults, mocks or swears at a person (a player, coach, reporter or fan), piles on, slurs, harassment, spam
- reason: under 15 words

Return JSON only: {{"verdicts": [{{"p_ai": 0, "unsupported": false, "off_mission": false, "risk": false, "reason": "..."}}]}} with exactly one entry per candidate, in the order given.

MISSION:
{mission}"""


@dataclass
class Verdict:
    p_ai: int
    unsupported: bool
    risk: bool
    reason: str = ""
    off_mission: bool = False

    def clears(self, cfg: ActionConfig) -> bool:
        return self.p_ai < cfg.veto_max_p_ai and not self.unsupported and not self.risk and not self.off_mission


@dataclass
class VetoResult:
    text: str
    passed: bool
    fwd: Verdict | None
    rev: Verdict | None
    why: str = ""
    orders_agree: bool = False

    def evidence(self) -> dict:
        return {"passed": self.passed, "why": self.why, "orders_agree": self.orders_agree,
                "fwd": asdict(self.fwd) if self.fwd else None, "rev": asdict(self.rev) if self.rev else None}


class VetoError(RuntimeError):
    """The judge failed or answered in the wrong shape; every draft in the batch counts as vetoed."""


def _judge_llm(llm: LLM, cfg: ActionConfig) -> LLM:
    return routing.on_model(llm, cfg.veto_model, "the veto judge")


def _one_pass(judge: LLM, texts: list[str], lane: str, context: str, mission: str) -> list[Verdict]:
    cands = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    out = judge.complete_json(
        VETO_SYSTEM.format(lane=lane, mission=mission or "(none given)"), f"{context}\n\nCANDIDATES:\n{cands}",
        mock=lambda: {"verdicts": [{"p_ai": 10, "unsupported": False, "off_mission": False, "risk": False,
                                    "reason": "mock"} for _ in texts]},
    )
    try:
        raw = out["verdicts"]
        if len(raw) != len(texts):
            raise VetoError(f"judge returned {len(raw)} verdicts for {len(texts)} candidates")
        # A missing off_mission field counts as a veto: an old-shape answer must not wave drafts through.
        return [Verdict(int(v["p_ai"]), bool(v["unsupported"]), bool(v["risk"]), str(v.get("reason", "")),
                        bool(v.get("off_mission", True))) for v in raw]
    except (KeyError, TypeError, ValueError) as e:
        raise VetoError(f"judge answered in the wrong shape: {e!r}") from e


def p_ai_scores(llm: LLM, cfg: ActionConfig, texts: list[str], *, context: str, mission: str = "") -> list[int]:
    """One-order P(AI) for real replies on the page. Raises VetoError on a malformed answer."""
    return [v.p_ai for v in _one_pass(_judge_llm(llm, cfg), texts, "reply", context, mission)]


def veto(llm: LLM, cfg: ActionConfig, texts: list[str], *, lane: str, context: str, mission: str) -> list[VetoResult]:
    """Judge `texts` forward and reversed. A draft passes only if both passes clear it."""
    texts = list(dict.fromkeys(t for t in texts if t.strip()))
    if not texts:
        return []
    judge = _judge_llm(llm, cfg)
    try:
        fwd = _one_pass(judge, texts, lane, context, mission)
        rev = _one_pass(judge, texts[::-1], lane, context, mission)[::-1]
    except VetoError as e:
        return [VetoResult(t, False, None, None, str(e)) for t in texts]
    results = []
    for t, f, r in zip(texts, fwd, rev):
        a, b = f.clears(cfg), r.clears(cfg)
        if a and b:
            results.append(VetoResult(t, True, f, r, orders_agree=True))
            continue
        if a != b:
            why = "split verdict between orders"
        else:
            flags = [n for n, on in (("reads as AI", max(f.p_ai, r.p_ai) >= cfg.veto_max_p_ai),
                                     ("unsupported claim", f.unsupported or r.unsupported),
                                     ("off mission", f.off_mission or r.off_mission),
                                     ("mute/report risk", f.risk or r.risk)) if on]
            why = ", ".join(flags)
        results.append(VetoResult(t, False, f, r, why, orders_agree=a == b))
    return results


class GroundingUnavailable(RuntimeError):
    pass


class PostJudgeGrounding:
    """Dhilan's judge, used only for its grounding score. baml_sdk is generated, so import lazily."""

    def __init__(self):
        try:
            from baml_sdk import CandidatePost, Performance, ReferencePost  # noqa: F401
            from harness.post_judge.judge import grade_with_references  # noqa: F401
        except ImportError as e:
            raise GroundingUnavailable(
                f"post_judge unavailable ({e}); merge PR #5 and run `BAML_VERSION=0.22.2 baml generate --directory eval`"
            ) from e

    def score(self, text: str, *, mission: str, context: str, lane: str, references: list[dict]) -> tuple[int, str]:
        from baml_sdk import CandidatePost, Performance, ReferencePost
        from harness.post_judge.judge import grade_with_references

        refs = [ReferencePost(id=r["id"], text=r["text"], account=r["account"], performance=Performance(r["performance"]),
                              category=r["category"], engagement=r["engagement"], media=r["media"], date=r["date"])
                for r in references]
        ev = grade_with_references(CandidatePost(text=text, mission=mission, context=context, lane=lane), refs,
                                   dataset_sha256=references[0].get("corpus_sha256", "") if references else "")
        return ev.grade.grounding.score, ev.grade.grounding.reason


def grounding_checker():
    """PostJudgeGrounding when installed, else None. HARNESS_GROUNDING=off disables it."""
    import os

    if os.getenv("HARNESS_GROUNDING", "").lower() == "off":
        return None
    try:
        return PostJudgeGrounding()
    except GroundingUnavailable as e:
        print(f"[decide] {e}; skipping the grounding veto")
        return None
