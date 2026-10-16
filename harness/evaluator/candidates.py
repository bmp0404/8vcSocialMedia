"""What the evaluator scores (Candidate) and the context it scores against (EvalContext)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from harness.llm import LLM
from harness.models import Action


@dataclass
class Candidate:
    """One draft (or, in backtests, one real post) to evaluate. Text-only: no author reach or metrics."""

    id: str
    text: str
    kind: str = "post"  # post | reply | quote | mention | follow
    responding_to: str = ""  # target post text for replies / quotes
    target_key: str | None = None  # target post url/id (groups candidates for triage)
    action: Action | None = None

    @classmethod
    def from_action(cls, a: Action) -> "Candidate":
        ctx = a.context or {}
        return cls(id=a.id, text=a.text, kind=a.type,
                   responding_to=(ctx.get("text") or ctx.get("summary") or "")[:600],
                   target_key=a.target_url or ctx.get("url"), action=a)

    def render(self) -> str:
        if self.responding_to and self.kind in {"reply", "quote"}:
            return f"{self.kind.upper()} TO THIS POST:\n{self.responding_to}\n\n{self.kind.upper()}:\n{self.text}"
        return f"{self.kind.upper()}:\n{self.text}"


@dataclass
class EvalContext:
    llm: LLM  # base harness LLM; fast (Haiku) and judge tiers are derived from it
    mission: str
    lane: str = "post"
    decider: str | None = None  # None -> HARNESS_DECIDER -> claude
    rounds: int | None = None  # Swiss rounds; None -> ceil(log2 n) + 1
    # predicted_score = weighted mean of within-batch percentiles of: qualify's panel score (when the
    # action already has one), the tournament win prob and the rubric engagement total. Defaults from the
    # real-data backtest: panel+pairwise was best on both samples; adding the rubric lowered Spearman.
    w_panel: float = 0.5
    w_tournament: float = 0.5
    w_rubric: float = 0.0
    # Optional third signal: the no-LLM feature model (features.py), post lane only, HARNESS_EVAL_FEATURES=on.
    w_features: float = field(default_factory=lambda: 0.5 if _features_on() else 0.0)
    rubric: bool = True  # still run the rubric for guardrail flags and per-criterion justifications
    seed: int = 0
    triage: bool = True  # reply lane: attach a triage verdict per target post
    audience: str = field(default="")  # the mission's audience (triage / rubric context)
    pair_audience: str = "neutral"  # pairwise judge audience: neutral | mission | custom text
    pair_tier: str = "fast"  # pairwise judge tier on claude: fast (Haiku) | judge (configured model)
    # triage tier on claude. One batched call per ~10 posts, so the judge tier is cheap; on real dev-tool
    # posts Haiku (low effort) declined 9-10 of 10 targets, including priority peers' on-subject launches.
    triage_tier: str = "judge"

    def __post_init__(self):
        self.audience = self.audience or audience_from_mission(self.mission)

    @classmethod
    def of(cls, ctx: Any, lane: str | None = None, **kw) -> "EvalContext":
        """Build from a lanes.common.LaneContext (or anything with .llm and .mission)."""
        if isinstance(ctx, cls):
            return ctx
        return cls(llm=ctx.llm, mission=ctx.mission, lane=lane or getattr(ctx, "lane", "post"), **kw)


def _features_on() -> bool:
    return (os.getenv("HARNESS_EVAL_FEATURES") or "off").strip().lower() in {"on", "1", "true", "yes"}


def audience_from_mission(mission: str) -> str:
    """'Who it's for' + the 'Audience panel' bullets: who the pairwise judge imagines on X."""
    out, inside = [], False
    for line in mission.splitlines():
        low = line.lower()
        if "who it's for" in low or "who it is for" in low:
            out.append(re.sub(r"\*\*[^*]+\*\*\s*", "", line).strip())
        if "audience panel" in low:
            inside = True
            continue
        if inside:
            if re.match(r"\s*[-*]\s+", line):
                out.append(line.strip())
            elif line.strip():
                inside = False
    return "\n".join(out) or "Developers on X who follow AI coding tools."
