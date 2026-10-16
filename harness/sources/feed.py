"""Teammate data feed: X posts from a scraper whose exact output format we don't control.

Enable with HARNESS_SOURCES=feed and point HARNESS_FEED_PATH at a file, a glob
(`data/scrape/*.jsonl`), or a directory; .json, .jsonl/.ndjson and .csv are read.

A .json file may be a list of posts or an object wrapping one (`data`, `tweets`, `posts`,
`items`, `results`, `statuses`), including the raw X API v2 shape (`includes.users` is used to
resolve `author_id`). Field names are matched loosely (see FIELD_ALIASES); rows with no text are
skipped and counted. The teammate's spreadsheet export (`data/devtools_virality_30d.csv`: Category,
Account, Post Content, Media, Likes, Replies, Retweets, Engagement, Post URL, Date, Segment) maps
directly; the tweet id is parsed from the URL's `/status/<id>` when there's no id column, and
Category / Segment / Media ride along in `metrics` (`category`, `segment`, `media`).

Big exports can be trimmed per run (newest first, applied after de-duplication):
  HARNESS_FEED_SINCE_DAYS   keep posts created within the last N days (undated posts are dropped)
  HARNESS_FEED_LIMIT        keep the N most recent posts
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from harness import config
from harness.models import Signal
from harness.sources.base import Source
from harness.sources.x_api import reply_score

EXTS = {".json", ".jsonl", ".ndjson", ".csv"}
WRAPPER_KEYS = ("data", "tweets", "posts", "items", "results", "statuses", "records")

FIELD_ALIASES = {
    "id": ("id", "tweet_id", "id_str", "rest_id", "post_id"),
    "text": ("text", "full_text", "content", "body", "tweet", "note_tweet.text", "legacy.full_text",
             "Post Content", "post_content", "Text", "Content"),
    "author": (
        "author", "username", "handle", "screen_name", "Account", "account", "user.username", "user.screen_name",
        "author.username", "author.screen_name", "author.handle", "user_screen_name", "author_handle",
        "core.user_results.result.legacy.screen_name",
    ),
    "url": ("url", "tweet_url", "link", "permalink", "Post URL", "post_url", "URL"),
    "created_at": ("created_at", "createdAt", "date", "Date", "timestamp", "time", "legacy.created_at"),
    "conversation_id": ("conversation_id", "conversation_id_str", "conversationId", "thread_id"),
    "followers": (
        "author_followers", "followers_count", "user.followers_count", "user.public_metrics.followers_count",
        "author.followers_count", "author.public_metrics.followers_count",
    ),
}

# Our metric name -> accepted source names (public_metrics or flat).
METRIC_ALIASES = {
    "likes": ("like_count", "likes", "Likes", "favorite_count", "favorites", "likeCount"),
    "reposts": ("retweet_count", "retweets", "Retweets", "reposts", "repost_count", "retweetCount"),
    "replies": ("reply_count", "replies", "Replies", "replyCount"),
    "quotes": ("quote_count", "quotes", "quoteCount"),
    "bookmarks": ("bookmark_count", "bookmarks", "bookmarkCount"),
    "views": ("impression_count", "impressions", "views", "view_count", "viewCount"),
    "engagement": ("engagement", "Engagement", "total_engagement"),
}
# Non-numeric spreadsheet columns kept in metrics as strings.
EXTRA_FIELDS = {"category": ("Category", "category"), "segment": ("Segment", "segment"),
                "media": ("Media", "media")}
STATUS_ID = re.compile(r"/status(?:es)?/(\d+)")


def _dig(row: Any, dotted: str) -> Any:
    cur = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first(row: dict, field: str) -> Any:
    for key in FIELD_ALIASES[field]:
        v = _dig(row, key)
        if isinstance(v, dict):  # e.g. "author": {"username": ...} handled by the dotted aliases
            continue
        if v not in (None, ""):
            return v
    return None


def _num(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(str(v).replace(",", "").strip()))
    except ValueError:
        return None


def _metrics(row: dict) -> dict:
    pm = row.get("public_metrics") or row.get("metrics") or {}
    pm = pm if isinstance(pm, dict) else {}
    legacy = row.get("legacy") if isinstance(row.get("legacy"), dict) else {}
    out = {}
    for ours, keys in METRIC_ALIASES.items():
        for k in keys:
            v = next((_num(src[k]) for src in (pm, row, legacy) if k in src and _num(src[k]) is not None), None)
            if v is not None:
                out[ours] = v
                break
    # x_api-compatible names so reply_score and memory.record understand them too
    for ours, raw in (("likes", "like_count"), ("reposts", "retweet_count"), ("replies", "reply_count"),
                      ("quotes", "quote_count"), ("views", "impression_count")):
        out[raw] = out.get(ours, 0)
    for ours, keys in EXTRA_FIELDS.items():
        v = next((row[k] for k in keys if row.get(k) not in (None, "")), None)
        if v is not None and not isinstance(v, (dict, list)):
            out[ours] = str(v).strip()
    return out


def _paths(spec: str) -> list[Path]:
    p = Path(spec)
    if not p.is_absolute():
        p = config.ROOT / p
    if p.is_dir():
        return sorted(f for f in p.rglob("*") if f.suffix.lower() in EXTS)
    if any(c in spec for c in "*?["):
        return sorted(Path(f) for f in glob.glob(str(p), recursive=True) if Path(f).suffix.lower() in EXTS)
    return [p] if p.exists() else []


def _rows_json(data: Any) -> Iterable[dict]:
    users: dict[str, dict] = {}
    if isinstance(data, dict):
        users = {u.get("id"): u for u in (data.get("includes") or {}).get("users", []) if isinstance(u, dict)}
        for k in WRAPPER_KEYS:
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            data = [data]
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        if users and row.get("author_id") in users and "user" not in row:
            row = {**row, "user": users[row["author_id"]]}
        yield row


def _read(path: Path) -> Iterable[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)
    elif suffix in {".jsonl", ".ndjson"}:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line:
                try:
                    yield from _rows_json(json.loads(line))
                except json.JSONDecodeError:
                    yield {}  # counted as skipped
    else:
        yield from _rows_json(json.loads(path.read_text(encoding="utf-8-sig")))


def to_signal(row: dict) -> Signal | None:
    """Map one scraped row to a Signal, or None if it has no text."""
    text = _first(row, "text")
    if not isinstance(text, str) or not text.strip():
        return None
    sid = _first(row, "id")
    url = _first(row, "url")
    if sid is None and url and (m := STATUS_ID.search(str(url))):
        sid = m.group(1)
    sid = str(sid) if sid is not None else None
    author = _first(row, "author")
    author = str(author).strip().lstrip("@") if author else None
    if not url and sid:
        url = f"https://x.com/{author or 'i/web'}/status/{sid}"
    created = _first(row, "created_at")
    conv = _first(row, "conversation_id")
    metrics = _metrics(row)
    followers = _num(_first(row, "followers"))
    if followers is not None:
        metrics["author_followers"] = followers
    metrics["reply_score"] = reply_score(metrics, str(created) if created else None)
    return Signal(
        source="x",
        text=text.strip(),
        id=sid,
        conversation_id=str(conv) if conv else sid,
        url=url,
        author=f"@{author}" if author else None,
        created_at=str(created) if created else None,
        metrics=metrics,
    )


def _created(s: Signal) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(s.created_at).replace("Z", "+00:00")) if s.created_at else None
    except ValueError:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts and ts.tzinfo is None else ts


def _env_int(name: str) -> int | None:
    v = (os.getenv(name) or "").strip()
    return int(v) if v else None


def trim(signals: list[Signal], since_days: int | None = None, limit: int | None = None) -> list[Signal]:
    """Newest first; keep posts from the last `since_days` days, then at most `limit` of them."""
    if since_days is None and limit is None:
        return signals
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    out = sorted(signals, key=lambda s: _created(s) or epoch, reverse=True)
    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        out = [s for s in out if (ts := _created(s)) and ts >= cutoff]
    return out[:limit] if limit is not None else out


class FeedSource(Source):
    """X posts from a teammate's scraper output (HARNESS_FEED_PATH)."""

    name = "feed"

    def fetch(self, mission: str) -> list[Signal]:
        spec = config.FEED_PATH.strip()
        if not spec:
            raise RuntimeError("set HARNESS_FEED_PATH (file, glob or directory) to use the feed source")
        paths = _paths(spec)
        if not paths:
            raise RuntimeError(f"HARNESS_FEED_PATH matched no .json/.jsonl/.csv files: {spec}")
        signals, seen, skipped, dupes = [], set(), 0, 0
        for path in paths:
            try:
                rows = list(_read(path))
            except (OSError, ValueError, csv.Error) as e:
                print(f"[feed] could not read {path.name}: {e}")
                continue
            for row in rows:
                s = to_signal(row) if isinstance(row, dict) else None
                if not s:
                    skipped += 1
                    continue
                key = s.id or s.url or s.text
                if key in seen:
                    dupes += 1
                    continue
                seen.add(key)
                signals.append(s)
        total = len(signals)
        since, limit = _env_int("HARNESS_FEED_SINCE_DAYS"), _env_int("HARNESS_FEED_LIMIT")
        signals = trim(signals, since, limit)
        rules = [f"since {since}d"] * (since is not None) + [f"limit {limit}"] * (limit is not None)
        kept = f"; kept the {len(signals)} most recent ({', '.join(rules)})" if rules else ""
        print(f"[feed] loaded {total} posts from {len(paths)} file(s); "
              f"skipped {skipped} (no text or unparseable), {dupes} duplicates{kept}")
        signals.sort(key=lambda s: s.metrics.get("reply_score", 0), reverse=True)
        return signals
