"""The outcomes ledger: what sync has seen, matched and fetched, so a rerun only fetches what's due.

It's a SQLite file of its own (data/outcomes.db by default, gitignored by data/*.db) rather than
tables in data/harness.db, whose schema Memory owns and teammates delete after schema changes.
Primary keys make the idempotency structural: one match per action and per post, one snapshot per
post and checkpoint, and a repeated insert is ignored.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from harness import config
from harness.outcomes.handoff import HOT_SOURCE, ApprovedAction, HotTarget
from harness.outcomes.label import BaselinePoint, Label
from harness.outcomes.match import Match
from harness.outcomes.rank import Rank
from harness.outcomes.reader import Counts, OwnPost, Timeline

DEFAULT_PATH = config.ROOT / "data" / "outcomes.db"

_COUNTS = "fetched_at TEXT, age_hours REAL, likes INTEGER, reposts INTEGER, replies INTEGER, quotes INTEGER, " \
          "views INTEGER, author_followers INTEGER"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY, run_id INTEGER, type TEXT, text TEXT, original_text TEXT, target_status TEXT,
    approved_at TEXT, source TEXT,
    not_posted_at TEXT  -- a timeline read reaching back to approval, 24h or more later, had no match
);
CREATE TABLE IF NOT EXISTS posts (
    status_id TEXT PRIMARY KEY, created_at TEXT, kind TEXT, replying_to_status TEXT, quoted_id TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    action_id TEXT PRIMARY KEY, status_id TEXT NOT NULL UNIQUE, score REAL, anchored INTEGER, matched_at TEXT,
    error TEXT, error_at TEXT  -- the last failed status fetch, cleared by the next success
);
-- Our matched posts, read with XReader.status when a checkpoint falls due.
CREATE TABLE IF NOT EXISTS snapshots (status_id TEXT, checkpoint INTEGER, {_COUNTS},
    PRIMARY KEY (status_id, checkpoint));
-- Every own post, read off the timeline: the account's norm at each checkpoint.
CREATE TABLE IF NOT EXISTS baseline (status_id TEXT, checkpoint INTEGER, {_COUNTS},
    PRIMARY KEY (status_id, checkpoint));
CREATE TABLE IF NOT EXISTS labels (
    action_id TEXT, checkpoint INTEGER, outcome TEXT, engagement INTEGER, baseline_n INTEGER,
    baseline_median REAL, baseline_p10 REAL, baseline_p90 REAL, labeled_at TEXT,
    PRIMARY KEY (action_id, checkpoint)
);
-- reach: the oldest creation time the read provably covered (see reader.own_timeline).
CREATE TABLE IF NOT EXISTS reads (id INTEGER PRIMARY KEY, at TEXT, n_posts INTEGER, reach TEXT);
-- The watcher's hot targets: which arm the coin gave each one (harness/watcher/arms.py).
CREATE TABLE IF NOT EXISTS hot_targets (
    action_id TEXT PRIMARY KEY, status_id TEXT, arm TEXT, profile TEXT, method TEXT
);
-- One row per matched reply, written once, at match time.
CREATE TABLE IF NOT EXISTS reply_matches (
    action_id TEXT PRIMARY KEY, status_id TEXT, parent_status TEXT, arm TEXT, minutes_after_parent REAL,
    followers_at_match INTEGER, matched_at TEXT
);
-- rank_in_thread at +1h and +24h after the reply (rank.py). Replaces the median label for replies.
CREATE TABLE IF NOT EXISTS ranks (
    action_id TEXT, checkpoint INTEGER, fetched_at TEXT, age_hours REAL, rank INTEGER, likes_rank INTEGER,
    page_replies INTEGER, thread_replies INTEGER, percentile REAL, percentile_max REAL, likes INTEGER,
    followers INTEGER, outcome TEXT, PRIMARY KEY (action_id, checkpoint)
);
"""


class LedgerHandleError(ValueError):
    """The ledger was built for another account."""


