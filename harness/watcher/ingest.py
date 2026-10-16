"""Thread ingestion: each watched post's conversation at +1h and +24h, kept as a benchmark item.

A checkpoint T runs inside the window [T, 2T) after posting, the rule `harness/outcomes/label.py`
uses, so a +1h read happens between 1 and 2 hours and a +24h read between 24 and 48. A post first
seen after a window closed never gets that checkpoint (`missed`), because a late read filed as +1h
would carry likes the thread had not yet earned at +1h.

Each read takes the first page of replies in both of X's sort orders and the post itself, three
requests, the same reads as `FxTwitterReader.replies`. Coverage is unique replies over the post's
own reply counter. A thread is kept only at coverage 0.8 or more, the corpus benchmark's
high-coverage line, because below it the middle of the thread is missing and losers skew late.

Kept threads are appended to data/threads/ in the files `harness.corpus.load` (branch sam/corpus)
reads: the post as a row of author_timelines.jsonl.gz, since it came off the account's own
timeline, and each reply as a row of reply_samples.jsonl.gz with `reply_to_post` and `coverage`,
like the context worker's reply samples. The loader requires all five of its inputs, so the two
it would find empty here (x_metrics, no dataset rows; the two flag CSVs) are created once, empty.
`load(Path("data/threads"))` then reads the directory as it is. Each append is one more gzip
member; Python's gzip reads a multi-member file as one stream. Rows also carry `checkpoint`,
`watch_profile` and, on replies, `minutes_after_parent` from the two Snowflake IDs. Repliers' bios
are left out: nothing reads them, and the repository is public.
"""

import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

from harness import config
from harness.context import snowflake
from harness.context.xreader import ReplySample
from harness.outcomes.label import WINDOW_FACTOR, checkpoint_name

CHECKPOINTS = (1, 24)  # hours after posting
MIN_COVERAGE = 0.8
THREADS_DIR = config.ROOT / "data" / "threads"
ROOTS_FILE, REPLIES_FILE = "author_timelines.jsonl.gz", "reply_samples.jsonl.gz"
# Inputs harness.corpus.load requires that the watcher has nothing for.
EMPTY_INPUTS = {"x_metrics.jsonl.gz": "", "boost_flags.csv.gz": "post_id,is_thread_continuation,boost_suspect\n",
                "timeline_posts.csv.gz": "post_id,is_thread_continuation\n"}
DROPPED_REPLY_FIELDS = ("author_description",)


def plan_checkpoints(created_at: datetime, first_seen: datetime) -> tuple[list[tuple[int, datetime, datetime]], list[int]]:
    """([(hours, opens, closes)] still reachable, [hours already missed]) for a post first seen at `first_seen`."""
    jobs, missed = [], []
    for t in CHECKPOINTS:
        opens, closes = created_at + timedelta(hours=t), created_at + timedelta(hours=t * WINDOW_FACTOR)
        if first_seen >= closes:
            missed.append(t)
        else:
            jobs.append((t, opens, closes))
    return jobs, missed


def fetch_thread(reader, status_id: str):
    """(the post, its ReplySample) from three reads: likes order, recency order, then the post."""
    top = reader.conversation(status_id, "likes")
    new = reader.conversation(status_id, "recency")
    root = reader.status(status_id)
    merged = {r.id: r for r in top + new if r.author_handle}
    return root, ReplySample(list(merged.values()), root.replies, root.fetched_at)


def minutes_between(earlier_id: str, later_id: str) -> float:
    return round((snowflake.to_unix_ms(later_id) - snowflake.to_unix_ms(earlier_id)) / 60000, 2)


def rows_for(root, sample: ReplySample, profile: str, checkpoint: int) -> tuple[dict, list[dict]]:
    """The post's row (author_timelines shape) and one row per reply (reply_samples shape)."""
    cp = checkpoint_name(checkpoint)
    meta = {"checkpoint": cp, "watch_profile": profile}
    age = (datetime.fromisoformat(sample.fetched_at) - root.created_at).total_seconds() / 60
    root_row = root.to_dict() | meta | {"coverage": sample.coverage, "reply_count": sample.reply_count,
                                        "sampled": len(sample.replies), "age_minutes_at_read": round(age, 2)}
    replies = []
    for r in sorted(sample.replies, key=lambda r: int(r.id)):
        row = {k: v for k, v in r.to_dict().items() if k not in DROPPED_REPLY_FIELDS}
        parent = r.replying_to_status or root.id
        replies.append({"reply_to_post": root.id, "coverage": sample.coverage, "reply_count": sample.reply_count}
                       | row | meta | {"minutes_after_parent": minutes_between(parent, r.id),
                                       "minutes_after_post": minutes_between(root.id, r.id)})
    return root_row, replies


def _append(path: Path, rows: list[dict]) -> None:
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    # mtime=0 keeps a member's bytes a function of its rows alone.
    with path.open("ab") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(data)


def ensure_inputs(threads_dir: Path) -> None:
    threads_dir.mkdir(parents=True, exist_ok=True)
    for name, header in EMPTY_INPUTS.items():
        path = threads_dir / name
        if not path.exists():
            with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(header.encode("utf-8"))


def append_thread(threads_dir: Path, root_row: dict, reply_rows: list[dict]) -> None:
    ensure_inputs(threads_dir)
    _append(threads_dir / ROOTS_FILE, [root_row])
    if reply_rows:
        _append(threads_dir / REPLIES_FILE, reply_rows)
