"""Load the raw snapshot into one deduplicated list of posts and replies.

Every source row is an fxtwitter status read. The same status shows up in several files (a dataset
post is also on its author's timeline; an author's reply to their own post is also in the reply
sample), so items are keyed by status ID and merged. Counts come from the most recent read.
"""

import csv
import gzip
import io
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.corpus.snapshot import RAW_DIR

TWITTER_EPOCH_MS = 1288834974657
# A post's counts are treated as settled 48 hours after it went out, the context worker's rule
# (data/context/DESIGN.md on sam/context-dynamics); here it is measured at the read that gave the counts.
MATURE_AFTER = timedelta(hours=48)
SOURCES = ("x_metrics", "author_timelines", "reply_samples")
METRIC_KEYS = ("likes", "reposts", "replies", "quotes", "bookmarks", "views")


def created_at(status_id: str | int) -> datetime:
    """A status ID is a Snowflake: the top bits are milliseconds since X's epoch."""
    return datetime.fromtimestamp(((int(status_id) >> 22) + TWITTER_EPOCH_MS) / 1000, tz=timezone.utc)


@dataclass
class Item:
    status_id: str
    kind: str  # "post" or "reply"
    author: str
    text: str
    created_at: datetime
    author_followers: int | None
    author_verified: bool | None
    author_description: str | None
    parent_id: str | None  # the status this reply answers; None for posts
    parent_author: str | None
    quoted_id: str | None
    media_types: list[str]
    n_links: int
    n_mentions: int
    lang: str | None
    metrics: dict  # likes, reposts, replies, quotes, bookmarks, views, fetched_at
    sources: list[str]
    reposted_by: list[str] = field(default_factory=list)  # timeline owners who reposted it
    thread_root: str | None = None  # the dataset post whose reply sample held this reply
    thread_coverage: float | None = None  # unique replies sampled / the root's reply count
    in_dataset: bool = False  # a row of data/devtools_virality_30d.csv
    is_thread_continuation: bool = False
    boost_suspect: bool | None = None  # None: no boost check ran on this post

    @property
    def item_id(self) -> str:
        return f"x:{self.status_id}"

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author}/status/{self.status_id}"

    @property
    def fetched_at(self) -> datetime:
        return datetime.fromisoformat(self.metrics["fetched_at"])

    @property
    def mature(self) -> bool:
        return self.fetched_at - self.created_at >= MATURE_AFTER

    @property
    def engagement(self) -> int:
        """likes + reposts + replies + quotes, the dataset's Engagement column."""
        m = self.metrics
        return m["likes"] + m["reposts"] + m["replies"] + m["quotes"]

    @property
    def has_media(self) -> bool:
        return bool(self.media_types)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["item_id"], d["url"], d["mature"] = self.item_id, self.url, self.mature
        return d


@dataclass
class Corpus:
    items: list[Item]
    skipped: Counter  # source -> rows with no status (fetch errors, empty-thread markers)
    rows: Counter  # source -> status rows read

    def __post_init__(self):
        self.by_id = {i.status_id: i for i in self.items}

    def __len__(self) -> int:
        return len(self.items)

    def get(self, status_id: str) -> Item | None:
        return self.by_id.get(str(status_id))

    def counts(self) -> dict:
        by_kind = Counter(i.kind for i in self.items)
        by_source = Counter(s for i in self.items for s in i.sources)
        by_kind_source = Counter(f"{i.kind}/{'+'.join(i.sources)}" for i in self.items)
        return {"items": len(self.items), "by_kind": dict(by_kind), "by_source": dict(by_source),
                "by_kind_and_sources": dict(sorted(by_kind_source.items())),
                "rows_read": dict(self.rows), "rows_skipped": dict(self.skipped),
                "thread_continuations": sum(i.is_thread_continuation for i in self.items),
                "reposted_only": sum(1 for i in self.items if i.reposted_by and i.sources == ["author_timelines"])}


