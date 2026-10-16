"""Krippendorff's alpha for nominal, ordinal and interval data.

Follows Krippendorff, "Computing Krippendorff's Alpha-Reliability" (2011): build the coincidence
matrix from every unit with two or more values, then compare observed and expected disagreement
under the chosen distance. The test pins the paper's worked example.
"""

from collections import Counter, defaultdict
from typing import Hashable, Iterable, Sequence


def alpha(units: Iterable[Sequence[Hashable]], level: str = "nominal", order: Sequence[Hashable] | None = None) -> float:
    """`units` yields the values the coders gave one unit (missing values left out).

    `level` is "nominal", "ordinal" or "interval". Ordinal needs `order`, the categories from lowest
    to highest; interval values must be numbers. Units with fewer than two values carry no pairs and
    are skipped. Raises ValueError when there is nothing to compare or no variation at all.
    """
    if level not in ("nominal", "ordinal", "interval"):
        raise ValueError(f"unknown level {level!r}")
    if level == "ordinal" and order is None:
        raise ValueError("ordinal alpha needs the category order")
    o = defaultdict(float)
    for values in units:
        values = list(values)
        m = len(values)
        if m < 2:
            continue
        counts = Counter(values)
        for c, nc in counts.items():
            for k, nk in counts.items():
                pairs = nc * (nk - 1) if c == k else nc * nk
                if pairs:
                    o[(c, k)] += pairs / (m - 1)
    if not o:
        raise ValueError("no unit has two or more values")
    n_c = Counter()
    for (c, _), v in o.items():
        n_c[c] += v
    n = sum(n_c.values())
    cats = list(n_c)
    if level == "ordinal":
        unknown = set(cats) - set(order)
        if unknown:
            raise ValueError(f"values {sorted(map(str, unknown))} are not in the ordinal order")
        rank = {v: i for i, v in enumerate(order)}
        ordered = sorted(cats, key=rank.__getitem__)
        pos = {v: i for i, v in enumerate(ordered)}

        def delta2(c, k):
            if c == k:
                return 0.0
            lo, hi = sorted((pos[c], pos[k]))
            between = sum(n_c[ordered[g]] for g in range(lo, hi + 1))
            return (between - (n_c[c] + n_c[k]) / 2) ** 2
    elif level == "interval":
        def delta2(c, k):
            return float(c - k) ** 2
    else:
        def delta2(c, k):
            return 0.0 if c == k else 1.0

    observed = sum(v * delta2(c, k) for (c, k), v in o.items())
    expected = sum(n_c[c] * n_c[k] * delta2(c, k) for c in cats for k in cats)
    if expected == 0:
        raise ValueError("every value is the same; alpha is undefined")
    return 1.0 - (n - 1) * observed / expected


def percent_agreement(units: Iterable[Sequence[Hashable]]) -> float:
    """Share of two-value units where both values match (for two annotators)."""
    pairs = [tuple(u) for u in units if len(u) == 2]
    if not pairs:
        raise ValueError("no unit has exactly two values")
    return sum(a == b for a, b in pairs) / len(pairs)
