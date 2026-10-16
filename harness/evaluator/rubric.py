"""Rubric judge: 1-5 on explicit, observable criteria, with a one-line justification each.

On the claude backend this runs on the judge tier (the configured model, Sonnet by default), batched
CLAUDE_BATCH["judge"] drafts per call. On a decision model each criterion is a 5-level `score`
question and the criterion score is the probability-weighted expected level (calibrated, can fall
between levels); decision models return no justification.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.evaluator.candidates import Candidate, EvalContext
from harness.evaluator.decide import Question, decide_batch

# name: (question, anchor for 1, anchor for 3, anchor for 5)
CRITERIA = {
    "hook": ("Does the FIRST LINE alone make a developer stop scrolling?",
             "first line is throat-clearing, generic or a restated headline",
             "first line is clear but unremarkable",
             "first line is a sharp claim, joke or concrete image you can't scroll past"),
    "specificity": ("How concrete and specific is it?",
                    "abstract buzzwords, no detail (\"AI is changing everything\")",
                    "one concrete detail among generalities",
                    "names a specific tool, file, failure mode, number or moment a practitioner recognizes"),
    "adds_new": ("Does it add something new? For a reply: relevant to the target post and not generic.",
                 "restates the obvious / the target post; could be pasted under any post",
                 "relevant, but a take people have seen many times",
                 "a new angle, tip, fact or joke that moves the conversation forward"),
    "brand_voice": ("Does it fit the brand voice in the MISSION (dry, specific, technical, confident, never smug)?",
                    "corporate polish, hype or a voice the MISSION forbids",
                    "neutral; could be any account",
                    "unmistakably the MISSION's voice"),
    "grounding": ("Is it factually grounded? Brand claims must appear in the MISSION; no invented stats, "
                  "customers or features; claims about others must be plausible and fair.",
                  "invents brand facts/stats or makes an unsupported claim about someone",
                  "mostly opinion with a vague or unverifiable claim",
                  "every factual claim is supported by the MISSION or is common knowledge"),
    "low_cringe": ("How free of cringe and salesiness is it (5 = no cringe)?",
                   "ad copy, engagement bait, forced meme, hashtag spam or a plug nobody asked for",
                   "slightly try-hard or a mild plug",
                   "reads like a respected engineer talking; zero sales energy"),
    "natural_x": ("Does it read natively for X (length, rhythm, format)?",
                  "press-release / LinkedIn / blog paragraph energy, or broken formatting",
                  "fine but a little long or stiff",
                  "short, punchy, natural line breaks; looks like it was born on X"),
}
# `total` (the engagement score) uses only the engagement criteria. The guardrails (voice, grounding,
# cringe) measure whether WE should post it, not whether it will land: on 80 real dev-tool posts they
# correlated negatively with engagement (launch posts score low on them and go viral), and dropping
# them from the total raised Spearman 0.28 -> 0.42. They feed `guard` and `flags` instead.
ENGAGEMENT = ("hook", "specificity", "adds_new", "natural_x")
GUARDRAILS = ("brand_voice", "grounding", "low_cringe")
WEIGHTS = {"hook": 1.0, "specificity": 1.0, "adds_new": 1.0, "natural_x": 1.0}
FLAG_BELOW = {"grounding": 2.5, "low_cringe": 2.0, "brand_voice": 2.0}  # expected score under this -> flagged

SYSTEM = """You are a strict editor scoring X (Twitter) drafts for the brand account in the MISSION.
Score each criterion from 1 to 5 using its anchors (2 and 4 sit between them). Score only what is
observable in the text; do not reward intent. Most drafts are a 2 or 3 on most criteria.

MISSION:
{mission}"""


def questions() -> list[Question]:
    out = []
    for name, (q, a1, a3, a5) in CRITERIA.items():
        out.append(Question(name, q, {"1": a1, "2": "between 1 and 3", "3": a3, "4": "between 3 and 5", "5": a5},
                            kind="score"))
    return out


@dataclass
class CriterionScore:
    score: float  # expected level 1-5 (= the choice on claude)
    why: str = ""
    probs: dict[str, float] | None = None


@dataclass
class RubricResult:
    scores: dict[str, CriterionScore] = field(default_factory=dict)
    total: float = 0.0  # engagement criteria, weighted mean rescaled to 0-1
    guard: float = 0.0  # guardrail criteria (voice, grounding, cringe), mean rescaled to 0-1
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": round(self.total, 3), "guard": round(self.guard, 3), "flags": self.flags,
                "scores": {k: {"score": round(v.score, 2), "why": v.why} for k, v in self.scores.items()}}


def aggregate(scores: dict[str, CriterionScore], weights: dict[str, float] = WEIGHTS) -> float:
    got = [(w, scores[k].score) for k, w in weights.items() if k in scores]
    if not got:
        return 0.0
    return (sum(w * s for w, s in got) / sum(w for w, _ in got) - 1) / 4


def rubric(cands: list[Candidate], ectx: EvalContext, backend: str | None = None) -> dict[str, RubricResult]:
    """{candidate id: RubricResult} for every candidate with text."""
    todo = [c for c in cands if c.text.strip()]
    if not todo:
        return {}
    answers = decide_batch([c.render() for c in todo], questions(), backend or ectx.decider, llm=ectx.llm,
                           system=SYSTEM.format(mission=ectx.mission), tier="judge")
    out = {}
    for c, ans in zip(todo, answers):
        scores = {}
        for name, d in ans.items():
            v = d.expected()
            if v is not None:
                scores[name] = CriterionScore(round(v, 3), d.why, d.probs)
        flags = [k for k, lim in FLAG_BELOW.items() if k in scores and scores[k].score < lim]
        guard = aggregate(scores, {k: 1.0 for k in GUARDRAILS})
        out[c.id] = RubricResult(scores, aggregate(scores), guard, flags)
    return out
