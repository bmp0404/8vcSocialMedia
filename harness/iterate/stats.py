"""Promotion statistics. Pure functions with no I/O, so each has one correct answer and a strict test.

The gate, in order:
1. Champion and challenger are scored on the same items. A paired cluster bootstrap resamples whole
   clusters (accounts or threads) once per draw and recomputes both metrics on that draw, giving a
   distribution of the improvement (challenger minus champion, sign-flipped when lower is better).
2. A one-sided bootstrap p-value: the share of draws where the improvement is <= 0, with the +1
   correction so it is never exactly 0.
3. Benjamini-Hochberg at level q across every comparison in the run, losers included.
4. For each comparison BH rejects, a one-sided lower confidence bound at level 1 - R*q/m, where R
   is the number of rejections and m the family size. This is the false-coverage-rate adjustment
   of Benjamini and Yekutieli (2005): across the comparisons a run selects, the expected share of
   bounds that miss the true improvement stays at or below q, provided each bootstrap bound covers at
   its stated level. A percentile cluster bootstrap with 20 to 30 clusters tends to cover a little
   less than stated, which is one more reason for step 5. The
   level never drops below 97.5% (alpha at most 0.025): that is the lower end of the paired 95%
   interval the eval worker requires to exclude zero, and with a family of one, R*q/m alone would
   allow a 90% bound.
5. Promote only if that lower bound exceeds the noise floor: the absolute metric difference
   between the champion and a repeat of the champion on the same items.

Step 5 is conservative on purpose. The bootstrap already reflects per-item noise; the repeat adds
run-level noise (lineup composition, model sampling) that a single run can hide. With 20 to 30
clusters, false promotions cost more than missed ones.
"""

import math
import random
from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass

Stat = Callable[[list[int], list[int]], float | None]
MAX_ALPHA = 0.025  # see step 4


def cluster_draws(clusters: Sequence[Hashable], n_boot: int, seed: int) -> list[tuple[list[int], list[int]]]:
    """Resampled index sets. Each draw picks len(unique clusters) clusters with replacement and returns
    (indices, draw_ids); draw_ids number the pick, so a cluster drawn twice counts as two clusters."""
    members: dict[Hashable, list[int]] = defaultdict(list)
    for i, c in enumerate(clusters):
        members[c].append(i)
    keys = sorted(members, key=repr)  # a fixed order, so the same seed gives the same draws
    if not keys:
        raise ValueError("no clusters to resample")
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        idx: list[int] = []
        ids: list[int] = []
        for d, k in enumerate(rng.choices(keys, k=len(keys))):
            idx += members[k]
            ids += [d] * len(members[k])
        draws.append((idx, ids))
    return draws


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default) of an already sorted, non-empty sequence."""
    if not sorted_values:
        raise ValueError("quantile of an empty sequence")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"quantile level {q} outside [0, 1]")
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


@dataclass(frozen=True)
class Paired:
    """Result of comparing a challenger with the champion on the same items."""

    champion: float
    challenger: float
    delta: float  # challenger - champion, in the metric's own units
    improvement: float  # delta, sign-flipped when lower is better
    draws: tuple[float, ...]  # sorted bootstrap improvements
    p_one_sided: float  # P(improvement <= 0) under the bootstrap, +1 corrected

    def lower_bound(self, alpha: float) -> float:
        """One-sided lower confidence bound on the improvement at level 1 - alpha."""
        return quantile(self.draws, alpha)

    def interval(self, level: float = 0.95) -> tuple[float, float]:
        """Two-sided percentile interval on the improvement."""
        a = (1 - level) / 2
        return quantile(self.draws, a), quantile(self.draws, 1 - a)


def paired_bootstrap(stat_a: Stat, stat_b: Stat, clusters: Sequence[Hashable], *, higher_is_better: bool,
                     n_boot: int = 2000, seed: int = 0) -> Paired:
    """Compare metric b (challenger) with metric a (champion) on the same items.

    `stat_x(indices, draw_ids)` computes the metric on the items at `indices`; `draw_ids` lets a
    within-cluster metric (a within-account AUC) treat a cluster drawn twice as two clusters.
    Draws where either metric is undefined are dropped; fewer than half defined raises."""
    everything = list(range(len(clusters)))
    a0, b0 = stat_a(everything, list(clusters)), stat_b(everything, list(clusters))
    if a0 is None or b0 is None:
        raise ValueError("metric undefined on the full sample")
    sign = 1.0 if higher_is_better else -1.0
    values = []
    for idx, ids in cluster_draws(clusters, n_boot, seed):
        a, b = stat_a(idx, ids), stat_b(idx, ids)
        if a is not None and b is not None:
            values.append(sign * (b - a))
    if len(values) < n_boot / 2:
        raise ValueError(f"metric undefined on {n_boot - len(values)} of {n_boot} bootstrap draws")
    values.sort()
    p = (1 + sum(v <= 0 for v in values)) / (1 + len(values))
    return Paired(a0, b0, b0 - a0, sign * (b0 - a0), tuple(values), p)


def marginal_interval(stat: Stat, clusters: Sequence[Hashable], n_boot: int = 2000, seed: int = 0,
                      level: float = 0.95) -> tuple[float, float] | None:
    """Percentile interval for one arm's metric, resampling clusters. For display only; never compare
    two arms by whether their marginal intervals overlap."""
    values = sorted(v for idx, ids in cluster_draws(clusters, n_boot, seed) if (v := stat(idx, ids)) is not None)
    if len(values) < n_boot / 2:
        return None
    a = (1 - level) / 2
    return quantile(values, a), quantile(values, 1 - a)


def benjamini_hochberg(pvalues: Sequence[float], q: float) -> tuple[list[float], list[bool]]:
    """Adjusted p-values (step-up, monotone, capped at 1) and which hypotheses are rejected at level q."""
    if not 0 < q < 1:
        raise ValueError(f"q must be in (0, 1), got {q}")
    m = len(pvalues)
    if m == 0:
        return [], []
    if any(not 0 <= p <= 1 for p in pvalues):
        raise ValueError(f"p-values must lie in [0, 1]: {list(pvalues)}")
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)  # starts at max(p) <= 1, so it never exceeds 1
        adjusted[i] = running
    # Tolerance for float rounding at exact boundaries: 0.05 * 6 / 3 is 0.10000000000000002.
    return adjusted, [adj <= q * (1 + 1e-9) for adj in adjusted]


def non_inferior(paired: Paired, margin: float, level: float = 0.95) -> bool:
    """True when the two-sided interval's lower end on the improvement is above -margin: the challenger
    is shown to be no more than `margin` worse. Guardrails use this, so an unsafe challenger can't
    pass just because a small sample couldn't prove it worse."""
    if margin < 0:
        raise ValueError(f"margin must be non-negative, got {margin}")
    return paired.interval(level)[0] > -margin


