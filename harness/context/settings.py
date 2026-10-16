"""Every tunable in harness/context, in one typed object.

Defaults are the values the code ships with. None has been fitted to labelled
outcomes; where a value came from a measurement, the comment says which. A
tuning loop can build `ContextConfig(boost=BoostParams(min_signals=3))` and
pass it down instead of editing constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.context.curves import InflectionParams


@dataclass(frozen=True)
class FetchParams:
    default_interval_s: float = 1.0  # per host, shared across processes (http.THROTTLE_DIR)
    bulk_interval_s: float = 2.0  # dataset backfill: Observe shares this IP's fxtwitter budget
    gdelt_interval_s: float = 6.0  # GDELT asks for >= 5 s
    trends_interval_s: float = 4.0  # Google Trends explore flow worked at 3-4 s
    timeout_s: float = 25.0
    max_retries: int = 3


@dataclass(frozen=True)
class BoostParams:
    ratio_high_q: float = 0.99  # a ratio fires above this dataset quantile ...
    ratio_low_q: float = 0.01  # ... or below this one
    min_rows_for_cutoff: int = 50
    like_rate_min_views: float = 2000
    repost_min_likes: float = 50
    bookmarks_min_likes: float = 200
    replies_min_likes: float = 200
    author_outlier: float = 20.0  # engagement >= 20x the author's timeline median
    reply_min_sample: int = 10
    low_follower: int = 20
    low_follower_share: float = 0.5
    dup_reply_share: float = 0.2
    burst_window_s: float = 120
    burst_share: float = 0.4
    min_signals: int = 2


@dataclass(frozen=True)
class EnrichParams:
    domain: str = "devtools"  # key into data/context/lexicons.json
    # Eval research measured 27% of posts within 180 s of the account's previous post, 4% of them viral.
    thread_gap_s: float = 180
    mature_h: float = 48  # eval research: viral rate 19% at 0-12 h of age vs 42% at 2-7 days
    timeline_min_posts: int = 10  # fewer timeline posts than this falls back to the dataset median
    big_account: int = 10_000  # a replier this large counts toward reply_big_accounts


@dataclass(frozen=True)
class TrackerParams:
    # (until this age in minutes, poll every this many minutes)
    segments: tuple[tuple[float, float], ...] = ((60, 5), (1440, 15), (4320, 60), (10080, 360))
    max_start_age_h: float = 6.0  # watch only starts tracking posts younger than this
    watch_every_min: int = 15
    amplifier_min_followers: int = 100_000
    loop_interval_s: float = 60.0

    def schedule(self) -> list[float]:
        ages, start = [], 0.0
        for until, step in self.segments:
            a = start + step
            while a <= until + 1e-9:
                ages.append(a)
                a += step
            start = until
        return ages


@dataclass(frozen=True)
class ContextConfig:
    inflection: InflectionParams = field(default_factory=InflectionParams)
    boost: BoostParams = field(default_factory=BoostParams)
    enrich: EnrichParams = field(default_factory=EnrichParams)
    tracker: TrackerParams = field(default_factory=TrackerParams)
    fetch: FetchParams = field(default_factory=FetchParams)


DEFAULT = ContextConfig()
