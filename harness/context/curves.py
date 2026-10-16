"""Engagement curves: snapshot schema, curve features, and inflection classification.

The X API returns only current counts. A curve has to come from repeated
snapshots of the same post (see `tracker.py`) or from the creation times of the
replies and quotes it received (Snowflake IDs, see `curve_from_event_times`).

Pipeline:
    snapshots --build_curve--> Curve (minutes since creation, cumulative count)
              --to_grid------> uniform bins of `dt` minutes (increment per bin)
              --features-----> CurveFeatures
              --classify-----> one shape label + independent flags

Shape labels (exactly one per curve, checked in this order):
    insufficient     too little data to say anything
    step_jump        one bin after the first hour carries >= 25% of the total and
                     arrives abruptly: the usual mark of a large account quoting
                     or reposting the post
    multi_wave       two or more separate velocity peaks: re-ignition, often news
    delayed_takeoff  quiet first hour, then a gradual rise to a peak >= 3 h in:
                     the mark of algorithmic pickup
    early_spike      peak in the first 2 h and velocity halves within 3 h after it
    slow_burn        no sharp peak, and half the engagement arrives after 6 h
    unclassified     none of the definitions fit

Flags (any number per curve):
    reply_ratio      replies + quotes >= likes (a "ratio"; controversy or dunking)
    drip_feed        increments far more regular than any organic process:
                     the mark of paid engagement delivered at a fixed rate
    like_only_burst  a stretch where likes grow and nothing else does
    count_drop       a count fell by >= 5% of its peak: X purging fake engagement
    unexplained_step a step_jump with no known amplifier at that moment, when
                     amplifier data was supplied

Every threshold lives in `InflectionParams`. None of them has been fitted to
data; they encode the definitions above, and the tests pin those definitions.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

METRICS = ("likes", "reposts", "replies", "quotes", "bookmarks", "views", "engagement")


@dataclass
class Snapshot:
    """Counts for one post at one moment. The schema the tracker stores, one JSON line each."""

    post_id: str
    observed_at: datetime  # UTC
    likes: int
    reposts: int
    replies: int
    quotes: int
    bookmarks: int | None = None
    views: int | None = None
    source: str = "fxtwitter"

    @property
    def engagement(self) -> int:
        return self.likes + self.reposts + self.replies + self.quotes

    def value(self, metric: str) -> float | None:
        if metric == "engagement":
            return float(self.engagement)
        v = getattr(self, metric)
        return None if v is None else float(v)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["observed_at"] = self.observed_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot:
        d = dict(d)
        d["observed_at"] = datetime.fromisoformat(d["observed_at"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_xpost(cls, post) -> Snapshot:
        return cls(
            post_id=post.id,
            observed_at=datetime.fromisoformat(post.fetched_at),
            likes=post.likes,
            reposts=post.reposts,
            replies=post.replies,
            quotes=post.quotes,
            bookmarks=post.bookmarks,
            views=post.views,
            source=post.source,
        )


@dataclass
class Amplifier:
    """Someone large who quoted, reposted or replied, and when."""

    at: datetime
    handle: str
    followers: int | None = None
    kind: str = "quote"  # quote | repost | reply


@dataclass
class Curve:
    t: list[float]  # minutes since creation, strictly increasing, t[0] == 0
    y: list[float]  # cumulative count, non-decreasing (running max of the raw counts)
    raw: list[float]  # counts as observed, which can fall
    metric: str = "engagement"

    @property
    def final(self) -> float:
        return self.y[-1]

    @property
    def duration(self) -> float:
        return self.t[-1]


@dataclass(frozen=True)
class InflectionParams:
    dt: float = 15.0  # bin width, minutes
    min_total: float = 20.0
    min_duration: float = 60.0
    min_snapshots: int = 3  # including the implicit zero at creation
    # waves
    wave_min_frac: float = 0.3  # a wave peak is >= 30% of the highest peak
    trough_frac: float = 0.3  # waves are separated by a trough <= 30% of the smaller peak
    wave_min_gap: float = 180.0  # and peaks >= 3 h apart
    # step_jump
    step_share: float = 0.25
    step_min_age: float = 60.0
    step_onset_frac: float = 0.2  # the bin before the step is <= 20% of the step bin
    # early_spike
    spike_max_peak: float = 120.0
    spike_max_half_life: float = 180.0
    # delayed_takeoff
    takeoff_min_peak: float = 180.0
    quiet_window: float = 60.0
    quiet_frac: float = 0.2
    takeoff_min_rise_bins: int = 2
    # slow_burn
    burn_max_peak_to_mean: float = 3.0
    burn_min_t50: float = 360.0
    # flags
    ratio_threshold: float = 1.0
    ratio_min_replies: int = 20
    # Under pure Poisson arrivals (simulated, 300 seeds, 15-minute polling) a
    # 16-interval window at <= 0.15 fired on 0/300 14-hour and 2/300 48-hour
    # tracks; a 12-interval window at <= 0.2 fired on 50/300 48-hour tracks.
    drip_window: int = 16
    drip_min_mean: float = 3.0
    drip_max_dispersion: float = 0.15
    drip_min_age: float = 120.0
    drip_max_len_ratio: float = 1.25
    like_only_min_likes: float = 100.0
    like_only_max_other_frac: float = 0.01
    drop_frac: float = 0.05


@dataclass
class CurveFeatures:
    metric: str
    final: float
    duration_min: float
    n_snapshots: int
    t50_min: float | None
    t90_min: float | None
    early_share_60: float | None
    early_share_360: float | None
    peak_velocity: float  # per minute, on the dt grid
    t_peak_min: float | None  # centre of the peak bin
    half_life_min: float | None  # peak until velocity first falls to half of it
    peak_to_mean: float | None
    rise_bins: int | None
    n_waves: int
    wave_peaks_min: list[float] = field(default_factory=list)
    step_bins_min: list[float] = field(default_factory=list)  # start of each step bin
    max_bin_share: float = 0.0
    max_drop_share: float = 0.0


@dataclass
class Inflection:
    shape: str
    flags: list[str]
    features: CurveFeatures
    explained_by: list[str] = field(default_factory=list)  # amplifier handles matched to a step
    stats: dict = field(default_factory=dict)  # numbers behind each flag

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- curve building


def build_curve(created_at: datetime, snapshots: list[Snapshot], metric: str = "engagement") -> Curve:
    """Sort, attach the implicit zero at creation, and take the running max.

    Snapshots taken before creation or missing the metric are dropped. Two
    snapshots at the same minute keep the later one.
    """
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; expected one of {METRICS}")
    points: dict[float, float] = {0.0: 0.0}
    for s in sorted(snapshots, key=lambda s: s.observed_at):
        v = s.value(metric)
        minutes = (s.observed_at - created_at).total_seconds() / 60.0
        if v is None or minutes < 0:
            continue
        if minutes == 0:
            points[0.0] = v
            continue
        points[minutes] = v
    t = sorted(points)
    raw = [points[k] for k in t]
    y, best = [], 0.0
    for v in raw:
        best = max(best, v)
        y.append(best)
    return Curve(t=t, y=y, raw=raw, metric=metric)


def curve_from_event_times(created_at: datetime, event_times: list[datetime], metric: str = "replies") -> Curve:
    """A cumulative curve from the creation times of replies or quotes (decode their Snowflake IDs)."""
    minutes = sorted((e - created_at).total_seconds() / 60.0 for e in event_times)
    minutes = [m for m in minutes if m >= 0]
    t, y = [0.0], [0.0]
    for i, m in enumerate(minutes, 1):
        if m == t[-1]:
            y[-1] = float(i)
        else:
            t.append(m)
            y.append(float(i))
    return Curve(t=t, y=y, raw=list(y), metric=metric)


def _interp(t: list[float], y: list[float], x: float) -> float:
    if x <= t[0]:
        return y[0]
    if x >= t[-1]:
        return y[-1]
    lo, hi = 0, len(t) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if t[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = t[hi] - t[lo]
    return y[lo] + (y[hi] - y[lo]) * (x - t[lo]) / span


def to_grid(curve: Curve, dt: float) -> list[float]:
    """Increment per full bin of `dt` minutes, by linear interpolation of the cumulative curve.

    The partial bin at the end is dropped. Interpolation spreads a jump seen
    across a long polling gap evenly over that gap, so a step only shows up as
    a step when snapshots were taken at least every `dt` minutes around it.
    """
    n = int(curve.duration // dt + 1e-9)
    cum = [_interp(curve.t, curve.y, k * dt) for k in range(n + 1)]
    return [cum[k + 1] - cum[k] for k in range(n)]


def _time_to_share(curve: Curve, share: float) -> float | None:
    if curve.final <= 0:
        return None
    target = share * curve.final
    for i in range(1, len(curve.t)):
        if curve.y[i] >= target:
            y0, y1 = curve.y[i - 1], curve.y[i]
            if y1 == y0:
                return curve.t[i]
            return curve.t[i - 1] + (curve.t[i] - curve.t[i - 1]) * (target - y0) / (y1 - y0)
    return None


def find_waves(v: list[float], dt: float, p: InflectionParams) -> list[int]:
    """Indices of separate velocity peaks.

    A candidate is a bin >= wave_min_frac of the highest bin that rises from
    the bin before it (or is the first bin) and does not fall into a higher
    next bin. Candidates merge into the previous wave unless the lowest bin
    between them is <= trough_frac of the smaller peak and they are at least
    wave_min_gap minutes apart; a merged wave keeps the higher peak.
    """
    if not v:
        return []
    top = max(v)
    if top <= 0:
        return []
    h = p.wave_min_frac * top
    n = len(v)
    cands = [
        i
        for i in range(n)
        if v[i] >= h and (i == 0 or v[i] > v[i - 1]) and (i == n - 1 or v[i] >= v[i + 1])
    ]
    waves: list[int] = []
    for c in cands:
        if not waves:
            waves.append(c)
            continue
        w = waves[-1]
        between = v[w + 1 : c]
        trough = min(between) if between else math.inf
        separate = trough <= p.trough_frac * min(v[w], v[c]) and (c - w) * dt >= p.wave_min_gap
        if separate:
            waves.append(c)
        elif v[c] > v[w]:
            waves[-1] = c
    return waves


def find_steps(v: list[float], final: float, dt: float, p: InflectionParams) -> list[int]:
    """Bins after step_min_age holding >= step_share of the total that start abruptly and do not keep rising."""
    steps = []
    for j in range(1, len(v)):
        inc = v[j] * dt
        if final <= 0 or inc < p.step_share * final or j * dt < p.step_min_age:
            continue
        abrupt = v[j - 1] <= p.step_onset_frac * v[j]
        peaked = j == len(v) - 1 or v[j + 1] < v[j]
        if abrupt and peaked:
            steps.append(j)
    return steps


def features(curve: Curve, p: InflectionParams = InflectionParams()) -> CurveFeatures:
    dt = p.dt
    inc = to_grid(curve, dt)
    v = [x / dt for x in inc]
    final = curve.final
    peak = max(v) if v else 0.0
    i_p = v.index(peak) if v and peak > 0 else None

    half_life = None
    rise_bins = None
    if i_p is not None:
        for k in range(i_p + 1, len(v)):
            if v[k] <= peak / 2:
                half_life = (k - i_p) * dt
                break
        quiet = [j for j in range(i_p) if v[j] <= p.quiet_frac * peak]
        rise_bins = i_p - quiet[-1] if quiet else i_p + 1

    mean_v = final / (len(v) * dt) if v and final > 0 else None
    waves = find_waves(v, dt, p)
    steps = find_steps(v, final, dt, p)
    drops = [max(curve.raw[: i + 1]) - curve.raw[i] for i in range(len(curve.raw))]
    peak_raw = max(curve.raw) if curve.raw else 0.0
    return CurveFeatures(
        metric=curve.metric,
        final=final,
        duration_min=curve.duration,
        n_snapshots=len(curve.t),
        t50_min=_time_to_share(curve, 0.5),
        t90_min=_time_to_share(curve, 0.9),
        early_share_60=_interp(curve.t, curve.y, 60) / final if final > 0 and curve.duration >= 60 else None,
        early_share_360=_interp(curve.t, curve.y, 360) / final if final > 0 and curve.duration >= 360 else None,
        peak_velocity=peak,
        t_peak_min=(i_p + 0.5) * dt if i_p is not None else None,
        half_life_min=half_life,
        peak_to_mean=peak / mean_v if mean_v else None,
        rise_bins=rise_bins,
        n_waves=len(waves),
        wave_peaks_min=[(i + 0.5) * dt for i in waves],
        step_bins_min=[j * dt for j in steps],
        max_bin_share=max(inc) / final if inc and final > 0 else 0.0,
        max_drop_share=max(drops) / peak_raw if peak_raw > 0 else 0.0,
    )


# --------------------------------------------------------------------------- classification


def classify_shape(f: CurveFeatures, p: InflectionParams = InflectionParams()) -> str:
    if f.final < p.min_total or f.duration_min < p.min_duration or f.n_snapshots < p.min_snapshots:
        return "insufficient"
    if f.step_bins_min:
        return "step_jump"
    if f.n_waves >= 2:
        return "multi_wave"
    if f.t_peak_min is None:
        return "insufficient"
    if (
        f.t_peak_min >= p.takeoff_min_peak
        and f.early_share_60 is not None
        and f.final * f.early_share_60 / p.quiet_window <= p.quiet_frac * f.peak_velocity
        and (f.rise_bins or 0) >= p.takeoff_min_rise_bins
    ):
        return "delayed_takeoff"
    if f.t_peak_min <= p.spike_max_peak and f.half_life_min is not None and f.half_life_min <= p.spike_max_half_life:
        return "early_spike"
    if (
        f.peak_to_mean is not None
        and f.peak_to_mean <= p.burn_max_peak_to_mean
        and f.t50_min is not None
        and f.t50_min >= p.burn_min_t50
    ):
        return "slow_burn"
    return "unclassified"


def is_reply_ratio(likes: float, replies: float, quotes: float, p: InflectionParams = InflectionParams()) -> bool:
    return replies >= p.ratio_min_replies and (replies + quotes) >= p.ratio_threshold * max(likes, 1)


def min_dispersion(
    created_at: datetime, snapshots: list[Snapshot], metric: str, p: InflectionParams = InflectionParams()
) -> float | None:
    """Lowest variance/mean of raw per-interval increments over any run of `drip_window` intervals.

    Only runs after `drip_min_age`, with interval lengths within
    `drip_max_len_ratio` of each other and a mean increment >= `drip_min_mean`,
    are scored. A Poisson process gives about 1; organic engagement is burstier
    (above 1); a fixed-rate delivery gives near 0. Computed on raw snapshots,
    never the interpolated grid, because interpolation makes increments uniform.
    """
    pts = []
    for s in sorted(snapshots, key=lambda s: s.observed_at):
        v = s.value(metric)
        if v is not None:
            pts.append(((s.observed_at - created_at).total_seconds() / 60.0, v))
    best = None
    w = p.drip_window
    for i in range(len(pts) - w):
        run = pts[i : i + w + 1]
        if run[0][0] < p.drip_min_age:
            continue
        lens = [b[0] - a[0] for a, b in zip(run, run[1:])]
        if min(lens) <= 0 or max(lens) / min(lens) > p.drip_max_len_ratio:
            continue
        incs = [b[1] - a[1] for a, b in zip(run, run[1:])]
        mean = sum(incs) / w
        if mean < p.drip_min_mean:
            continue
        var = sum((x - mean) ** 2 for x in incs) / (w - 1)
        d = var / mean
        best = d if best is None else min(best, d)
    return best


def like_only_burst(snapshots: list[Snapshot], p: InflectionParams = InflectionParams()) -> bool:
    """Any interval between consecutive snapshots where likes rose >= like_only_min_likes and
    reposts + replies + quotes together rose by <= like_only_max_other_frac of that."""
    snaps = sorted(snapshots, key=lambda s: s.observed_at)
    for a, b in zip(snaps, snaps[1:]):
        d_likes = b.likes - a.likes
        d_other = (b.reposts - a.reposts) + (b.replies - a.replies) + (b.quotes - a.quotes)
        if d_likes >= p.like_only_min_likes and d_other <= p.like_only_max_other_frac * d_likes:
            return True
    return False


def detect(
    created_at: datetime,
    snapshots: list[Snapshot],
    metric: str = "engagement",
    params: InflectionParams = InflectionParams(),
    amplifiers: list[Amplifier] | None = None,
) -> Inflection:
    """Classify one post's curve and raise its flags. `amplifiers=None` means "not looked up"."""
    curve = build_curve(created_at, snapshots, metric)
    f = features(curve, params)
    shape = classify_shape(f, params)
    flags: list[str] = []
    stats: dict = {}

    last = max(snapshots, key=lambda s: s.observed_at) if snapshots else None
    if last and is_reply_ratio(last.likes, last.replies, last.quotes, params):
        flags.append("reply_ratio")
        stats["reply_like_ratio"] = (last.replies + last.quotes) / max(last.likes, 1)

    disp = min_dispersion(created_at, snapshots, metric, params)
    stats["min_dispersion"] = disp
    if disp is not None and disp <= params.drip_max_dispersion:
        flags.append("drip_feed")
    if like_only_burst(snapshots, params):
        flags.append("like_only_burst")
    if f.max_drop_share >= params.drop_frac:
        flags.append("count_drop")
        stats["max_drop_share"] = f.max_drop_share

    explained: list[str] = []
    if shape == "step_jump" and amplifiers is not None:
        for start in f.step_bins_min:
            lo = created_at + timedelta(minutes=start - params.dt)
            hi = created_at + timedelta(minutes=start + params.dt)
            explained += [a.handle for a in amplifiers if lo <= a.at <= hi]
        if not explained:
            flags.append("unexplained_step")
    return Inflection(shape=shape, flags=flags, features=f, explained_by=explained, stats=stats)


BOOST_FLAGS = {"drip_feed", "like_only_burst", "count_drop", "unexplained_step"}
