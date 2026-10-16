"""Fetch current X metrics for every dataset post, and author timelines.

Writes append-only JSONL so an interrupted run resumes where it stopped:

- `data/context/x_metrics.jsonl`: one line per dataset post, the `XPost` as
  seen at `fetched_at`, or `{"id", "error"}` when the fetch failed (a 404
  usually means the post was deleted, which is itself a signal).
- `data/context/author_timelines.jsonl`: recent posts per dataset account,
  used for an author baseline that is not distorted by the dataset dropping
  every post with 11-99 engagement.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from harness import config
from harness.context import snowflake
from harness.context.http import FetchError, PoliteClient
from harness.context.settings import DEFAULT
from harness.context.xreader import FxTwitterReader, XReader

DATASET = config.ROOT / "data" / "devtools_virality_30d.csv"
CONTEXT_DIR = config.ROOT / "data" / "context"
X_METRICS = CONTEXT_DIR / "x_metrics.jsonl"
TIMELINES = CONTEXT_DIR / "author_timelines.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_dataset(path: Path = DATASET) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def backfill_posts(reader: XReader, rows: list[dict], out: Path = X_METRICS, limit: int | None = None) -> int:
    done = {r["id"] for r in read_jsonl(out)}
    todo = [str(snowflake.status_id_from_url(r["Post URL"])) for r in rows]
    todo = [i for i in todo if i not in done][:limit]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for n, post_id in enumerate(todo, 1):
            try:
                rec = reader.status(post_id).to_dict()
            except FetchError as e:
                rec = {"id": post_id, "error": str(e), "fetched_at": _now()}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            if n % 50 == 0:
                print(f"[backfill] posts {n}/{len(todo)}")
    return len(todo)


def backfill_timelines(
    reader: XReader, handles: list[str], since: datetime, out: Path = TIMELINES, max_posts: int = 400
) -> int:
    """Page each account's timeline back to `since`. One JSONL line per post per account."""
    done = {r["timeline_of"] for r in read_jsonl(out)}
    fetched = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for handle in handles:
            h = handle.lstrip("@")
            if h in done:
                continue
            try:
                posts = _timeline_since(reader, h, since, max_posts)
            except FetchError as e:
                f.write(json.dumps({"timeline_of": h, "error": str(e), "fetched_at": _now()}) + "\n")
                continue
            for p in posts:
                f.write(json.dumps({"timeline_of": h, **p.to_dict()}) + "\n")
            if not posts:
                f.write(json.dumps({"timeline_of": h, "error": "empty timeline", "fetched_at": _now()}) + "\n")
            f.flush()
            fetched += 1
            print(f"[backfill] timeline @{h}: {len(posts)} posts")
    return fetched


def _timeline_since(reader: XReader, handle: str, since: datetime, max_posts: int):
    # Grow the request until the oldest original post is older than `since`.
    # The pinned post comes first and can be months old, so skip it when judging.
    limit = 40
    while True:
        posts = reader.profile_statuses(handle, limit=limit)
        originals = [p for p in posts if not p.is_repost]
        reached = originals and min(p.created_at for p in originals[1:] or originals) < since
        if reached or len(posts) < limit or limit >= max_posts:
            return posts
        limit = min(limit * 2, max_posts)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(limit: int | None = None, timelines: bool = True, interval_s: float = DEFAULT.fetch.bulk_interval_s) -> None:
    rows = load_dataset()
    # The bulk job runs at 0.5 req/s because Observe shares this IP's fxtwitter budget.
    # max_age_s=None reuses cached responses, so reruns do not refetch.
    reader = FxTwitterReader(PoliteClient(min_interval_s=interval_s), max_age_s=None)
    n = backfill_posts(reader, rows, limit=limit)
    print(f"[backfill] fetched {n} posts -> {X_METRICS}")
    if timelines:
        handles = sorted({r["Account"] for r in rows})
        since = datetime(2026, 9, 14, tzinfo=timezone.utc)
        m = backfill_timelines(reader, handles, since)
        print(f"[backfill] fetched {m} timelines -> {TIMELINES}")
