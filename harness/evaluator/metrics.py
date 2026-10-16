"""Agreement metrics between a method's scores and actual engagement (stdlib only)."""

from __future__ import annotations

import math
import random
from itertools import combinations


def ranks(xs: list[float]) -> list[float]:
    """1-based ranks, ties get their average rank."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    k = 0
    while k < len(order):
        j = k
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[k]]:
            j += 1
        for m in range(k, j + 1):
            out[order[m]] = (k + j) / 2 + 1
        k = j + 1
    return out


def pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx == 0 or sy == 0:
        return float("nan")
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def spearman(x: list[float], y: list[float]) -> float:
    return pearson(ranks(x), ranks(y))


def spearman_ci(x: list[float], y: list[float], n_boot: int = 1000, seed: int = 0, alpha: float = 0.10) -> tuple[float, float]:
    """Bootstrap (1 - alpha) interval for Spearman."""
    rng = random.Random(seed)
    n, vals = len(x), []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        v = spearman([x[i] for i in idx], [y[i] for i in idx])
        if not math.isnan(v):
            vals.append(v)
    if not vals:
        return float("nan"), float("nan")
    vals.sort()
    return vals[int(alpha / 2 * len(vals))], vals[int((1 - alpha / 2) * len(vals)) - 1]


def precision_at_k(pred: list[float], actual: list[float], k: int | None = None) -> float:
    """Share of the method's top-k that is in the actual top-k (k defaults to the top quartile)."""
    n = len(pred)
    k = k or max(1, n // 4)
    top_pred = set(sorted(range(n), key=lambda i: -pred[i])[:k])
    top_act = set(sorted(range(n), key=lambda i: -actual[i])[:k])
    return len(top_pred & top_act) / k


def pairwise_accuracy(pred: list[float], actual: list[float], groups: list[str] | None = None) -> float:
    """Over pairs with different actual values (within the same group if given): share the method orders
    correctly; a tie in the method counts half."""
    good = tot = 0.0
    for i, j in combinations(range(len(pred)), 2):
        if groups and groups[i] != groups[j]:
            continue
        if actual[i] == actual[j]:
            continue
        tot += 1
        d = (pred[i] - pred[j]) * (actual[i] - actual[j])
        good += 1.0 if d > 0 else 0.5 if pred[i] == pred[j] else 0.0
    return good / tot if tot else float("nan")


def auc(pred: list[float], positive: list[bool], groups: list[str] | None = None) -> float:
    """P(score of a positive > score of a negative), ties half; optionally only within groups."""
    good = tot = 0.0
    for i, j in combinations(range(len(pred)), 2):
        if positive[i] == positive[j] or (groups and groups[i] != groups[j]):
            continue
        p, q = (i, j) if positive[i] else (j, i)
        tot += 1
        good += 1.0 if pred[p] > pred[q] else 0.5 if pred[p] == pred[q] else 0.0
    return good / tot if tot else float("nan")


def zscores(xs: list[float]) -> list[float]:
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / n) or 1.0
    return [(x - m) / sd for x in xs]


def percentile_within(values: list[float], groups: list[str]) -> list[float]:
    """Each value's percentile rank (0-1, ties averaged) within its group."""
    by = {}
    for i, g in enumerate(groups):
        by.setdefault(g, []).append(i)
    out = [0.5] * len(values)
    for idx in by.values():
        r = ranks([values[i] for i in idx])
        n = len(idx)
        for i, rv in zip(idx, r):
            out[i] = (rv - 1) / (n - 1) if n > 1 else 0.5
    return out


def wilson(k: float, n: int, z: float = 1.645) -> tuple[float, float]:
    """90% Wilson interval for a proportion k/n."""
    if n == 0:
        return float("nan"), float("nan")
    ph = k / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    m = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d
