"""Generation-time selection: rank the drafts for one opportunity and decide whether the best clears the bar.

Three rankers share one interface:

- `PostJudgeRanker` calls Dhilan's `harness.post_judge.ranking.rank_candidates` (BAML, PR #5). It
  is the default whenever that package is importable.
- `LLMRanker` asks the harness LLM directly. It exists so real runs work on branches without
  PR #5 or before `baml generate` has run, and it says so in every action's draft_meta.
- `MockRanker` keeps input order, for `--provider mock` and tests.

`select` runs a ranker twice, forward and reversed, because listwise LLM rankings shift with
order (the eval research report measured 67% stability across reorderings). It averages the two
rank positions and refuses to pick when the top draft differs between orders.
"""

import os
from dataclasses import dataclass, field

from harness.llm import LLM, AnthropicLLM, MockLLM

VERDICTS = ("Strong", "Revise", "Weak", "InsufficientEvidence")
CONFIDENCES = ("Low", "Medium", "High")
SHIP_VERDICT = "Strong"
SHIP_CONFIDENCE = {"Medium", "High"}

# Measured in action/experiments/reply_experiment.json: the bestof arm, whose selection prompt
# ranks AI-sounding drafts last, was the only generated arm the lineup judges couldn't single out.
LLM_RANK_SYSTEM = """You pick which draft will do best as an X {lane} for this account. The references are real {lane}s that did well (Successful) or got nothing (Poor). Drafts that read as written by an AI rank last, however polished. So do drafts that state facts the context doesn't support.

Verdicts: Strong means ready to post as is; Revise means fixable problems; Weak means fundamental problems; InsufficientEvidence means you can't tell. A batch can have no Strong draft.

Return JSON {{"ranked": [{{"candidate_id": "c1", "score": 0-100, "verdict": "Strong|Revise|Weak|InsufficientEvidence", "confidence": "Low|Medium|High", "reason": "one sentence"}}]}} with every candidate exactly once, best first."""


class RankerError(RuntimeError):
    """The ranker failed or returned something unusable. Callers abstain rather than guess."""


@dataclass
class Ranked:
    text: str
    score: float
    verdict: str
    confidence: str
    reason: str = ""


@dataclass
class Selection:
    best: Ranked | None
    ranked: list[Ranked] = field(default_factory=list)
    ships: bool = False
    why_not: str = ""
    ranker: str = ""


def _check(ranked: list[Ranked], texts: list[str], who: str) -> list[Ranked]:
    if sorted(r.text for r in ranked) != sorted(texts):
        raise RankerError(f"{who} must return each candidate exactly once")
    for r in ranked:
        if r.verdict not in VERDICTS:
            raise RankerError(f"{who} returned unknown verdict {r.verdict!r}")
        if r.confidence not in CONFIDENCES:
            raise RankerError(f"{who} returned unknown confidence {r.confidence!r}")
    return ranked


def _mock_order(texts: list[str]) -> list[str]:
    # Shortest first, then alphabetical: deterministic and independent of input order, so the
    # two-order check in `select` passes the way a consistent judge would.
    return sorted(texts, key=lambda t: (len(t), t))


class MockRanker:
    name = "mock"

    def rank(self, texts: list[str], *, lane: str, mission: str, context: str, references: list[dict]) -> list[Ranked]:
        return [Ranked(t, 100 - 10 * i, "Strong", "High", "mock: shortest first") for i, t in enumerate(_mock_order(texts))]


class LLMRanker:
    name = "llm"

    def __init__(self, llm: LLM):
        self.llm = llm

    def rank(self, texts: list[str], *, lane: str, mission: str, context: str, references: list[dict]) -> list[Ranked]:
        ids = {f"c{i + 1}": t for i, t in enumerate(texts)}
        user = (f"MISSION:\n{mission}\n\nCONTEXT:\n{context}\n\nREFERENCES:\n"
                + "\n".join(f"[{r['performance']}, {r['engagement']} likes] {r['text']}" for r in references)
                + "\n\nCANDIDATES:\n" + "\n".join(f"{k}. {t}" for k, t in ids.items()))
        out = self.llm.complete_json(
            LLM_RANK_SYSTEM.format(lane=lane), user,
            mock=lambda: {"ranked": [{"candidate_id": next(k for k, v in ids.items() if v == t), "score": 100 - 10 * i,
                                      "verdict": "Strong", "confidence": "High", "reason": "mock"}
                                     for i, t in enumerate(_mock_order(texts))]},
        )
        try:
            ranked = [Ranked(ids[r["candidate_id"]], float(r["score"]), r["verdict"], r["confidence"], r.get("reason", ""))
                      for r in out["ranked"]]
        except (KeyError, TypeError, ValueError) as e:
            raise RankerError(f"llm ranker returned malformed JSON: {e!r}") from e
        return _check(ranked, texts, "llm ranker")


