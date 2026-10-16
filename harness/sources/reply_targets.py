"""Reply-lane target discovery: fresh, fast-moving X posts where an early reply gets seen.

A two-follower account gets reach by replying near the top of a busy thread. A good target is
(1) moving fast now, (2) young, so the thread is still filling and still being shown, and
(3) not yet crowded with replies that bury ours. `score_target` multiplies those three.

Velocity comes from two snapshots of the same post when we have them (the previous run's counts
are kept in a small JSON file under out/), otherwise from the average rate since posting.
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from harness import config
from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, iso_utc, now_utc, parse_time
from harness.sources.watch import load_watch
from harness.sources.x_common import XAccessError, XPost, get_backend

# Uncalibrated guesses; nobody has measured reply visibility against post age for this account.
# Half-life of a post's attention, in minutes.
HALF_LIFE_MIN = 60.0
# Replies at which an early reply's visibility halves.
CROWD_REPLIES = 50.0
# Floor on age so a two-minute-old post with 3 likes doesn't read as 90 likes an hour.
MIN_AGE_MIN = 5.0
# A delta between snapshots closer than this is mostly noise.
MIN_SNAPSHOT_GAP_MIN = 2.0
SNAPSHOT_TTL = timedelta(days=2)


@dataclass
class Snapshot:
    observed_at: str
    engagement: int


@dataclass
class TargetScore:
    post: XPost
    age_minutes: float
    engagement_per_hour: float | None
    rate_from_delta: bool
    score: float


def engagement(post: XPost) -> int | None:
    """likes + reposts + replies + quotes, or None when the backend reported none of them."""
    counts = [post.likes, post.reposts, post.replies, post.quotes]
    if all(c is None for c in counts):
        return None
    return sum(c or 0 for c in counts)


def score_target(post: XPost, now: datetime, prev: Snapshot | None = None) -> TargetScore:
    created = parse_time(post.created_at)
    if created is None:
        raise ValueError(f"post {post.id} has no parseable created_at: {post.created_at!r}")
    age = max((now - created).total_seconds() / 60, 0.0)
    eng = engagement(post)
    rate, from_delta = None, False
    if eng is not None:
        prev_at = parse_time(prev.observed_at) if prev else None
        gap = (now - prev_at).total_seconds() / 60 if prev_at else 0.0
        if prev and gap >= MIN_SNAPSHOT_GAP_MIN:
            rate, from_delta = max(eng - prev.engagement, 0) / gap * 60, True
        else:
            rate = eng / max(age, MIN_AGE_MIN) * 60
    freshness = 0.5 ** (age / HALF_LIFE_MIN)
    crowding = 1 / (1 + (post.replies or 0) / CROWD_REPLIES)
    score = (rate or 0.0) * freshness * crowding
    return TargetScore(post, round(age, 1), None if rate is None else round(rate, 1), from_delta, score)


def rank_targets(
    posts: list[XPost],
    now: datetime,
    snapshots: dict[str, Snapshot],
    max_age_minutes: float,
    exclude_authors: set[str] = frozenset(),
) -> list[TargetScore]:
    """Score posts, drop replies, stale posts and excluded authors, best first. Dedupes by id."""
    excluded = {a.lower() for a in exclude_authors}
    best: dict[str, TargetScore] = {}
    for post in posts:
        if post.is_reply or post.author.lower() in excluded:
            continue
        scored = score_target(post, now, snapshots.get(post.id))
        if scored.age_minutes > max_age_minutes:
            continue
        if post.id not in best or scored.score > best[post.id].score:
            best[post.id] = scored
    return sorted(best.values(), key=lambda t: t.score, reverse=True)


def on_topic(post: XPost, keywords: list[str]) -> bool:
    text = post.text.lower()
    return any(k.lower() in text for k in keywords)


def target_signal(t: TargetScore) -> Signal:
    metrics = t.post.metrics()
    metrics.update(age_minutes=t.age_minutes, target_score=round(t.score, 2), rate_from_delta=int(t.rate_from_delta))
    if t.engagement_per_hour is not None:
        metrics["engagement_per_hour"] = t.engagement_per_hour
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
        if eng is not None and post.metrics_verified:
            snaps[post.id] = Snapshot(observed_at=iso_utc(now), engagement=eng)
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
                    got = searcher.search(query, since, limit=watch.x.per_query)
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
        ranked = rank_targets(posts, now, snaps, cfg.max_age_minutes, exclude)
        save_snapshots(path, snaps, posts, now)
        return [target_signal(t) for t in ranked[: cfg.limit]]
