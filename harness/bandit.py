"""Thompson-sampling bandit over mind variants: better-performing prompts get more turns."""

import random
from dataclasses import dataclass

from harness.reward import Reward


@dataclass
class ArmStats:
    alpha: float = 0.0  # weighted successes
    beta: float = 0.0  # weighted failures
    n: int = 0  # scored actions

    @property
    def mean(self) -> float:
        return (1 + self.alpha) / (2 + self.alpha + self.beta)

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(1 + self.alpha, 1 + self.beta)


def arm_stats(rows: list[dict], rewards: dict[str, Reward]) -> dict[str, ArmStats]:
    stats: dict[str, ArmStats] = {}
    for r in rows:
        a = r["action"]
        rw = rewards.get(a["id"])
        if not rw or not a.get("variant"):
            continue
        s = stats.setdefault(a["variant"], ArmStats())
        s.alpha += rw.value * rw.weight
        s.beta += (1 - rw.value) * rw.weight
        s.n += 1
    return stats


def choose(variant_ids: list[str], stats: dict[str, ArmStats], k: int, rng: random.Random | None = None) -> list[str]:
    """Pick k variants to draft this run. Untested variants sample from Beta(1,1), so they get explored."""
    rng = rng or random.Random()
    draws = {v: stats.get(v, ArmStats()).sample(rng) for v in variant_ids}
    return sorted(variant_ids, key=draws.get, reverse=True)[:k]
