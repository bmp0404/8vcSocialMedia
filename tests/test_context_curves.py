"""Inflection detection on synthetic curves with known shapes.

Each builder samples an analytic cumulative curve every `step` minutes, so the
expected label follows from the definitions in harness/context/curves.py.
"""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from harness.context.curves import (
    Amplifier,
    InflectionParams,
    Snapshot,
    build_curve,
    classify_shape,
    curve_from_event_times,
    detect,
    features,
    find_waves,
    to_grid,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
P = InflectionParams()


def snaps_from(cum, hours=48, step=15, split=(1.0, 0.0, 0.0, 0.0)):
    """Snapshots of a cumulative function; `split` divides each count into likes/reposts/replies/quotes."""
    out = []
    for k in range(1, int(hours * 60 / step) + 1):
        m = k * step
        total = cum(m)
        parts = [round(total * s) for s in split]
        out.append(Snapshot("1", T0 + timedelta(minutes=m), *parts))
    return out


def exp_cum(total, tau, start=0.0):
    return lambda m: total * (1 - math.exp(-(m - start) / tau)) if m > start else 0.0


def gamma_cum(total, theta, start):
    def f(m):
        s = m - start
        return total * (1 - math.exp(-s / theta) * (1 + s / theta)) if s > 0 else 0.0

    return f


def logistic_cum(total, mid, width):
    return lambda m: total / (1 + math.exp(-(m - mid) / width))


def shape_of(snaps, **kw):
    return detect(T0, snaps, **kw).shape


# ------------------------------------------------------------------ shapes


def test_early_spike():
    assert shape_of(snaps_from(exp_cum(1000, 30))) == "early_spike"


def test_slow_burn_linear():
    assert shape_of(snaps_from(lambda m: 2000 * m / 2880)) == "slow_burn"


def test_delayed_takeoff_logistic():
    assert shape_of(snaps_from(logistic_cum(1000, 480, 90))) == "delayed_takeoff"


def test_step_jump_after_small_spike():
    base = exp_cum(200, 30)

    def cum(m):
        jump = 600 if m >= 375 else 0  # lands between the 360 and 375 minute snapshots
        tail = 50 * (1 - math.exp(-(m - 375) / 60)) if m > 375 else 0
        return base(m) + jump + tail

    assert shape_of(snaps_from(cum)) == "step_jump"


def test_multi_wave_spike_then_gradual_reignition():
    # Second wave peaks ~11.6 h after the first: outside the 20-28 h circadian window.
    w1, w2 = exp_cum(500, 45), gamma_cum(1500, 90, start=600)
    assert shape_of(snaps_from(lambda m: w1(m) + w2(m))) == "multi_wave"


def test_second_peak_a_day_later_is_circadian_echo():
    # Second wave peaks at ~24 h (1380 + 90 min): the audience's next day, not news.
    w1, w2 = exp_cum(500, 45), gamma_cum(1500, 90, start=1380)
    assert shape_of(snaps_from(lambda m: w1(m) + w2(m))) == "circadian_echo"


def test_stillborn_when_observed_but_tiny():
    assert shape_of(snaps_from(exp_cum(10, 30))) == "stillborn"


def test_insufficient_beats_stillborn_when_observed_under_an_hour():
    assert shape_of(snaps_from(exp_cum(5, 30), hours=0.75)) == "insufficient"


def test_insufficient_when_observed_under_an_hour():
    assert shape_of(snaps_from(exp_cum(1000, 30), hours=0.75)) == "insufficient"


def test_unclassified_peak_between_spike_and_takeoff_windows():
    # Peak at ~150 min: too late for early_spike (<=120), too early for delayed_takeoff (>=180).
    assert shape_of(snaps_from(gamma_cum(1000, 150, start=0))) == "unclassified"


def test_ramp_into_late_peak_is_takeoff_not_step():
    # Same timing as the step test but the rise takes many bins, so it is pickup, not a quote.
    assert shape_of(snaps_from(logistic_cum(800, 400, 40))) == "delayed_takeoff"


def test_step_jump_wins_over_two_waves():
    # A step after a spike also produces two peaks; the abrupt one is labelled step_jump.
    base = exp_cum(400, 30)
    cum = lambda m: base(m) + (500 if m >= 1215 else 0)  # noqa: E731
    assert shape_of(snaps_from(cum)) == "step_jump"


def test_sparse_polling_hides_a_step():
    # The same jump seen only through 6 h gaps is spread over the gap, so it is not called a step.
    base = exp_cum(200, 30)
    cum = lambda m: base(m) + (600 if m >= 375 else 0)  # noqa: E731
    snaps = [s for s in snaps_from(cum) if (s.observed_at - T0).total_seconds() / 60 % 360 == 0]
    assert detect(T0, snaps).shape != "step_jump"


# ------------------------------------------------------------------ features


def test_half_life_of_binned_exponential_is_exact():
    # Bin k velocity is proportional to exp(-k*dt/tau); with tau=30, dt=15 it first
    # drops to <= half of bin 0 at k=2 (exp(-1) = 0.37), so the half-life is 30 min.
    f = features(build_curve(T0, snaps_from(exp_cum(1000, 30))), P)
    assert f.t_peak_min == 7.5
    assert f.half_life_min == 30.0
    assert f.n_waves == 1


def test_t50_of_linear_curve_is_half_the_window():
    # One engagement per minute keeps every snapshot an exact integer.
    f = features(build_curve(T0, snaps_from(lambda m: m)), P)
    assert f.t50_min == pytest.approx(1440.0)
    assert f.t90_min == pytest.approx(2592.0)
    assert f.peak_to_mean == pytest.approx(1.0)


def test_build_curve_takes_running_max_and_keeps_raw():
    snaps = [
        Snapshot("1", T0 + timedelta(minutes=10), 100, 0, 0, 0),
        Snapshot("1", T0 + timedelta(minutes=20), 90, 0, 0, 0),
        Snapshot("1", T0 + timedelta(minutes=30), 120, 0, 0, 0),
    ]
    c = build_curve(T0, snaps, "likes")
    assert c.t == [0.0, 10.0, 20.0, 30.0]
    assert c.y == [0.0, 100.0, 100.0, 120.0]
    assert c.raw == [0.0, 100.0, 90.0, 120.0]


def test_build_curve_ignores_snapshots_before_creation():
    snaps = [Snapshot("1", T0 - timedelta(minutes=5), 50, 0, 0, 0), Snapshot("1", T0 + timedelta(minutes=5), 7, 0, 0, 0)]
    assert build_curve(T0, snaps, "likes").y == [0.0, 7.0]


def test_build_curve_rejects_unknown_metric():
    with pytest.raises(ValueError):
        build_curve(T0, [], "impressions")


def test_grid_drops_partial_last_bin():
    c = build_curve(T0, [Snapshot("1", T0 + timedelta(minutes=40), 40, 0, 0, 0)], "likes")
    assert to_grid(c, 15) == pytest.approx([15.0, 15.0])


def test_curve_from_event_times_counts_ties_once_per_timestamp():
    times = [T0 + timedelta(minutes=m) for m in (5, 5, 12, -3)]
    c = curve_from_event_times(T0, times)
    assert c.t == [0.0, 5.0, 12.0]
    assert c.y == [0.0, 2.0, 3.0]


def test_find_waves_merges_peaks_without_a_deep_trough():
    v = [10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 9, 8, 9, 0]
    # peaks at 0 and 13 are separated (trough 0, 13 bins * 15 min >= 180); 15 merges into 13.
    assert find_waves(v, 15, P) == [0, 13]


def test_find_waves_requires_minimum_gap():
    v = [10, 0, 10, 0]
    assert find_waves(v, 15, P) == [0]


def _views_snaps(views_at, eng_at, minutes):
    return [Snapshot("1", T0 + timedelta(minutes=m), eng_at(m), 0, 0, 0, views=views_at(m)) for m in minutes]


def test_view_surge_low_conversion_flags_promoted_reach():
    # 100 views/min with 2% conversion, then 2,000 views/min converting at 0.1%.
    mins = list(range(15, 181, 15))
    views = lambda m: 100 * m if m <= 120 else 12000 + 2000 * (m - 120)  # noqa: E731
    eng = lambda m: round(0.02 * min(m, 120) * 100 + (0.001 * 2000 * (m - 120) if m > 120 else 0))  # noqa: E731
    inf = detect(T0, _views_snaps(views, eng, mins))
    assert "view_surge_low_conversion" in inf.flags
    assert inf.stats["view_surge"]["view_rate_ratio"] == 20.0


def test_view_surge_that_converts_is_not_flagged():
    mins = list(range(15, 181, 15))
    views = lambda m: 100 * m if m <= 120 else 12000 + 2000 * (m - 120)  # noqa: E731
    eng = lambda m: round(0.02 * views(m))  # noqa: E731
    assert "view_surge_low_conversion" not in detect(T0, _views_snaps(views, eng, mins)).flags


def test_slow_start_against_author_baseline():
    snaps = snaps_from(exp_cum(100, 30))
    assert "slow_start" in detect(T0, snaps, baseline_60=500).flags
    assert "slow_start" not in detect(T0, snaps, baseline_60=50).flags


def test_classify_shape_uses_snapshot_floor():
    f = features(build_curve(T0, [Snapshot("1", T0 + timedelta(minutes=120), 500, 0, 0, 0)]), P)
    assert f.n_snapshots == 2
    assert classify_shape(f, P) == "insufficient"


# ------------------------------------------------------------------ flags


def test_reply_ratio_flag():
    snaps = snaps_from(exp_cum(1000, 30), split=(0.4, 0.0, 0.5, 0.1))
    assert "reply_ratio" in detect(T0, snaps).flags


def test_no_reply_ratio_below_minimum_replies():
    snaps = snaps_from(exp_cum(30, 30), split=(0.3, 0.0, 0.6, 0.1))  # 18 replies
    assert "reply_ratio" not in detect(T0, snaps).flags


def test_drip_feed_flagged_on_fixed_rate_delivery():
    spike = exp_cum(300, 30)
    cum = lambda m: spike(m) + (20 * (m - 120) / 15 if m > 120 else 0)  # noqa: E731
    inf = detect(T0, snaps_from(cum, hours=14))
    assert "drip_feed" in inf.flags
    assert inf.stats["min_dispersion"] < 0.01


def _poisson(rng, lam):
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def _poisson_snaps(seed, lam=20, hours=48):
    rng = random.Random(seed)
    total, out = 0, []
    for k in range(1, int(hours * 4) + 1):
        total += _poisson(rng, lam)
        out.append(Snapshot("1", T0 + timedelta(minutes=15 * k), total, 0, 0, 0))
    return out


def test_poisson_arrivals_are_not_drip_feed():
    assert "drip_feed" not in detect(T0, _poisson_snaps(seed=7)).flags


def test_drip_feed_false_positive_rate_on_poisson_is_low():
    # Organic arrivals are at least as bursty as Poisson; under pure Poisson the
    # dispersion test should fire rarely even scanning every 12-interval window.
    fired = sum("drip_feed" in detect(T0, _poisson_snaps(seed=s)).flags for s in range(100))
    assert fired <= 2


def test_drip_feed_ignores_uniform_increments_created_by_sparse_polling():
    # Two snapshots 12 h apart interpolate to identical bins; dispersion uses raw intervals only.
    snaps = [Snapshot("1", T0 + timedelta(hours=2), 300, 0, 0, 0), Snapshot("1", T0 + timedelta(hours=14), 1300, 0, 0, 0)]
    assert "drip_feed" not in detect(T0, snaps).flags


def test_like_only_burst():
    snaps = [
        Snapshot("1", T0 + timedelta(minutes=30), 100, 20, 10, 2),
        Snapshot("1", T0 + timedelta(minutes=45), 260, 20, 11, 2),
        Snapshot("1", T0 + timedelta(minutes=90), 270, 22, 12, 2),
    ]
    assert "like_only_burst" in detect(T0, snaps).flags


def test_count_drop():
    snaps = [
        Snapshot("1", T0 + timedelta(minutes=30), 1000, 50, 10, 2),
        Snapshot("1", T0 + timedelta(minutes=60), 1100, 55, 12, 2),
        Snapshot("1", T0 + timedelta(minutes=90), 980, 55, 12, 2),
    ]
    inf = detect(T0, snaps)
    assert "count_drop" in inf.flags


def _step_snaps():
    base = exp_cum(200, 30)
    return snaps_from(lambda m: base(m) + (600 if m >= 375 else 0))


def test_step_with_matching_amplifier_is_explained():
    amp = Amplifier(at=T0 + timedelta(minutes=365), handle="bigaccount", followers=2_000_000)
    inf = detect(T0, _step_snaps(), amplifiers=[amp])
    assert inf.shape == "step_jump"
    assert inf.explained_by == ["bigaccount"]
    assert "unexplained_step" not in inf.flags


def test_step_with_no_amplifier_found_is_flagged():
    inf = detect(T0, _step_snaps(), amplifiers=[])
    assert "unexplained_step" in inf.flags


def test_step_without_amplifier_lookup_is_not_flagged():
    assert "unexplained_step" not in detect(T0, _step_snaps(), amplifiers=None).flags
