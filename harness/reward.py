"""Turn human reviews and real outcomes into one 0-1 reward per action.

- Real outcome: the action type's success metrics (see actions/catalog.py), weighted and ranked against
  every other recorded outcome measured the same way (percentile), so the bar rises as the account grows
  and a DM reply is never compared with post likes. On X, reposts and quotes weigh most: that's what spreads.
- Human review: a faster, weaker signal. Approved = 1, approved-after-edit = 0.7, killed = 0.
"""

from collections import defaultdict
from dataclasses import dataclass

from harness.actions import BY_TYPE
from harness.actions.catalog import X_METRICS

ENGAGEMENT_WEIGHTS = X_METRICS
OUTCOME_WEIGHT = 3.0  # evidence weight of one real post vs. one human verdict
REVIEW_WEIGHT = 1.0
REVIEW_REWARD = {"approved": 1.0, "approved_edited": 0.7, "killed": 0.0}


@dataclass
class Reward:
    value: float
    weight: float
    source: str  # "outcome" | "review"


def weights_for(action: dict) -> dict:
    spec = BY_TYPE.get(action.get("type"))
    return spec.metrics if spec and spec.metrics else ENGAGEMENT_WEIGHTS


def engagement(metrics: dict, weights: dict = ENGAGEMENT_WEIGHTS) -> float:
    return sum(w * float(metrics.get(k, 0) or 0) for k, w in weights.items())


def _percentile(x: float, population: list[float]) -> float:
    if len(population) <= 1:
        return 0.5
    below = sum(p < x for p in population)
    equal = sum(p == x for p in population) - 1  # exclude itself
    return (below + 0.5 * equal) / (len(population) - 1)


def score(rows: list[dict]) -> dict[str, Reward]:
    """rows from memory.scored_actions() -> {action_id: Reward}. Real outcomes override reviews."""
    group = lambda r: frozenset(weights_for(r["action"]).items())
    pools: dict[frozenset, list[float]] = defaultdict(list)
    for r in rows:
        if r["metrics"]:
            pools[group(r)].append(engagement(r["metrics"], weights_for(r["action"])))
    rewards: dict[str, Reward] = {}
    for r in rows:
        aid = r["action"]["id"]
        if r["metrics"]:
            raw = engagement(r["metrics"], weights_for(r["action"]))
            rewards[aid] = Reward(_percentile(raw, pools[group(r)]), OUTCOME_WEIGHT, "outcome")
        elif r["review"] in ("approved", "killed"):
            key = "approved_edited" if r["review"] == "approved" and r["edited_text"] else r["review"]
            rewards[aid] = Reward(REVIEW_REWARD[key], REVIEW_WEIGHT, "review")
    return rewards
