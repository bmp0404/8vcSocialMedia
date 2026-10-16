"""Reply-lane target discovery: fresh, fast-moving X posts where an early reply gets seen.

A reply from an account the viewer doesn't follow never reaches For You: X's home mixer drops
out-of-network replies (`OONRetweetReplyFilter`, xai-org/x-algorithm at 92f0918). A two-follower
account's reply is seen only inside the parent's thread, so the parent's live traffic is the
whole opportunity. A good target is (1) drawing traffic now, (2) young, so the thread is still
being shown, (3) not yet crowded with replies that bury ours, and (4) from an account in the
size band where a small account's reply isn't filtered or buried. `score_target` multiplies them.

Traffic is views per hour when the backend reports views (fxtwitter and the X API do; views move
faster and more smoothly than likes), otherwise engagements per hour. Each comes from the delta
between this run's counts and the previous run's (kept in a small JSON file under out/), or the
average since posting when there is no earlier snapshot. Posts scored on views and posts scored
on engagement aren't on one scale, which only happens when backends are mixed in one run.
"""

import json
import os
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from harness import config
from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, iso_utc, now_utc, parse_time
from harness.sources.knobs import ObserveKnobs, current
from harness.sources.watch import load_watch
from harness.sources.x_common import XAccessError, XPost, get_backend
from harness.sources.x_fx import FxXBackend, _reader, thread_replies, verify_counts

# Storage housekeeping, not a tunable: snapshots older than this can't give a useful delta.
SNAPSHOT_TTL = timedelta(days=2)


@dataclass
class Snapshot:
    observed_at: str
    engagement: int | None = None
    views: int | None = None


@dataclass
class TargetScore:
    post: XPost
    age_minutes: float
    engagement_per_hour: float | None
    views_per_hour: float | None
    rate_from_delta: bool
    size_band: float
    score: float


def engagement(post: XPost) -> int | None:
    """likes + reposts + replies + quotes, or None when the backend reported none of them."""
    counts = [post.likes, post.reposts, post.replies, post.quotes]
    if all(c is None for c in counts):
        return None
    return sum(c or 0 for c in counts)


def size_band(followers: int | None, age_minutes: float, replies: int, k: ObserveKnobs | None = None) -> float:
    """Weight for the parent author's size, from the bands in ObserveKnobs. 1.0 when followers are unknown."""
    k = k or current()
    if followers is None:
        return 1.0
    if k.mid_account <= followers <= k.big_account:
        return 1.0
    if k.small_account <= followers < k.mid_account:
        return k.small_band
    if followers > k.big_account and age_minutes < k.giant_window_min and replies < k.giant_max_replies:
        return k.giant_band
    return 0.0


def _per_hour(
    now_count: int | None, prev_count: int | None, gap_min: float, age_min: float, k: ObserveKnobs
) -> tuple[float | None, bool]:
    if now_count is None:
        return None, False
    if prev_count is not None and gap_min >= k.min_snapshot_gap_min:
        return max(now_count - prev_count, 0) / gap_min * 60, True
    return now_count / max(age_min, k.min_age_min) * 60, False


def score_target(post: XPost, now: datetime, prev: Snapshot | None = None, k: ObserveKnobs | None = None) -> TargetScore:
    k = k or current()
    created = parse_time(post.created_at)
    if created is None:
        raise ValueError(f"post {post.id} has no parseable created_at: {post.created_at!r}")
    age = max((now - created).total_seconds() / 60, 0.0)
    prev_at = parse_time(prev.observed_at) if prev else None
    gap = (now - prev_at).total_seconds() / 60 if prev_at else 0.0
    eng_rate, eng_delta = _per_hour(engagement(post), prev.engagement if prev else None, gap, age, k)
    view_rate, view_delta = _per_hour(post.views, prev.views if prev else None, gap, age, k)
    traffic = view_rate if view_rate is not None else eng_rate
    replies = post.replies or 0
    band = size_band(post.author_followers, age, replies, k)
    freshness = 0.5 ** (age / k.target_half_life_min)
    crowding = (1 + replies) ** -k.crowding_exponent
    score = (traffic or 0.0) * freshness * crowding * band
    return TargetScore(
        post,
        round(age, 1),
        None if eng_rate is None else round(eng_rate, 1),
        None if view_rate is None else round(view_rate, 1),
        view_delta if view_rate is not None else eng_delta,
        band,
        score,
    )


