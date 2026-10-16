"""evaluate(): rubric + pairwise tournament (+ triage for replies) over a batch of drafts, best first."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.evaluator.candidates import Candidate, EvalContext
from harness.evaluator.metrics import ranks
from harness.evaluator.rubric import RubricResult, rubric
from harness.evaluator.tournament import Tournament, run_tournament
from harness.evaluator.triage import Triage, triage
from harness.models import Action


@dataclass
class Ranked:
    candidate: Candidate
    rank: int = 0
    score: float = 0.0  # = predicted_score: weighted mean of within-batch percentiles (see EvalContext)
    win_prob: float | None = None  # P(beats an average draft in this batch), from Bradley-Terry
    rating: float | None = None  # Elo-style, 1500 = batch average
    rubric: RubricResult | None = None
    triage: Triage | None = None
    flags: list[str] = field(default_factory=list)
    features: float | None = None  # no-LLM feature-model score (HARNESS_EVAL_FEATURES=on, post lane)

    @property
    def action(self) -> Action | None:
        return self.candidate.action

    def to_dict(self) -> dict:
        return {"rank": self.rank, "score": round(self.score, 3), "win_prob": self.win_prob,
                "rating": round(self.rating) if self.rating is not None else None,
                "rubric": self.rubric.to_dict() if self.rubric else None,
                "triage": self.triage.to_dict() if self.triage else None, "flags": self.flags,
                **({"features": round(self.features, 3)} if self.features is not None else {})}


def _pct(xs: list[float]) -> list[float]:
    r = ranks(xs)
    return [(v - 1) / (len(xs) - 1) if len(xs) > 1 else 0.5 for v in r]


def _blend(live: list[Ranked], ectx: EvalContext) -> None:
    """score = weighted mean of within-batch percentiles of the available components."""
    comps = []  # (weight, values)
    panel = [r.action.predicted_score if r.action is not None and r.action.panel else None for r in live]
    if ectx.w_panel and live and all(v is not None for v in panel):
        comps.append((ectx.w_panel, panel))
    if ectx.w_tournament and live:
        comps.append((ectx.w_tournament, [r.win_prob if r.win_prob is not None else 0.5 for r in live]))
    if ectx.w_rubric and live and all(r.rubric for r in live):
        comps.append((ectx.w_rubric, [r.rubric.total for r in live]))
    if ectx.w_features and live and ectx.lane == "post":  # trained on original posts, not replies
        from harness.evaluator import features

        for r in live:
            r.features = features.score(r.candidate.text)
        if all(r.features is not None for r in live):
            comps.append((ectx.w_features, [r.features for r in live]))
    if not comps:
        return
    pcts = [(w, _pct(vals)) for w, vals in comps]
    tot = sum(w for w, _ in pcts)
    for k, r in enumerate(live):
        r.score = sum(w * p[k] for w, p in pcts) / tot


def _eligible(c: Candidate) -> bool:
    a = c.action
    return bool(c.text.strip()) and not (a and a.vetoed)


def evaluate(candidates: list[Action | Candidate], ctx: Any, lane: str | None = None,
             backend: str | None = None, annotate: bool = True) -> list[Ranked]:
    """Rank drafts. `ctx` is an EvalContext or a lanes.common.LaneContext.

    Vetoed drafts (e.g. by qualify's critic) and text-less actions (follows) are not judged; they keep
    their existing predicted_score and sort last. For Actions, `predicted_score` is overwritten with the
    batch-relative score (0-1) and, with `annotate`, the details go to `action.context["eval"]`.
    """
    ectx = EvalContext.of(ctx, lane)
    cands = [c if isinstance(c, Candidate) else Candidate.from_action(c) for c in candidates]
    live = [c for c in cands if _eligible(c)]
    is_live = {id(c) for c in live}
    rub = rubric(live, ectx, backend) if live and (ectx.rubric or ectx.w_rubric) else {}
    tour = run_tournament(live, ectx, backend) if len(live) > 1 else Tournament(
        win_prob={c.id: 0.5 for c in live}, strength={c.id: 1.0 for c in live})

    tri: dict[str, Triage] = {}
    if ectx.triage:
        targets = {}
        for c in live:
            a = c.action
            if c.kind in {"reply", "quote"} and c.target_key and a is not None:
                targets.setdefault(c.target_key, {"url": c.target_key, "author": (a.context or {}).get("author"),
                                                  "text": c.responding_to})
        tri = triage(list(targets.values()), ectx, backend) if targets else {}

    ranked = []
    for c in cands:
        r = Ranked(c)
        if id(c) in is_live:
            r.rubric = rub.get(c.id)
            r.win_prob = tour.win_prob.get(c.id)
            r.rating = tour.rating(c.id)
            r.triage = tri.get(c.target_key or "")
            r.flags = list(r.rubric.flags) if r.rubric else []
            if r.triage and r.triage.decision != "engage":
                r.flags.append(f"triage:{r.triage.decision}")
        elif c.action is not None:
            r.score = c.action.predicted_score
        ranked.append(r)

    _blend([r for r in ranked if id(r.candidate) in is_live], ectx)
    ranked.sort(key=lambda r: (id(r.candidate) in is_live, r.score), reverse=True)
    for n, r in enumerate(ranked, 1):
        r.rank = n
        a = r.action
        if a is not None and id(r.candidate) in is_live:
            a.predicted_score = round(r.score, 3)
            if annotate:
                a.context = {**(a.context or {}), "eval": r.to_dict()}
    print(f"[evaluator] ranked {len(live)} drafts ({len(cands) - len(live)} skipped): "
          f"{len(rub)} rubric, {tour.calls} pairwise decisions, {len(tri)} triaged")
    return ranked
