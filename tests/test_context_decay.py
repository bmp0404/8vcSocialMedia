import pytest

from harness.context.decay import DecayParams, _bin_integral, crane_sornette_class, fit_alpha, post_peak_counts

P = DecayParams()


def expected_counts(amplitude, alpha, bins=96):
    return [amplitude * _bin_integral(k, alpha, P) for k in range(bins)]


@pytest.mark.parametrize("alpha", [0.2, 0.6, 1.0, 1.4])
def test_fit_recovers_alpha_from_exact_expected_counts(alpha):
    posts = [expected_counts(a, alpha) for a in (300.0, 1200.0, 80.0)]
    assert fit_alpha(posts) == pytest.approx(alpha, abs=1e-3)


def test_fit_pools_posts_of_different_lengths():
    posts = [expected_counts(500.0, 0.6, bins=20), expected_counts(500.0, 0.6, bins=200)]
    assert fit_alpha(posts) == pytest.approx(0.6, abs=1e-3)


def test_fit_refuses_too_few_events():
    assert fit_alpha([[10, 5, 3]]) is None


def test_bin_integral_at_alpha_one_is_log():
    assert _bin_integral(0, 1.0, P) == pytest.approx(0.6931471805599453)


def test_post_peak_counts_starts_at_first_max():
    assert post_peak_counts([1, 5, 9, 9, 3]) == [9, 9, 3]
    assert post_peak_counts([]) == []


@pytest.mark.parametrize(
    "alpha,sudden,label",
    [
        (1.3, True, "exogenous_subcritical"),
        (0.7, True, "exogenous_critical"),
        (1.0, True, "exogenous_critical"),  # equidistant: ties go to critical
        (0.25, False, "endogenous_critical"),
        (0.35, False, "endogenous_critical"),
        (0.45, False, "mixed"),  # nearer 0.6 than 0.2
        (1.3, False, "mixed"),
    ],
)
def test_crane_sornette_class(alpha, sudden, label):
    assert crane_sornette_class(alpha, sudden) == label
