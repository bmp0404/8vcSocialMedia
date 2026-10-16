"""The held-out test a reflection must pass before it becomes a lesson.

The dataset's labels are raw engagement cutoffs, so they mostly track account size: account identity
alone separates viral from Deadweight at AUC 0.906 (8vc-context/research/memory.md). A test that pooled
posts across accounts would reward any trait big accounts share. So every held-out post sits in a
stratum (an account, split into launch and non-launch posts), and the test is the Cochran-Mantel-
Haenszel test: does the predicate pick out the claimed outcome within strata, combined across them?
It uses every held-out post, not one pair per account, which a simulation in the same report showed
was too weak to keep real effects.

Each run tests several hypotheses, so `benjamini_hochberg` controls the false discovery rate across
all of them. A retest that misses significance is inconclusive, not a contradiction; a contradiction
needs the opposite direction to be significant.
"""

from dataclasses import dataclass
from math import erfc, sqrt


@dataclass(frozen=True)
class Table:
    """One stratum's 2x2 counts: predicate true/false by target outcome/other outcome."""

    stratum: str
    true_target: int
    true_other: int
    false_target: int
    false_other: int

    @property
    def n(self) -> int:
        return self.true_target + self.true_other + self.false_target + self.false_other


def mh_sums(tables: list[Table]) -> tuple[float, float]:
    """Observed minus expected true_target count summed over strata, and its variance under no association.

    Strata with fewer than two posts, or where the predicate or the outcome doesn't vary, add nothing.
    """
    diff = var = 0.0
    for t in tables:
        n = t.n
        if n < 2:
            continue
        row_true, row_false = t.true_target + t.true_other, t.false_target + t.false_other
        col_target, col_other = t.true_target + t.false_target, t.true_other + t.false_other
        diff += t.true_target - row_true * col_target / n
        var += row_true * row_false * col_target * col_other / (n * n * (n - 1))
    return diff, var


def upper_tail(z: float) -> float:
    """P(Z >= z) for a standard normal Z."""
    return 0.5 * erfc(z / sqrt(2))


def benjamini_hochberg(p_values: list[float], q: float = 0.1) -> list[bool]:
    """Which hypotheses to accept at false discovery rate q, in input order."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    cutoff = -1
    for rank, i in enumerate(order, start=1):
        if p_values[i] <= rank / m * q:
            cutoff = rank
    accepted = [False] * m
    for rank, i in enumerate(order, start=1):
        if rank <= cutoff:
            accepted[i] = True
    return accepted


def heldout_test(tables: list[Table], *, min_posts_with_trait: int = 5) -> dict:
    """Effect and one-sided p-values for a predicate across strata.

    `p_value` tests "the predicate picks out the target outcome"; `p_opposite` tests the reverse, which is
    what a contradiction needs. `effect` is the share of target-outcome posts with the trait minus the share
    of other-outcome posts with it, combined across strata (Mantel-Haenszel weights). The `*_with_trait`
    counts are pooled, for display. `enough_data` is False when fewer than
    `min_posts_with_trait` held-out posts have the trait, since no p-value means much then.
    """
    diff, var = mh_sums(tables)
    sd = sqrt(var)
    target = sum(t.true_target + t.false_target for t in tables)
    other = sum(t.true_other + t.false_other for t in tables)
    with_trait_target = sum(t.true_target for t in tables)
    with_trait_other = sum(t.true_other for t in tables)
    # Mantel-Haenszel risk difference: per-stratum (share of targets with the trait - share of others with
    # it), weighted by T*O/N. Its numerator equals `diff`, so its sign always agrees with the test.
    weight = sum((t.true_target + t.false_target) * (t.true_other + t.false_other) / t.n for t in tables if t.n >= 2)
    effect = diff / weight if weight else 0.0
    return {
        "strata": sum(1 for t in tables if t.n >= 2),
        "posts": target + other,
        "target_with_trait": with_trait_target,
        "target_posts": target,
        "other_with_trait": with_trait_other,
        "other_posts": other,
        "effect": round(effect, 3),
        "z": round(diff / sd, 3) if var else 0.0,
        # Continuity-corrected one-sided tails, one per direction.
        "p_value": round(upper_tail((diff - 0.5) / sd), 4) if var else 1.0,
        "p_opposite": round(upper_tail((-diff - 0.5) / sd), 4) if var else 1.0,
        "enough_data": with_trait_target + with_trait_other >= min_posts_with_trait,
        "test": "one-sided Cochran-Mantel-Haenszel across account strata, continuity-corrected",
    }


def verdicts(results: list[dict], q: float = 0.1) -> list[str]:
    """"supported", "contradicted" or "inconclusive" per result, with Benjamini-Hochberg across the run.

    Supported: enough data, positive effect, accepted by BH on `p_value`. Contradicted: enough data and
    accepted by BH on `p_opposite`. Anything else is inconclusive and should change nothing.
    """
    up = benjamini_hochberg([r["p_value"] for r in results], q)
    down = benjamini_hochberg([r["p_opposite"] for r in results], q)
    out = []
    for r, u, d in zip(results, up, down):
        if r["enough_data"] and u and r["effect"] > 0:
            out.append("supported")
        elif r["enough_data"] and d and r["effect"] < 0:
            out.append("contradicted")
        else:
            out.append("inconclusive")
    return out