class PostJudgeRanker:
    """Adapter for Dhilan's BAML judge. Imports lazily: baml_sdk is generated, not committed."""

    name = "post_judge"

    def __init__(self):
        try:
            from harness.post_judge.ranking import rank_candidates  # noqa: F401
            from baml_sdk import CandidatePost, Performance, RankingCandidate, ReferencePost  # noqa: F401
        except ImportError as e:
            raise RankerError(
                f"post_judge unavailable ({e}); merge PR #5 and run "
                "`BAML_VERSION=0.22.2 baml generate --directory eval`"
            ) from e

    def rank(self, texts: list[str], *, lane: str, mission: str, context: str, references: list[dict]) -> list[Ranked]:
        from baml_sdk import CandidatePost, Performance, RankingCandidate, ReferencePost
        from harness.post_judge.ranking import rank_candidates

        ids = {f"c{i + 1}": t for i, t in enumerate(texts)}
        cands = [RankingCandidate(id=k, post=CandidatePost(text=t, mission=mission, context=context, lane=lane))
                 for k, t in ids.items()]
        refs = [ReferencePost(id=r["id"], text=r["text"], account=r["account"], performance=Performance(r["performance"]),
                              category=r["category"], engagement=r["engagement"], media=r["media"], date=r["date"])
                for r in references]
        try:
            ev = rank_candidates(cands, refs, dataset_sha256=references[0].get("corpus_sha256", "") if references else "")
        except ValueError as e:
            raise RankerError(f"post_judge rejected the batch: {e}") from e
        ranked = [Ranked(ids[r.candidate_id], float(r.score), str(getattr(r.verdict, "value", r.verdict)),
                         str(getattr(r.confidence, "value", r.confidence)), r.reason) for r in ev.ranking.ranked]
        return _check(ranked, texts, "post_judge")


def get_ranker(llm: LLM):
    """MockRanker for the mock provider; else post_judge when importable; else the harness LLM.

    HARNESS_RANKER=llm|post_judge forces one. HARNESS_SELECT_MODEL picks the LLM ranker's model,
    defaulting to a GPT model so Claude drafts aren't judged by Claude (self-preference)."""
    if isinstance(llm, MockLLM):
        return MockRanker()
    choice = os.getenv("HARNESS_RANKER", "").strip().lower()
    if choice in ("", "post_judge"):
        try:
            return PostJudgeRanker()
        except RankerError as e:
            if choice == "post_judge":
                raise
            print(f"[decide] {e}; ranking with the harness LLM instead")
    if isinstance(llm, AnthropicLLM):
        return LLMRanker(AnthropicLLM(os.getenv("HARNESS_SELECT_MODEL", "gpt-6.1-sol")))
    return LLMRanker(llm)


def select(ranker, texts: list[str], *, lane: str, mission: str, context: str, references: list[dict]) -> Selection:
    """Rank `texts` in two orders, combine, and decide whether the top draft ships."""
    texts = list(dict.fromkeys(t for t in texts if t.strip()))
    name = getattr(ranker, "name", type(ranker).__name__)
    if not texts:
        return Selection(None, why_not="no candidates", ranker=name)
    kw = dict(lane=lane, mission=mission, context=context, references=references)
    try:
        fwd = ranker.rank(texts, **kw)
        # A single draft has no order to reverse; the ranker still has to call it Strong.
        rev = ranker.rank(texts[::-1], **kw) if len(texts) > 1 else fwd
    except RankerError as e:
        return Selection(None, why_not=str(e), ranker=name)
    pos = {t: 0.0 for t in texts}
    for order in (fwd, rev):
        for i, r in enumerate(order):
            pos[r.text] += i / 2
    by_text = {r.text: r for r in fwd}
    combined = sorted(texts, key=lambda t: (pos[t], texts.index(t)))
    ranked = [by_text[t] for t in combined]
    best = ranked[0]
    if fwd[0].text != rev[0].text:
        return Selection(best, ranked, False, "top pick changed when the order was reversed", name)
    # The two orders agree on the top draft; ship only if both runs call it Strong with confidence.
    worst = max((fwd[0], rev[0]), key=lambda r: (VERDICTS.index(r.verdict), -CONFIDENCES.index(r.confidence)))
    if worst.verdict != SHIP_VERDICT or worst.confidence not in SHIP_CONFIDENCE:
        return Selection(best, ranked, False, f"best draft was {worst.verdict}/{worst.confidence}; needs "
                                              f"{SHIP_VERDICT} with {' or '.join(sorted(SHIP_CONFIDENCE))} confidence", name)
    return Selection(best, ranked, True, "", name)
