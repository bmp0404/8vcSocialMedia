import math

import pytest

from harness.learning.stats import Table, benjamini_hochberg, heldout_test, mh_sums, upper_tail, verdicts


def test_single_stratum_matches_hand_computation():
    # E[a] = 4*4/8 = 2, Var = 4*4*4*4 / (8*8*7) = 4/7, corrected z = (3 - 2 - 0.5) / sqrt(4/7)
    diff, var = mh_sums([Table("a", 3, 1, 1, 3)])
    assert diff == pytest.approx(1.0)
    assert var == pytest.approx(4 / 7)
    r = heldout_test([Table("a", 3, 1, 1, 3)])
    assert r["p_value"] == round(upper_tail(0.5 / math.sqrt(4 / 7)), 4) == 0.2542
    assert r["effect"] == 0.5
    assert r["enough_data"] is False  # only 4 posts carry the trait, under the default of 5


def test_strata_are_not_pooled():
    # Simpson's paradox. Pooled, the trait is on 51 of 90 targets vs 20 of 82 others, so it looks predictive.
    # Within each stratum it is rarer on targets (0.625 vs 0.83, and 0.10 vs 0.14), so the stratified test
    # must point the other way.
    tables = [Table("big", 50, 10, 30, 2), Table("small", 1, 10, 9, 60)]
    pooled = (50 + 1) / (80 + 10) - (10 + 10) / (12 + 70)
    assert pooled > 0.3
    r = heldout_test(tables)
    assert r["z"] < 0 and r["effect"] < 0
    assert r["p_value"] > 0.5
    assert r["p_opposite"] < r["p_value"]


def test_strata_without_variation_add_nothing():
    assert mh_sums([Table("x", 5, 0, 0, 0), Table("y", 1, 0, 0, 0)]) == (0.0, 0.0)
    assert heldout_test([Table("x", 5, 0, 0, 0)])["p_value"] == 1.0


def test_benjamini_hochberg_known_example():
    # m=4, q=0.1: thresholds 0.025, 0.05, 0.075, 0.1. Sorted p: 0.01, 0.03, 0.04, 0.2.
    # 0.04 <= 0.075 so the first three pass; 0.2 > 0.1 fails.
    assert benjamini_hochberg([0.04, 0.2, 0.01, 0.03], 0.1) == [True, False, True, True]
    # Step-up: a p-value above its own threshold still passes when a larger rank passes.
    assert benjamini_hochberg([0.03, 0.04], 0.08) == [True, True]
    assert benjamini_hochberg([0.5, 0.6], 0.1) == [False, False]
    assert benjamini_hochberg([], 0.1) == []


def test_verdicts_need_data_direction_and_fdr():
    strong = heldout_test([Table("a", 20, 2, 5, 20), Table("b", 15, 3, 4, 18)])
    opposite = heldout_test([Table("a", 2, 20, 20, 5), Table("b", 3, 15, 18, 4)])
    tiny = heldout_test([Table("a", 2, 0, 0, 3)])
    assert strong["p_value"] < 0.001 and strong["effect"] > 0
    assert verdicts([strong, opposite, tiny], 0.1) == ["supported", "contradicted", "inconclusive"]