@dataclass(frozen=True)
class Decision:
    status: str  # promoted | rejected | inconclusive
    reason: str
    interval_level: float  # the level of the bound the decision used
    bound: float  # the lower bound compared with the noise floor (or the 95% upper bound for "rejected")


def decide(paired: Paired, *, rejected_by_bh: bool, n_rejected: int, family_size: int, q: float,
           noise_floor: float) -> Decision:
    """Apply steps 4 and 5 of the module docstring to one comparison."""
    if noise_floor < 0 or math.isnan(noise_floor):
        raise ValueError(f"noise floor must be a non-negative number, got {noise_floor}")
    if rejected_by_bh:
        if n_rejected < 1 or family_size < n_rejected:
            raise ValueError(f"inconsistent multiplicity: {n_rejected} rejected of {family_size}")
        alpha = min(n_rejected * q / family_size, MAX_ALPHA)
        bound = paired.lower_bound(alpha)
        if bound > noise_floor:
            return Decision("promoted", f"improvement lower bound {bound:+.3f} at {1 - alpha:.3f} clears the noise "
                                        f"floor {noise_floor:.3f}", 1 - alpha, bound)
        return Decision("inconclusive", f"significant after BH, but the lower bound {bound:+.3f} does not clear the "
                                        f"noise floor {noise_floor:.3f}", 1 - alpha, bound)
    hi = paired.interval(0.95)[1]
    if hi < 0:
        return Decision("rejected", f"challenger is worse: the 95% interval on the improvement ends at {hi:+.3f}",
                        0.95, hi)
    lo = paired.interval(0.95)[0]
    return Decision("inconclusive", f"not significant after BH; 95% interval on the improvement [{lo:+.3f}, {hi:+.3f}]",
                    0.95, lo)


def mean_stat(outcomes: Sequence[float]) -> Stat:
    """A Stat for metrics that are a plain mean of per-item outcomes (a detection rate)."""
    def stat(idx: list[int], _ids: list[int]) -> float | None:
        return sum(outcomes[i] for i in idx) / len(idx) if idx else None
    return stat


def within_cluster_auc_stat(scores: Sequence[float], labels: Sequence[bool]) -> Stat:
    """A Stat for the within-account AUC: the share of (positive, negative) pairs inside the same draw
    group where the positive scores higher, ties counting half. None when no group has both labels."""
    def stat(idx: list[int], ids: list[int]) -> float | None:
        groups: dict[int, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
        for i, g in zip(idx, ids):
            groups[g][0 if labels[i] else 1].append(scores[i])
        credit, pairs = 0.0, 0
        for pos, neg in groups.values():
            for p in pos:
                for n in neg:
                    pairs += 1
                    credit += 1.0 if p > n else 0.5 if p == n else 0.0
        return credit / pairs if pairs else None
    return stat