def _read_jsonl(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path.name} line {n} is not JSON: {e}") from e


def _read_csv(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(io.StringIO(f.read())))


def _truthy(v: str | None) -> bool | None:
    if v in (None, ""):
        return None
    if v not in ("True", "False"):
        raise ValueError(f"expected True or False, got {v!r}")
    return v == "True"


def _new_item(row: dict, source: str) -> Item:
    sid = str(row["id"])
    parent = row.get("replying_to_status")
    return Item(
        status_id=sid,
        kind="reply" if parent or row.get("replying_to") else "post",
        author=row["author_handle"],
        text=row.get("text") or "",
        created_at=created_at(sid),
        author_followers=row.get("author_followers"),
        author_verified=row.get("author_verified"),
        author_description=row.get("author_description"),
        parent_id=str(parent) if parent else None,
        parent_author=row.get("replying_to"),
        quoted_id=str(row["quoted_id"]) if row.get("quoted_id") else None,
        media_types=list(row.get("media_types") or []),
        n_links=int(row.get("n_links") or 0),
        n_mentions=int(row.get("n_mentions") or 0),
        lang=row.get("lang"),
        metrics={k: int(row.get(k) or 0) for k in METRIC_KEYS} | {"fetched_at": row["fetched_at"]},
        sources=[source],
    )


def _merge(item: Item, row: dict, source: str) -> None:
    if source not in item.sources:
        item.sources.append(source)
    newer = row["fetched_at"] > item.metrics["fetched_at"]
    if newer:
        item.metrics = {k: int(row.get(k) or 0) for k in METRIC_KEYS} | {"fetched_at": row["fetched_at"]}
        if row.get("author_followers") is not None:
            item.author_followers = row["author_followers"]
    if not item.author_description and row.get("author_description"):
        item.author_description = row["author_description"]
    if not item.parent_id and row.get("replying_to_status"):
        item.parent_id, item.kind = str(row["replying_to_status"]), "reply"
        item.parent_author = row.get("replying_to")


def load(raw_dir: Path = RAW_DIR) -> Corpus:
    raw_dir = Path(raw_dir)
    items: dict[str, Item] = {}
    skipped, rows = Counter(), Counter()
    for source in SOURCES:
        path = raw_dir / f"{source}.jsonl.gz"
        if not path.is_file():
            raise FileNotFoundError(f"{path} is missing; rebuild with `python -m harness.corpus snapshot --src ...`")
        for row in _read_jsonl(path):
            if "id" not in row or "error" in row:
                skipped[source] += 1
                continue
            rows[source] += 1
            sid = str(row["id"])
            if sid in items:
                _merge(items[sid], row, source)
            else:
                items[sid] = _new_item(row, source)
            item = items[sid]
            if source == "author_timelines" and row.get("is_repost"):
                owner = row.get("reposted_by") or row.get("timeline_of")
                if owner and owner not in item.reposted_by:
                    item.reposted_by.append(owner)
            if source == "reply_samples":
                item.thread_root = str(row["reply_to_post"])
                item.thread_coverage = row.get("coverage")
            if source == "x_metrics":
                item.in_dataset = True

    for flags in (_read_csv(raw_dir / "boost_flags.csv.gz"), _read_csv(raw_dir / "timeline_posts.csv.gz")):
        for r in flags:
            item = items.get(str(r["post_id"]))
            if item is None:
                continue
            if _truthy(r.get("is_thread_continuation")):
                item.is_thread_continuation = True
            if "boost_suspect" in r:
                item.boost_suspect = _truthy(r["boost_suspect"])

    for item in items.values():
        if item.kind == "reply" and item.parent_author and item.parent_author.lower() == item.author.lower():
            item.is_thread_continuation = True

    ordered = sorted(items.values(), key=lambda i: int(i.status_id))
    return Corpus(ordered, skipped, rows)
