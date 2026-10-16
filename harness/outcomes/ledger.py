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
from harness.outcomes.handoff import ApprovedAction
from harness.outcomes.label import BaselinePoint, Label
from harness.outcomes.match import Match
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
CREATE TABLE IF NOT EXISTS reads (
    id INTEGER PRIMARY KEY, at TEXT, n_posts INTEGER, oldest TEXT, exhausted INTEGER, requests INTEGER
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
    """Insert new actions; refresh text and approval time of known ones, keeping their sync state."""
    conn.executemany(
        """INSERT INTO actions (action_id, run_id, type, text, original_text, target_status, approved_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (action_id) DO UPDATE SET run_id = excluded.run_id, type = excluded.type,
               text = excluded.text, original_text = excluded.original_text,
               target_status = excluded.target_status, approved_at = excluded.approved_at, source = excluded.source""",
        [(a.id, a.run_id, a.type, a.text, a.original_text, a.target_status, iso(a.approved_at), a.source)
         for a in actions],
    )


def actions(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM actions ORDER BY approved_at, action_id").fetchall()


def to_action(row) -> ApprovedAction:
    return ApprovedAction(row["action_id"], row["run_id"], row["type"], row["text"], row["original_text"],
                          row["target_status"], datetime.fromisoformat(row["approved_at"]), row["source"])


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
    oldest = min((p.created_at for p in tl.posts), default=None)
    conn.execute("INSERT INTO reads (at, n_posts, oldest, exhausted, requests) VALUES (?, ?, ?, ?, ?)",
                 (tl.fetched_at, len(tl.posts), iso(oldest) if oldest else None, int(tl.exhausted), tl.requests))


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
        """SELECT m.*, p.created_at, p.kind, p.text AS posted_text FROM matches m
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
