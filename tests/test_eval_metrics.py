import math

import pytest

from harness.evals import metrics as m


def test_ranks_average_ties():
    assert m.ranks([10, 20, 20, 30]) == [1, 2.5, 2.5, 4]
    assert m.ranks([3, 1, 2]) == [3, 1, 2]


def test_spearman_known_values():
    assert m.spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) == pytest.approx(0.8)
    assert m.spearman([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)
    # ties: ranks [1, 2.5, 2.5, 4] vs [1, 2, 3, 4] give 4.5 / sqrt(4.5 * 5) = 3 / sqrt(10)
    assert m.spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(3 / math.sqrt(10))


def test_spearman_undefined():
    assert m.spearman([1, 1, 1], [1, 2, 3]) is None
    assert m.spearman([1], [2]) is None
    with pytest.raises(ValueError):
        m.spearman([1, 2], [1])


def test_pairwise_accuracy_order_and_ties():
    assert m.pairwise_accuracy([1, 2, 3], [1, 2, 3]) == (1.0, 3)
    assert m.pairwise_accuracy([3, 2, 1], [1, 2, 3]) == (0.0, 3)
    assert m.pairwise_accuracy([1, 1, 1], [1, 2, 3]) == (0.5, 3)
    assert m.pairwise_accuracy([1, 2], [5, 5]) == (None, 0)


def test_pairwise_accuracy_only_compares_within_groups():
    scores, targets = [1, 2, 10, 11], [0, 1, 0, 1]
    assert m.pairwise_accuracy(scores, targets, ["a", "a", "b", "b"]) == (1.0, 2)
    # Without groups the cross pair (score 2, target 1) vs (score 10, target 0) counts against it.
    assert m.pairwise_accuracy(scores, targets) == (0.75, 4)


def test_pairwise_accuracy_is_auc_for_binary_targets():
    assert m.pairwise_accuracy([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1])[0] == pytest.approx(0.75)


def test_precision_at_k():
    assert m.precision_at_k([0.9, 0.8, 0.7, 0.6], [1, 0, 1, 0], 2) == 0.5
    assert m.precision_at_k([0.9, 0.8, 0.7, 0.6], [1, 0, 1, 0], 3) == pytest.approx(2 / 3)
    assert m.precision_at_k([1, 2], [1, 0], 0) is None
    assert m.precision_at_k([1, 2], [1, 0], 3) is None


def test_precision_at_k_expected_value_over_boundary_ties():
    # One sure hit above the cut, then one slot filled from three tied items holding one positive.
    assert m.precision_at_k([1, 0.5, 0.5, 0.5], [1, 1, 0, 0], 2) == pytest.approx((1 + 1 / 3) / 2)
    # Input order must not matter.
    assert m.precision_at_k([0.5, 0.5, 1, 0.5], [0, 1, 1, 0], 2) == pytest.approx((1 + 1 / 3) / 2)


def test_top_pick_beats_median():
    scores = [3, 1, 2, 1, 2, 9]
    targets = [10, 0, 5, 4, 2, 7]
    groups = ["a", "a", "a", "b", "b", "solo"]
    # a: top pick has target 10 > median 5 -> 1. b: top pick has target 2 < median 3 -> 0. solo is skipped.
    assert m.top_pick_beats_median(scores, targets, groups) == (0.5, 2)


def test_top_pick_ties_and_median_hits():
    assert m.top_pick_beats_median([1, 1], [0, 2], ["a", "a"]) == (0.5, 1)
    assert m.top_pick_beats_median([0, 5, 0], [1, 2, 3], ["a", "a", "a"]) == (0.5, 1)
    assert m.top_pick_beats_median([1], [1], ["a"]) == (None, 0)


def test_brier_reliability_ece():
    assert m.brier([1.0, 0.0], [True, False]) == 0
    assert m.brier([0.5, 0.5], [True, False]) == 0.25
    assert m.brier([], []) is None
    probs, outcomes = [0.1, 0.15, 0.9, 1.0], [False, True, True, True]
    bins = m.reliability(probs, outcomes, bins=5)
    assert [b["n"] for b in bins] == [2, 0, 0, 0, 2]
    assert bins[0]["mean_pred"] == pytest.approx(0.125) and bins[0]["observed"] == 0.5
    assert bins[4]["mean_pred"] == pytest.approx(0.95) and bins[4]["observed"] == 1.0
    assert m.ece(probs, outcomes, bins=5) == pytest.approx(0.5 * 0.375 + 0.5 * 0.05)


def test_wilson_known_intervals():
    lo, hi = m.wilson(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-4), pytest.approx(0.7634, abs=1e-4))
    lo, hi = m.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    assert m.wilson(1, 0) is None


def test_cluster_bootstrap_counts_a_redrawn_group_twice():
    groups = ["a", "a", "b", "c", "c", "c"]
    # Every resample draws 3 groups; each draw must get its own id even when a group repeats.
    assert m.cluster_bootstrap(lambda idx, d: len(set(d)), groups, n_boot=200) == (3, 3)


def test_cluster_bootstrap_is_seeded_and_drops_undefined():
    groups = list("aabbccdd")
    values = list(range(8))

    def mean(idx, d):
        return sum(values[i] for i in idx) / len(idx)

    assert m.cluster_bootstrap(mean, groups, seed=1) == m.cluster_bootstrap(mean, groups, seed=1)
    assert m.cluster_bootstrap(lambda idx, d: None, groups) is None
