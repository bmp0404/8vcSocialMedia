"""Post-peak power-law decay, pooled per account, and Crane-Sornette classes.

Crane and Sornette (PNAS 2008) sort bursts of attention by how they start and
how fast they decay. After the peak the activity rate falls as a power law,
rate(t) ~ (t + c)^-alpha, and alpha sits near one of three values set by the
memory exponent theta (about 0.4 in their data):

    exogenous subcritical  sudden onset, alpha ~ 1 + theta  (1.4): a shock nobody passes on
    exogenous critical     sudden onset, alpha ~ 1 - theta  (0.6): a shock word of mouth carries
    endogenous critical    gradual onset, alpha ~ 1 - 2*theta (0.2): built by sharing alone

A single post rarely has the ~50 post-peak events a fit needs, so the
exponent is fitted jointly over an account's posts: each post keeps its own
amplitude, all share one alpha. The fit is Poisson maximum likelihood on
binned counts; for a fixed alpha each amplitude has a closed form, so alpha is
found by a grid search on the profile likelihood, then refined.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DecayParams:
    dt: float = 15.0  # bin width, minutes
    c: float = 15.0  # offset in (t + c)^-alpha, keeps the rate finite at the peak
    alpha_min: float = 0.05
    alpha_max: float = 3.0
    grid_step: float = 0.01
    theta: float = 0.4
    min_events: int = 50  # pooled post-peak events needed before a fit is reported


def _bin_integral(k: int, alpha: float, p: DecayParams) -> float:
    """Integral of (t + c)^-alpha over bin k = [k*dt, (k+1)*dt)."""
    a, b = k * p.dt + p.c, (k + 1) * p.dt + p.c
    if abs(alpha - 1.0) < 1e-12:
        return math.log(b / a)
    return (b ** (1 - alpha) - a ** (1 - alpha)) / (1 - alpha)


def profile_loglik(posts: list[list[float]], alpha: float, p: DecayParams = DecayParams()) -> float:
    """Poisson log-likelihood (dropping the log n! constant) with each post's amplitude at its MLE.

    `posts` holds, per post, the counts in consecutive dt bins starting at its peak.
    """
    total = 0.0
    for counts in posts:
        integ = [_bin_integral(k, alpha, p) for k in range(len(counts))]
        n, s = sum(counts), sum(integ)
        if n == 0 or s <= 0:
            continue
        amp = n / s
        total += sum(c * math.log(amp * i) for c, i in zip(counts, integ) if c > 0) - n
    return total


def fit_alpha(posts: list[list[float]], p: DecayParams = DecayParams()) -> float | None:
    """Shared decay exponent for `posts` (post-peak binned counts). None when events < min_events."""
    if sum(sum(c) for c in posts) < p.min_events:
        return None
    steps = int(round((p.alpha_max - p.alpha_min) / p.grid_step))
    grid = [p.alpha_min + i * p.grid_step for i in range(steps + 1)]
    best = max(grid, key=lambda a: profile_loglik(posts, a, p))
    # Golden-section refinement inside one grid step either side.
    lo, hi = max(p.alpha_min, best - p.grid_step), min(p.alpha_max, best + p.grid_step)
    g = (math.sqrt(5) - 1) / 2
    for _ in range(40):
        m1, m2 = hi - g * (hi - lo), lo + g * (hi - lo)
        if profile_loglik(posts, m1, p) >= profile_loglik(posts, m2, p):
            hi = m2
        else:
            lo = m1
    return (lo + hi) / 2


def post_peak_counts(increments: list[float]) -> list[float]:
    """The binned increments from the highest bin onward (first bin if tied)."""
    if not increments:
        return []
    i = increments.index(max(increments))
    return increments[i:]


def crane_sornette_class(alpha: float, sudden_onset: bool, p: DecayParams = DecayParams()) -> str:
    """Nearest class for a fitted exponent and the onset type.

    A sudden onset picks the nearer exogenous exponent. A gradual onset is
    endogenous_critical when alpha is nearest 1 - 2*theta, else `mixed`: the
    onset and the decay disagree, which the theory does not predict.
    """
    exo_sub, exo_crit, endo = 1 + p.theta, 1 - p.theta, 1 - 2 * p.theta
    if sudden_onset:
        # Ties (alpha exactly between the two) go to critical; the tolerance absorbs float error.
        return "exogenous_subcritical" if abs(alpha - exo_sub) < abs(alpha - exo_crit) - 1e-9 else "exogenous_critical"
    nearest = min((exo_sub, "exo"), (exo_crit, "exo"), (endo, "endo"), key=lambda x: abs(alpha - x[0]))
    return "endogenous_critical" if nearest[1] == "endo" else "mixed"
