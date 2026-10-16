"""Checkpoints and labels: when to read a post's counts, and whether they beat the account's norm.

A snapshot taken at age `a` hours belongs to checkpoint T when T <= a < 2T, so +1h covers 1-2 hours,
+6h 6-12, +24h 24-48. The last checkpoint, +48h, has no upper bound: the context worker treats a
post as settled at 48 hours (`label_mature`, data/context/DESIGN.md on PR #18). The same rule places
both our post's snapshot and every baseline snapshot, so a post is only compared with posts of
about its own age. The factor of 2 is a guess with no evidence behind it.

The label follows memory's within-account definition (AUTHOR_PRIOR_DEFINITION in
harness/learning/dataset.py on PR #21): success means more engagement than the median of the
account's own earlier posts, failure means at or below it. Here the earlier posts are read at the
same checkpoint, and replies are compared only with replies, posts and quotes with each other.
"""

import statistics
from dataclasses import dataclass
from datetime import datetime

CHECKPOINTS = (1, 6, 24, 48)  # hours after posting
WINDOW_FACTOR = 2  # uncalibrated
BASELINE_MIN = 10  # fewer earlier posts at the checkpoint than this and the label is insufficient_baseline
BASELINE_MAX = 30  # only the most recent earlier posts, so the norm follows the account as it grows. Uncalibrated.

SUCCESS, FAILURE, INSUFFICIENT = "success", "failure", "insufficient_baseline"
FINAL = {SUCCESS, FAILURE}
# Thread continuations are left out of every baseline, as the context worker does: a thread's
# second post isn't a failed post.
GROUP = {"original": "original", "reply": "reply", "self_reply": None}


def checkpoint_for(age_hours: float) -> int | None:
    """The checkpoint whose window holds `age_hours`, or None between windows and before +1h."""
    for t in reversed(CHECKPOINTS):
        if age_hours >= t:
            return t if t == CHECKPOINTS[-1] or age_hours < t * WINDOW_FACTOR else None
    return None


def checkpoint_name(t: int) -> str:
    return f"+{t}h"


def age_hours(created_at: datetime, at: datetime) -> float:
    return (at - created_at).total_seconds() / 3600


@dataclass(frozen=True)
class BaselinePoint:
    status_id: str
    created_at: datetime
    kind: str
    engagement: int


@dataclass(frozen=True)
class Label:
    outcome: str
    engagement: int
    baseline_n: int
    baseline_median: float | None = None
    baseline_p10: float | None = None
    baseline_p90: float | None = None


def baseline(points: list[BaselinePoint], status_id: str, created_at: datetime, kind: str) -> list[int]:
    """Engagement of the account's most recent earlier posts of the same group, at one checkpoint."""
    group = GROUP[kind]
    if group is None:
        return []
    earlier = [p for p in points
               if GROUP.get(p.kind) == group and p.created_at < created_at and p.status_id != status_id]
    earlier.sort(key=lambda p: p.created_at, reverse=True)
    return [p.engagement for p in earlier[:BASELINE_MAX]]


def label(engagement: int, values: list[int]) -> Label:
    if len(values) < BASELINE_MIN:
        return Label(INSUFFICIENT, engagement, len(values))
    median = statistics.median(values)
    deciles = statistics.quantiles(values, n=10, method="inclusive")
    return Label(SUCCESS if engagement > median else FAILURE, engagement, len(values), float(median),
                 round(deciles[0], 2), round(deciles[-1], 2))
