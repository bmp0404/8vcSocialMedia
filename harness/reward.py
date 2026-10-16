"""Turn human reviews and real X metrics into one 0-1 reward per action.

- Real outcome: weighted engagement, ranked against every other recorded outcome (percentile),
  so the bar rises as the account grows. Reposts and quotes weigh most: that's what spreads.
- Human review: a faster, weaker signal. Approved = 1, approved-after-edit = 0.7, killed = 0.
"""

from dataclasses import dataclass

ENGAGEMENT_WEIGHTS = {
    "likes": 1, "replies": 3, "bookmarks": 3, "reposts": 5, "quotes": 6,
    "profile_clicks": 2, "link_clicks": 4, "follows": 10,
}
OUTCOME_WEIGHT = 3.0  # evidence weight of one real post vs. one human verdict
REVIEW_WEIGHT = 1.0
REVIEW_REWARD = {"approved": 1.0, "approved_edited": 0.7, "killed": 0.0}


@dataclass
class Reward:
    value: float
    weight: float
    source: str  # "outcome" | "review"


def engagement(metrics: dict) -> float:
    return sum(w * float(metrics.get(k, 0) or 0) for k, w in ENGAGEMENT_WEIGHTS.items())


def _percentile(x: float, population: list[float]) -> float:
    if len(population) <= 1:
        return 0.5
    below = sum(p < x for p in population)
    equal = sum(p == x for p in population) - 1  # exclude itself
    return (below + 0.5 * equal) / (len(population) - 1)


def score(rows: list[dict]) -> dict[str, Reward]:
    """rows from memory.scored_actions() -> {action_id: Reward}. Real outcomes override reviews."""
    raws = [engagement(r["metrics"]) for r in rows if r["metrics"]]
    rewards: dict[str, Reward] = {}
    for r in rows:
        aid = r["action"]["id"]
        if r["metrics"]:
            rewards[aid] = Reward(_percentile(engagement(r["metrics"]), raws), OUTCOME_WEIGHT, "outcome")
        elif r["review"] in ("approved", "killed"):
            key = "approved_edited" if r["review"] == "approved" and r["edited_text"] else r["review"]
            rewards[aid] = Reward(REVIEW_REWARD[key], REVIEW_WEIGHT, "review")
    return rewards