def connect(path: Path = DEFAULT_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def handle(conn) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'handle'").fetchone()
    return row["value"] if row else None


def claim_handle(conn, value: str, path: Path) -> None:
    """Bind the ledger to one account; refuse to mix another account's posts into its baseline."""
    current = handle(conn)
    if current is None:
        conn.execute("INSERT INTO meta VALUES ('handle', ?)", (value,))
    elif current.lower() != value.lower():
        raise LedgerHandleError(f"{path} holds @{current}'s posts; pass --ledger with another file for @{value}")


def save_actions(conn, actions: list[ApprovedAction]) -> None:
    """Insert new actions; refresh the text of known ones and keep their first approval time."""
    conn.executemany(
        """INSERT INTO actions (action_id, run_id, type, text, original_text, target_status, approved_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (action_id) DO UPDATE SET run_id = excluded.run_id, type = excluded.type,
               text = excluded.text, original_text = excluded.original_text,
               target_status = excluded.target_status, source = excluded.source,
               approved_at = MIN(approved_at, excluded.approved_at)""",
        [(a.id, a.run_id, a.type, a.text, a.original_text, a.target_status, iso(a.approved_at), a.source)
         for a in actions],
    )


def actions(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM actions ORDER BY approved_at, action_id").fetchall()


def to_action(row) -> ApprovedAction:
    return ApprovedAction(row["action_id"], row["run_id"], row["type"], row["text"], row["original_text"],
                          row["target_status"], datetime.fromisoformat(row["approved_at"]), row["source"],
                          anchor_only=(row["source"] or "").startswith(HOT_SOURCE))


def to_post(row) -> OwnPost:
    return OwnPost(row["status_id"], datetime.fromisoformat(row["created_at"]), row["kind"], row["text"],
                   row["replying_to_status"], row["quoted_id"])


def save_posts(conn, posts: list[OwnPost]) -> int:
    """Store posts not seen before. Returns how many were new."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO posts VALUES (?, ?, ?, ?, ?, ?)",
        [(p.id, iso(p.created_at), p.kind, p.replying_to_status, p.quoted_id, p.text) for p in posts],
    )
    return conn.total_changes - before


def save_read(conn, tl: Timeline) -> None:
    conn.execute("INSERT INTO reads (at, n_posts, reach) VALUES (?, ?, ?)",
                 (tl.fetched_at, len(tl.posts), iso(tl.reach) if tl.reach else None))


def _insert_counts(conn, table: str, status_id: str, cp: int, age: float, c: Counts) -> bool:
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (status_id, cp, c.fetched_at, round(age, 2), c.likes, c.reposts, c.replies, c.quotes, c.views,
         c.author_followers),
    )
    return cur.rowcount == 1


def save_snapshot(conn, status_id: str, cp: int, age: float, c: Counts) -> bool:
    """Our matched post at a checkpoint. False when that checkpoint already has one."""
    return _insert_counts(conn, "snapshots", status_id, cp, age, c)


def save_baseline(conn, status_id: str, cp: int, age: float, c: Counts) -> bool:
    """Any own post at a checkpoint, from a timeline read. The first reading in the window is kept."""
    return _insert_counts(conn, "baseline", status_id, cp, age, c)


def has_snapshot(conn, status_id: str, cp: int) -> bool:
    return conn.execute("SELECT 1 FROM snapshots WHERE status_id = ? AND checkpoint = ?",
                        (status_id, cp)).fetchone() is not None


def last_read(conn) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reads ORDER BY id DESC LIMIT 1").fetchone()


def unmatched_posts(conn) -> list[OwnPost]:
    rows = conn.execute("SELECT * FROM posts WHERE status_id NOT IN (SELECT status_id FROM matches)").fetchall()
    return [to_post(r) for r in rows]


def save_matches(conn, matches: list[Match], at: datetime) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO matches (action_id, status_id, score, anchored, matched_at) VALUES (?, ?, ?, ?, ?)",
        [(m.action_id, m.status_id, m.score, int(m.anchored), iso(at)) for m in matches],
    )


def matched(conn) -> list[sqlite3.Row]:
    """Matched actions joined to their posts."""
    return conn.execute(
        """SELECT m.*, p.created_at, p.kind, p.text AS posted_text, p.replying_to_status FROM matches m
           JOIN posts p ON p.status_id = m.status_id ORDER BY p.created_at"""
    ).fetchall()


def set_not_posted(conn, action_id: str, at: str) -> None:
    conn.execute("UPDATE actions SET not_posted_at = ? WHERE action_id = ? AND not_posted_at IS NULL",
                 (at, action_id))


def set_error(conn, action_id: str, error: str | None, at: datetime) -> None:
    conn.execute("UPDATE matches SET error = ?, error_at = ? WHERE action_id = ?",
                 (error, iso(at) if error else None, action_id))


def snapshots(conn, status_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM snapshots WHERE status_id = ? ORDER BY checkpoint", (status_id,)).fetchall()


def baseline_points(conn, cp: int) -> list[BaselinePoint]:
    rows = conn.execute(
        """SELECT b.status_id, p.created_at, p.kind, b.likes + b.reposts + b.replies + b.quotes AS engagement
           FROM baseline b JOIN posts p ON p.status_id = b.status_id WHERE b.checkpoint = ?""",
        (cp,),
    ).fetchall()
    return [BaselinePoint(r["status_id"], datetime.fromisoformat(r["created_at"]), r["kind"], r["engagement"])
            for r in rows]


def labels(conn, action_id: str) -> dict[int, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM labels WHERE action_id = ?", (action_id,)).fetchall()
    return {r["checkpoint"]: r for r in rows}


def save_label(conn, action_id: str, cp: int, lab: Label, at: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (action_id, cp, lab.outcome, lab.engagement, lab.baseline_n, lab.baseline_median, lab.baseline_p10,
         lab.baseline_p90, iso(at)),
    )


def save_hot_targets(conn, targets: list[HotTarget]) -> None:
    conn.executemany(
        """INSERT INTO hot_targets VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (action_id) DO UPDATE SET arm = excluded.arm, profile = excluded.profile,
               method = excluded.method""",
        [(t.action_id, t.status_id, t.arm, t.profile, t.method) for t in targets],
    )


def hot_targets(conn) -> dict[str, sqlite3.Row]:
    return {r["action_id"]: r for r in conn.execute("SELECT * FROM hot_targets").fetchall()}


def save_reply_match(conn, action_id: str, status_id: str, parent_status: str, arm: str | None,
                     minutes_after_parent: float, followers: int | None, at: datetime) -> None:
    conn.execute("INSERT OR IGNORE INTO reply_matches VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (action_id, status_id, parent_status, arm, minutes_after_parent, followers, iso(at)))


def reply_match(conn, action_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reply_matches WHERE action_id = ?", (action_id,)).fetchone()


def save_rank(conn, action_id: str, cp: int, fetched_at: str, age: float, r: Rank) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO ranks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (action_id, cp, fetched_at, round(age, 2), r.rank, r.likes_rank, r.page_replies, r.thread_replies,
         r.percentile, r.percentile_max, r.likes, r.followers, r.outcome),
    )
    return cur.rowcount == 1


def has_rank(conn, action_id: str, cp: int) -> bool:
    return conn.execute("SELECT 1 FROM ranks WHERE action_id = ? AND checkpoint = ?",
                        (action_id, cp)).fetchone() is not None


def ranks(conn, action_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ranks WHERE action_id = ? ORDER BY checkpoint", (action_id,)).fetchall()
