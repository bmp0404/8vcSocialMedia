"""Metrics for scoring a predictor against real engagement. Pure functions, no I/O.

Ties get half credit everywhere a ranking is compared, so a predictor that gives every post the same
score lands exactly at chance instead of at whatever order the input happened to be in.
Functions return None when a statistic is undefined (too few items, or nothing to rank).
"""

import math
import random
from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence


def ranks(xs: Sequence[float]) -> list[float]:
    """1-based ranks; tied values share the mean of the ranks they span."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys):
        raise ValueError(f"length mismatch: {len(xs)} vs {len(ys)}")
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation of the ranks, which handles ties correctly."""
    return pearson(ranks(xs), ranks(ys))


def pairwise_accuracy(
    scores: Sequence[float], targets: Sequence[float], groups: Sequence[Hashable] | None = None
) -> tuple[float | None, int]:
    """Share of pairs the scores order the same way as the targets, counting score ties as half.

    Only pairs inside the same group count, and pairs with equal targets are skipped. With one group
    and 0/1 targets this is the ROC AUC. Returns (accuracy, number of pairs compared).
    """
    if not (len(scores) == len(targets) == (len(groups) if groups is not None else len(scores))):
        raise ValueError("scores, targets and groups must be the same length")
    members: dict[Hashable, list[int]] = defaultdict(list)
    for i in range(len(scores)):
        members[groups[i] if groups is not None else 0].append(i)
    credit, pairs = 0.0, 0
    for idx in members.values():
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                i, j = idx[a], idx[b]
                if targets[i] == targets[j]:
                    continue
                pairs += 1
                hi, lo = (i, j) if targets[i] > targets[j] else (j, i)
                if scores[hi] > scores[lo]:
                    credit += 1.0
                elif scores[hi] == scores[lo]:
                    credit += 0.5
    return (credit / pairs if pairs else None), pairs


def precision_at_k(scores: Sequence[float], labels: Sequence[bool], k: int) -> float | None:
    """Share of the k highest-scored items that are positive.

    When the k-th place falls inside a group of tied scores, the result is the expected precision
    over a random order of that group, not whatever order the input had.
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must be the same length")
    if k <= 0 or k > len(scores):
        return None
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    cutoff = scores[order[k - 1]]
    above = [i for i in order if scores[i] > cutoff]
    tied = [i for i in order if scores[i] == cutoff]
    hits = sum(labels[i] for i in above)
    hits += (k - len(above)) * sum(labels[i] for i in tied) / len(tied)
    return hits / k


def top_pick_beats_median(
    scores: Sequence[float], targets: Sequence[float], groups: Sequence[Hashable]
) -> tuple[float | None, int]:
    """For each group of 2+ items, does the top-scored item's target beat the group's median target?

    This mirrors how qualify is used: pick the best of one account's candidates. Strictly above the
    median counts 1, equal counts 0.5, and a tie for top score averages over the tied items.
    Returns (rate, number of groups).
    """
    members: dict[Hashable, list[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        members[g].append(i)
    total, n = 0.0, 0
    for idx in members.values():
        if len(idx) < 2:
            continue
        ts = sorted(targets[i] for i in idx)
        m = len(ts)
        median = ts[m // 2] if m % 2 else (ts[m // 2 - 1] + ts[m // 2]) / 2
        best = max(scores[i] for i in idx)
        tops = [i for i in idx if scores[i] == best]
        total += sum(1.0 if targets[i] > median else 0.5 if targets[i] == median else 0.0 for i in tops) / len(tops)
        n += 1
    return (total / n if n else None), n


def brier(probs: Sequence[float], outcomes: Sequence[bool]) -> float | None:
    if len(probs) != len(outcomes):
        raise ValueError("probs and outcomes must be the same length")
    if not probs:
        return None
    return sum((p - float(o)) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def reliability(probs: Sequence[float], outcomes: Sequence[bool], bins: int = 5) -> list[dict]:
    """Equal-width bins over [0, 1]. A probability of exactly 1.0 goes in the last bin."""
    if len(probs) != len(outcomes):
        raise ValueError("probs and outcomes must be the same length")
    rows = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(probs) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if not idx:
            rows.append({"lo": lo, "hi": hi, "n": 0, "mean_pred": None, "observed": None})
            continue
        rows.append({
            "lo": lo, "hi": hi, "n": len(idx),
            "mean_pred": sum(probs[i] for i in idx) / len(idx),
            "observed": sum(outcomes[i] for i in idx) / len(idx),
        })
    return rows


def ece(probs: Sequence[float], outcomes: Sequence[bool], bins: int = 5) -> float | None:
    """Expected calibration error: bin-size-weighted mean of |mean prediction - observed rate|."""
    if not probs:
        return None
    return sum(r["n"] / len(probs) * abs(r["mean_pred"] - r["observed"])
               for r in reliability(probs, outcomes, bins) if r["n"])


def wilson(successes: float, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def cluster_bootstrap(
    stat: Callable[[list[int], list[int]], float | None],
    groups: Sequence[Hashable],
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    """Percentile interval for stat(indices, draw_ids), resampling whole groups with replacement.

    Posts from one account share an audience, so they aren't independent draws; resampling
    accounts rather than posts keeps one big account from making the interval look narrower than it is.
    draw_ids[j] numbers the draw that put indices[j] in the sample, so an account drawn twice counts
    as two groups and a within-group statistic doesn't pair a post with its own copy.
    Draws where the statistic is undefined are dropped. Returns None if fewer than half are defined.
    """
    members: dict[Hashable, list[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        members[g].append(i)
    keys = list(members)
    if not keys:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(n_boot):
        idx, draw_ids = [], []
        for d, g in enumerate(rng.choices(keys, k=len(keys))):
            idx += members[g]
            draw_ids += [d] * len(members[g])
        v = stat(idx, draw_ids)
        if v is not None:
            values.append(v)
    if len(values) < n_boot / 2:
        return None
    values.sort()
    lo = values[int(alpha / 2 * (len(values) - 1))]
    hi = values[int(math.ceil((1 - alpha / 2) * (len(values) - 1)))]
    return lo, hi