def rank_targets(
    posts: list[XPost],
    now: datetime,
    snapshots: dict[str, Snapshot],
    max_age_minutes: float,
    exclude_authors: set[str] = frozenset(),
    k: ObserveKnobs | None = None,
) -> list[TargetScore]:
    """Best targets first. Drops replies, stale posts, excluded authors and zero size bands;
    dedupes by id; keeps at most k.max_per_author per author.

    Replies are dropped, so every target is the root of its own conversation, and two targets
    can never share a root.
    """
    k = k or current()
    excluded = {a.lower() for a in exclude_authors}
    best: dict[str, TargetScore] = {}
    for post in posts:
        if post.is_reply or post.author.lower() in excluded:
            continue
        scored = score_target(post, now, snapshots.get(post.id), k)
        if scored.age_minutes > max_age_minutes or scored.size_band == 0:
            continue
        if post.id not in best or scored.score > best[post.id].score:
            best[post.id] = scored
    per_author: Counter = Counter()
    ranked = []
    for t in sorted(best.values(), key=lambda t: t.score, reverse=True):
        per_author[t.post.author.lower()] += 1
        if per_author[t.post.author.lower()] <= k.max_per_author:
            ranked.append(t)
    return ranked


def on_topic(post: XPost, keywords: list[str]) -> bool:
    text = post.text.lower()
    return any(k.lower() in text for k in keywords)


def target_signal(t: TargetScore) -> Signal:
    metrics = t.post.metrics()
    metrics.update(
        age_minutes=t.age_minutes,
        target_score=round(t.score, 2),
        rate_from_delta=int(t.rate_from_delta),
        size_band=t.size_band,
    )
    if t.engagement_per_hour is not None:
        metrics["engagement_per_hour"] = t.engagement_per_hour
    if t.views_per_hour is not None:
        metrics["views_per_hour"] = t.views_per_hour
    return Signal(
        source="x_targets",
        text=t.post.text,
        url=t.post.url,
        author=t.post.author,
        created_at=t.post.created_at,
        metrics=metrics,
        kind="reply_target",
    )


def snapshot_path() -> Path:
    return Path(os.getenv("HARNESS_X_SNAPSHOTS") or config.OUT_DIR / "x_snapshots.json")


def load_snapshots(path: Path, now: datetime) -> dict[str, Snapshot]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    snaps = {pid: Snapshot(**s) for pid, s in raw.items()}
    return {pid: s for pid, s in snaps.items() if now - parse_time(s.observed_at) < SNAPSHOT_TTL}


def save_snapshots(path: Path, snaps: dict[str, Snapshot], posts: list[XPost], now: datetime) -> None:
    for post in posts:
        eng = engagement(post)
        if post.metrics_verified and (eng is not None or post.views is not None):
            snaps[post.id] = Snapshot(observed_at=iso_utc(now), engagement=eng, views=post.views)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: asdict(v) for k, v in snaps.items()}, indent=1), encoding="utf-8")


class ReplyTargetSource(Source):
    """Fresh posts from watched accounts (and top search results) ranked by reply opportunity."""

    name = "x_targets"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch()
        cfg = watch.reply_targets
        now = now_utc()
        since = now - timedelta(minutes=cfg.max_age_minutes)
        posts: list[XPost] = []
        errors: list[str] = []

        handles = [(h, False) for h in cfg.accounts] + [(h, True) for h in cfg.broad_accounts]
        timelines = None
        if handles:
            try:
                timelines = get_backend("user_posts")
            except XAccessError as e:
                timelines = None
                errors.append(f"timelines skipped: {e}")
            for handle, broad in handles if timelines else []:
                try:
                    got = timelines.user_posts(handle, since, limit=10)
                except SourceError as e:
                    errors.append(f"@{handle}: {e}")
                    continue
                posts.extend(p for p in got if not broad or on_topic(p, cfg.keywords))
        if cfg.include_search and watch.x.queries:
            try:
                searcher = get_backend("search")
                for query in watch.x.queries:
                    got = verify_counts(searcher.search(query, since, limit=watch.x.per_query))
                    posts.extend(p for p in got if not cfg.keywords or on_topic(p, cfg.keywords))
            except XAccessError as e:
                errors.append(f"search skipped: {e}")
            except SourceError as e:
                errors.append(f"search: {e}")
        for err in errors:
            print(f"[observe] {self.name}: {err}")
        if not posts and errors:
            raise SourceError(f"{self.name}: every read failed; first error: {errors[0]}")

        path = snapshot_path()
        snaps = load_snapshots(path, now)
        exclude = {watch.brand_handle} if watch.brand_handle else set()
        k = current()
        ranked = rank_targets(posts, now, snaps, cfg.max_age_minutes, exclude, k)
        save_snapshots(path, snaps, posts, now)
        signals = [target_signal(t) for t in ranked[: cfg.limit]]
        if k.thread_replies and signals:
            self._attach_threads(signals, timelines if isinstance(timelines, FxXBackend) else None, k.thread_replies)
        return signals

    def _attach_threads(self, signals: list[Signal], fx: FxXBackend | None, limit: int) -> None:
        """Give each target its thread's top replies. A failure leaves that target's thread empty."""
        try:
            reader = fx.reader if fx else _reader()
        except XAccessError as e:
            print(f"[observe] {self.name}: threads skipped: {e}")
            return
        for s in signals:
            try:
                s.thread = thread_replies(reader, s.url.rsplit("/", 1)[-1], limit)
            except SourceError as e:
                print(f"[observe] {self.name}: no thread for {s.url}: {e}")
