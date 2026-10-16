"""Pairwise accuracy and cluster-bootstrap intervals, standard library only.

A pair is (winner, loser, cluster). A scorer is right on a pair when it scores the winner higher;
a tie counts as half, so a constant scorer gets exactly 0.5. Intervals resample whole clusters
(threads or authors), because pairs that share a thread are not independent.
"""

import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Hashable, Iterable, Sequence

B_DEFAULT = 2000


def pair_credit(s_winner: float, s_loser: float) -> float:
    if s_winner > s_loser:
        return 1.0
    if s_winner == s_loser:
        return 0.5
    return 0.0


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float
    n_pairs: int
    n_clusters: int

    def to_dict(self, digits: int = 4) -> dict:
        return {"estimate": round(self.estimate, digits), "ci95": [round(self.low, digits), round(self.high, digits)],
                "pairs": self.n_pairs, "clusters": self.n_clusters}

    def __str__(self) -> str:
        return f"{self.estimate:.3f} [{self.low:.3f}, {self.high:.3f}] ({self.n_pairs} pairs, {self.n_clusters} clusters)"


def _cluster_sums(credits: Sequence[float], clusters: Sequence[Hashable]) -> dict:
    sums = defaultdict(lambda: [0.0, 0])
    for c, k in zip(credits, clusters):
        sums[k][0] += c
        sums[k][1] += 1
    return sums


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("no bootstrap values")
    i = q * (len(sorted_vals) - 1)
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def accuracy(credits: Sequence[float], clusters: Sequence[Hashable], b: int = B_DEFAULT, seed: int = 0) -> Interval:
    """Pair-weighted accuracy with a 95% percentile interval from resampling clusters."""
    if len(credits) != len(clusters):
        raise ValueError("credits and clusters differ in length")
    if not credits:
        raise ValueError("no pairs to score")
    sums = _cluster_sums(credits, clusters)
    keys = sorted(sums, key=str)
    est = sum(credits) / len(credits)
    rng = random.Random(seed)
    boots = []
    for _ in range(b):
        s = n = 0
        for _ in keys:
            cs, cn = sums[keys[rng.randrange(len(keys))]]
            s += cs
            n += cn
        boots.append(s / n)
    boots.sort()
    return Interval(est, _percentile(boots, 0.025), _percentile(boots, 0.975), len(credits), len(keys))


def paired_difference(credits_a: Sequence[float], credits_b: Sequence[float], clusters: Sequence[Hashable],
                      b: int = B_DEFAULT, seed: int = 0) -> Interval:
    """Accuracy of A minus accuracy of B on the same pairs, resampling clusters once per replicate."""
    if not (len(credits_a) == len(credits_b) == len(clusters)):
        raise ValueError("credits and clusters differ in length")
    diffs = [a - bb for a, bb in zip(credits_a, credits_b)]
    sums = _cluster_sums(diffs, clusters)
    keys = sorted(sums, key=str)
    est = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    boots = []
    for _ in range(b):
        s = n = 0
        for _ in keys:
            cs, cn = sums[keys[rng.randrange(len(keys))]]
            s += cs
            n += cn
        boots.append(s / n)
    boots.sort()
    return Interval(est, _percentile(boots, 0.025), _percentile(boots, 0.975), len(diffs), len(keys))


def credits_for(pairs: Iterable, score: Callable[[str], float]) -> list[float]:
    """`pairs` yields objects with .winner and .loser item IDs; `score` maps an item ID to a number."""
    return [pair_credit(score(p.winner), score(p.loser)) for p in pairs]
