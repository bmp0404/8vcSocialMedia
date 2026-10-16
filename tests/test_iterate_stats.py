"""Strict tests for the promotion statistics: each case has one correct answer, worked by hand."""

import math

import pytest

from harness.iterate import stats


def test_quantile_matches_linear_interpolation():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert stats.quantile(xs, 0.0) == 1.0
    assert stats.quantile(xs, 1.0) == 4.0
    assert stats.quantile(xs, 0.5) == 2.5
    assert stats.quantile(xs, 1 / 3) == pytest.approx(2.0)
    assert stats.quantile([7.0], 0.3) == 7.0
    with pytest.raises(ValueError):
        stats.quantile([], 0.5)
    with pytest.raises(ValueError):
        stats.quantile(xs, 1.5)


def test_benjamini_hochberg_hand_example():
    # Sorted p: 0.005, 0.01, 0.03, 0.04 -> p*m/rank = 0.02, 0.02, 0.04, 0.04; step-up minima give the same.
    p = [0.01, 0.04, 0.03, 0.005]
    adjusted, rejected = stats.benjamini_hochberg(p, q=0.05)
    assert adjusted == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert rejected == [True, True, True, True]
    _, rejected = stats.benjamini_hochberg(p, q=0.03)
    assert rejected == [True, False, False, True]


def test_benjamini_hochberg_step_up_rescues_a_smaller_p():
    # Rank 1 alone fails (0.04 * 3 / 1 = 0.12) but rank 3 passes (0.045 * 3 / 3 = 0.045), so all three reject.
    adjusted, rejected = stats.benjamini_hochberg([0.04, 0.042, 0.045], q=0.05)
    assert adjusted == pytest.approx([0.045, 0.045, 0.045])
    assert rejected == [True, True, True]


def test_benjamini_hochberg_edges():
    assert stats.benjamini_hochberg([], 0.1) == ([], [])
    adjusted, rejected = stats.benjamini_hochberg([0.3], 0.1)
    assert adjusted == [0.3] and rejected == [False]
    adjusted, _ = stats.benjamini_hochberg([0.9, 0.95], 0.1)
    assert adjusted == pytest.approx([0.95, 0.95])
    assert all(a <= 1.0 for a in stats.benjamini_hochberg([1.0, 1.0, 0.99], 0.1)[0])
    with pytest.raises(ValueError):
        stats.benjamini_hochberg([0.2, 1.2], 0.1)
    with pytest.raises(ValueError):
        stats.benjamini_hochberg([0.2], 0.0)


def test_cluster_draws_keep_clusters_whole():
    clusters = ["a", "a", "b", "b", "b", "c"]
    members = {"a": [0, 1], "b": [2, 3, 4], "c": [5]}
    for idx, ids in stats.cluster_draws(clusters, n_boot=50, seed=3):
        assert len(set(ids)) == 3  # one pick per unique cluster
        for d in set(ids):
            picked = [i for i, g in zip(idx, ids) if g == d]
            assert picked in members.values()
    assert stats.cluster_draws(clusters, 20, seed=1) == stats.cluster_draws(clusters, 20, seed=1)
    with pytest.raises(ValueError):
        stats.cluster_draws([], 10, 0)


def test_paired_bootstrap_identical_arms_has_p_one():
    outcomes = [1, 0, 1, 1, 0, 0, 1, 0]
    s = stats.mean_stat(outcomes)
    res = stats.paired_bootstrap(s, s, list(range(8)), higher_is_better=True, n_boot=200, seed=0)
    assert res.delta == 0 and res.improvement == 0
    assert set(res.draws) == {0.0}
    assert res.p_one_sided == 1.0


def test_paired_bootstrap_certain_win_lower_is_better():
    champ = stats.mean_stat([1.0] * 10)  # detected every time
    chall = stats.mean_stat([0.0] * 10)  # never detected
    res = stats.paired_bootstrap(champ, chall, list(range(10)), higher_is_better=False, n_boot=500, seed=0)
    assert res.champion == 1.0 and res.challenger == 0.0
    assert res.delta == -1.0 and res.improvement == 1.0
    assert set(res.draws) == {1.0}
    assert res.p_one_sided == pytest.approx(1 / 501)
    assert res.interval(0.95) == (1.0, 1.0)


def test_paired_bootstrap_sign_follows_direction():
    a, b = stats.mean_stat([0.2] * 6), stats.mean_stat([0.5] * 6)
    up = stats.paired_bootstrap(a, b, list(range(6)), higher_is_better=True, n_boot=50, seed=0)
    down = stats.paired_bootstrap(a, b, list(range(6)), higher_is_better=False, n_boot=50, seed=0)
    assert up.improvement == pytest.approx(0.3) and down.improvement == pytest.approx(-0.3)
    assert up.delta == down.delta == pytest.approx(0.3)


def test_paired_bootstrap_pairs_items_within_a_draw():
    # Per-item differences are all exactly +1, so every paired draw is +1 even though each arm varies a lot
    # across draws. Unpaired resampling would produce a spread.
    champ_vals = [0, 1, 2, 3, 4, 5, 6, 7]
    chall_vals = [v + 1 for v in champ_vals]
    res = stats.paired_bootstrap(stats.mean_stat(champ_vals), stats.mean_stat(chall_vals), list(range(8)),
                                 higher_is_better=True, n_boot=300, seed=2)
    assert all(d == pytest.approx(1.0) for d in res.draws)


