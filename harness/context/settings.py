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
    # Dataset jobs. 2.0 s left room for Observe on the same IP before the throttle was shared across
    # processes; with the shared timestamp file, every caller of this client together stays at 1 req/s.
    bulk_interval_s: float = 2.0
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
    # paid_or_external_reach (research report, uncalibrated): views >= 5x the author's median
    # views and engagement per view <= 0.4x the author's median, for authors with >= 5 posts.
    reach_views_ratio: float = 5.0
    reach_conversion_ratio: float = 0.4
    reach_min_author_posts: int = 5
    min_reply_coverage: float = 0.8  # reply timing votes only on samples covering this share of replies
    staff_share: float = 0.2  # coordinated_authentic: >= 20% of sampled reposters or repliers name the brand
    staff_min_sample: int = 10
    new_account_share: float = 0.5


@dataclass(frozen=True)
class EnrichParams:
    domain: str = "devtools"  # key into data/context/lexicons.json
    # Eval research measured 27% of posts within 180 s of the account's previous post, 4% of them viral.
    thread_gap_s: float = 180
    mature_h: float = 48  # eval research: viral rate 19% at 0-12 h of age vs 42% at 2-7 days
    timeline_min_posts: int = 10  # fewer timeline posts than this falls back to the dataset median
    big_account: int = 10_000  # a replier this large counts toward reply_big_accounts
    # David's CSV was committed at this time (git log c9f8904); its counts are from just before.
    scrape_time_iso: str = "2026-10-16T01:07:58+00:00"
    prior_min_posts: int = 5  # pre_author_prior_median_eng needs this many earlier posts
    prev_window_h: float = 2.0  # pre_author_posts_prev{h}h: X decays repeat posts by one author in a batch
    min_reply_coverage: float = 0.8  # reply timing stats only when the sample covers this share of replies
    new_account_days: int = 90  # a reposter who joined this recently counts as a new account
    weights: tuple[float, float, float, float] = (0.66, 1.0, 5.0, 5.0)  # like, repost, reply, quote: X 2026 ranker


@dataclass(frozen=True)
class TrackerParams:
    # Log-spaced ages (minutes): early speed predicts final size (Szabo and Huberman 2010;
    # Cheng et al. 2014), so the early points matter most. 10 requests per post per week.
    log_schedule: tuple[float, ...] = (5, 15, 30, 60, 120, 240, 480, 1440, 2880, 10080)
    # Dense mode, (until this age, poll every this many minutes): about 168 requests per post,
    # but the only mode fine enough for step_jump detection on the 15-minute curve grid.
    dense: bool = False
    segments: tuple[tuple[float, float], ...] = ((60, 5), (1440, 15), (4320, 60), (10080, 360))
    max_start_age_h: float = 6.0  # watch only starts tracking posts younger than this
    watch_every_min: int = 15
    amplifier_min_followers: int = 100_000
    loop_interval_s: float = 60.0
    reply_ks: tuple[int, ...] = (1, 5, 10)  # report the minutes until the k-th reply
    # X's author_cold_start gives posts by authors with <= 50k followers, under 2 h old, a
    # mid-feed slot sampled from their like-per-view rate (xai-org/x-algorithm, per the research report).
    cold_start_max_followers: int = 50_000
    cold_start_window_min: float = 120.0

    def schedule(self) -> list[float]:
        if not self.dense:
            return [float(a) for a in self.log_schedule]
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