def test_paired_bootstrap_rejects_undefined_metric():
    never = lambda idx, ids: None  # noqa: E731
    with pytest.raises(ValueError):
        stats.paired_bootstrap(never, never, [0, 1, 2], higher_is_better=True, n_boot=10)


def test_within_cluster_auc_ignores_cross_cluster_pairs_and_halves_ties():
    # Cluster A: positives 0.9 and 0.4 against negative 0.5 -> 1 + 0. Cluster B: tie 0.7 vs 0.7 -> 0.5.
    # The cross-cluster pair (B's positive 0.7 vs A's negative 0.5) must not count.
    scores = [0.9, 0.4, 0.5, 0.7, 0.7]
    labels = [True, True, False, True, False]
    clusters = ["A", "A", "A", "B", "B"]
    stat = stats.within_cluster_auc_stat(scores, labels)
    assert stat(list(range(5)), clusters) == pytest.approx(1.5 / 3)
    assert stat([0, 1], ["A", "A"]) is None  # no negatives in any group


def test_within_cluster_auc_treats_a_repeated_cluster_as_two():
    # One cluster drawn twice: draw ids 0 and 1. Pairs form only inside each copy (2 pairs), never across.
    stat = stats.within_cluster_auc_stat([1.0, 0.0], [True, False])
    assert stat([0, 1, 0, 1], [0, 0, 1, 1]) == 1.0


def _paired_from(draws, champion=0.5, challenger=0.4, higher=False):
    imp = (challenger - champion) * (1 if higher else -1)
    return stats.Paired(champion, challenger, challenger - champion, imp, tuple(sorted(draws)),
                        (1 + sum(d <= 0 for d in draws)) / (1 + len(draws)))


def test_decide_promotes_only_beyond_the_noise_floor():
    draws = [i / 100 for i in range(1, 101)]  # improvements 0.01 .. 1.00
    paired = _paired_from(draws)
    # One rejection in a family of 10 at q=0.1: alpha = R*q/m = 0.01, below the 0.025 cap.
    # quantile(draws, 0.01): position 0.99 -> 0.01 + 0.99 * 0.01 = 0.0199.
    dec = stats.decide(paired, rejected_by_bh=True, n_rejected=1, family_size=10, q=0.1, noise_floor=0.019)
    assert dec.status == "promoted"
    assert dec.interval_level == pytest.approx(0.99)
    assert dec.bound == pytest.approx(0.0199)
    dec = stats.decide(paired, rejected_by_bh=True, n_rejected=1, family_size=10, q=0.1, noise_floor=0.02)
    assert dec.status == "inconclusive"


def test_decide_never_uses_a_bound_looser_than_97_5_percent():
    draws = [i / 100 for i in range(1, 101)]
    paired = _paired_from(draws)
    # A family of one at q=0.1 would give alpha 0.1; the cap holds it at 0.025.
    # quantile(draws, 0.025): position 2.475 -> 0.03 + 0.475 * 0.01 = 0.03475.
    dec = stats.decide(paired, rejected_by_bh=True, n_rejected=1, family_size=1, q=0.1, noise_floor=0.0)
    assert dec.interval_level == pytest.approx(0.975)
    assert dec.bound == pytest.approx(0.03475)


def test_non_inferiority_needs_the_lower_end_above_minus_margin():
    near = _paired_from([-0.05 + i / 1000 for i in range(101)])  # improvements -0.05 .. +0.05
    assert stats.non_inferior(near, margin=0.1)
    assert not stats.non_inferior(near, margin=0.02)
    unknown = _paired_from([-0.4 + i / 125 for i in range(101)])  # -0.4 .. +0.4: too wide to tell
    assert not stats.non_inferior(unknown, margin=0.1)
    with pytest.raises(ValueError):
        stats.non_inferior(near, margin=-0.1)


def test_decide_rejects_a_clearly_worse_challenger_and_keeps_the_rest_inconclusive():
    worse = _paired_from([-0.3 + i / 1000 for i in range(100)])
    assert stats.decide(worse, rejected_by_bh=False, n_rejected=0, family_size=3, q=0.1,
                        noise_floor=0.0).status == "rejected"
    mixed = _paired_from([-0.1 + i / 500 for i in range(100)])
    assert stats.decide(mixed, rejected_by_bh=False, n_rejected=0, family_size=3, q=0.1,
                        noise_floor=0.0).status == "inconclusive"


def test_decide_validates_inputs():
    p = _paired_from([0.1, 0.2])
    with pytest.raises(ValueError):
        stats.decide(p, rejected_by_bh=True, n_rejected=0, family_size=1, q=0.1, noise_floor=0.0)
    with pytest.raises(ValueError):
        stats.decide(p, rejected_by_bh=False, n_rejected=0, family_size=1, q=0.1, noise_floor=-0.1)
    with pytest.raises(ValueError):
        stats.decide(p, rejected_by_bh=False, n_rejected=0, family_size=1, q=0.1, noise_floor=math.nan)


def test_marginal_interval_is_none_when_undefined():
    assert stats.marginal_interval(lambda i, d: None, [0, 1, 2], n_boot=20) is None
    lo, hi = stats.marginal_interval(stats.mean_stat([1, 1, 1]), [0, 1, 2], n_boot=20)
    assert lo == hi == 1.0
