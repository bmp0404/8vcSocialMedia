"""The watcher's memory across restarts: a SQLite file at out/watch/state.db.

Primary keys make deduplication structural. A post is recorded once (`posts`), so a restart that
reads the same timeline again neither re-queues its hot draft nor reschedules its ingestion. A hot
job is one row per status (`hot`) and an ingestion job one row per status and checkpoint
(`ingest`). Rows a crash left `running` go back to the queue on the next start (`recover`).

Every connection is short-lived, so the poller and the hot-path thread can share the file.
"""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    status_id TEXT PRIMARY KEY, profile TEXT, handle TEXT, created_at TEXT, first_seen_at TEXT,
    age_at_first_seen_s REAL, replies_at_first_seen INTEGER, hot INTEGER, text TEXT
);
-- One draft attempt per fresh post. `post` holds the XPost as JSON so a restart can run a queued job.
CREATE TABLE IF NOT EXISTS hot (
    status_id TEXT PRIMARY KEY, profile TEXT, state TEXT, queued_at TEXT, started_at TEXT, finished_at TEXT,
    seconds REAL, path TEXT, outcome TEXT, error TEXT, post TEXT
);
-- Thread ingestion at +1h and +24h. due_at opens the job; closes_at (twice the checkpoint) ends it.
CREATE TABLE IF NOT EXISTS ingest (
    status_id TEXT, checkpoint INTEGER, profile TEXT, due_at TEXT, closes_at TEXT, state TEXT, attempts INTEGER,
    done_at TEXT, coverage REAL, sampled INTEGER, reply_count INTEGER, error TEXT,
    PRIMARY KEY (status_id, checkpoint)
);
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY, at TEXT, profile TEXT, handle TEXT, ok INTEGER, n_posts INTEGER, new_posts INTEGER,
    requests INTEGER, seconds REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, pid INTEGER, started_at TEXT, stopped_at TEXT, requests INTEGER, stop_reason TEXT
);
"""

HOT_QUEUED, HOT_RUNNING, HOT_DONE, HOT_FAILED, HOT_STALE = "queued", "running", "done", "failed", "stale"
PENDING, RUNNING, KEPT, LOW_COVERAGE, NO_REPLIES, MISSED, FAILED = (
    "pending", "running", "kept", "low_coverage", "no_replies", "missed", "failed")
INGEST_DONE = {KEPT, LOW_COVERAGE, NO_REPLIES, MISSED, FAILED}


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    # ------------------------------------------------------------ posts

    def add_post(self, profile: str, post, seen_at: datetime, hot: bool) -> bool:
        """Record a watched post the first time it's seen. False when it was already known."""
        age = (seen_at - post.created_at).total_seconds()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO posts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (post.id, profile, post.author_handle, iso(post.created_at), iso(seen_at), round(age, 1),
                 post.replies, int(hot), post.text),
            )
            return cur.rowcount == 1

    def known(self, status_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM posts WHERE status_id = ?", (status_id,)).fetchone() is not None

    # ------------------------------------------------------------ hot jobs

    def queue_hot(self, profile: str, post, at: datetime) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO hot (status_id, profile, state, queued_at, post) VALUES (?, ?, ?, ?, ?)",
                (post.id, profile, HOT_QUEUED, iso(at), json.dumps(post.to_dict())),
            )
            return cur.rowcount == 1

    def claim_hot(self, at: datetime) -> sqlite3.Row | None:
        """The oldest queued hot job, now marked running, or None."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM hot WHERE state = ? ORDER BY queued_at, status_id LIMIT 1",
                               (HOT_QUEUED,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE hot SET state = ?, started_at = ? WHERE status_id = ?",
                         (HOT_RUNNING, iso(at), row["status_id"]))
            return row

    def finish_hot(self, status_id: str, state: str, at: datetime, *, seconds: float | None = None,
                   path: str | None = None, outcome: str | None = None, error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE hot SET state = ?, finished_at = ?, seconds = ?, path = ?, outcome = ?, error = ? "
                         "WHERE status_id = ?", (state, iso(at), seconds, path, outcome, error, status_id))

    def hot_rows(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM hot ORDER BY queued_at").fetchall()

    # ------------------------------------------------------------ ingestion

    def schedule(self, status_id: str, profile: str, jobs: list[tuple[int, datetime, datetime]],
                 missed: list[int]) -> None:
        """Insert ingestion jobs (checkpoint hours, due, closes) and checkpoints already missed. Existing rows win."""
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO ingest (status_id, checkpoint, profile, due_at, closes_at, state, attempts) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                [(status_id, cp, profile, iso(due), iso(closes), PENDING) for cp, due, closes in jobs]
                + [(status_id, cp, profile, None, None, MISSED) for cp in missed],
            )

    def claim_ingest(self, now: datetime) -> sqlite3.Row | None:
        """The earliest pending job whose window has opened, now marked running; jobs whose window
        closed become missed first. A crash leaves the claimed job running, and `recover` re-pends it."""
        stamp = iso(now)
        with self._conn() as conn:
            conn.execute("UPDATE ingest SET state = ?, error = 'window closed before the job ran' "
                         "WHERE state = ? AND closes_at <= ?", (MISSED, PENDING, stamp))
            row = conn.execute("SELECT * FROM ingest WHERE state = ? AND due_at <= ? ORDER BY due_at LIMIT 1",
                               (PENDING, stamp)).fetchone()
            if row is not None:
                conn.execute("UPDATE ingest SET state = ? WHERE status_id = ? AND checkpoint = ?",
                             (RUNNING, row["status_id"], row["checkpoint"]))
            return row

    def next_ingest_at(self) -> datetime | None:
        with self._conn() as conn:
            row = conn.execute("SELECT MIN(due_at) AS t FROM ingest WHERE state = ?", (PENDING,)).fetchone()
        return utc(row["t"]) if row and row["t"] else None

    def finish_ingest(self, status_id: str, cp: int, state: str, at: datetime, *, coverage=None, sampled=None,
                      reply_count=None, error=None) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE ingest SET state = ?, done_at = ?, coverage = ?, sampled = ?, reply_count = ?, "
                         "error = ? WHERE status_id = ? AND checkpoint = ?",
                         (state, iso(at), coverage, sampled, reply_count, error, status_id, cp))

    def retry_ingest(self, status_id: str, cp: int, error: str, retry_at: datetime, max_attempts: int) -> str:
        """Count a failed attempt and push the job back, or fail it after max_attempts. Returns the new state."""
        with self._conn() as conn:
            row = conn.execute("SELECT attempts FROM ingest WHERE status_id = ? AND checkpoint = ?",
                               (status_id, cp)).fetchone()
            attempts = (row["attempts"] or 0) + 1
            state = FAILED if attempts >= max_attempts else PENDING
            conn.execute("UPDATE ingest SET state = ?, attempts = ?, error = ?, due_at = ? "
                         "WHERE status_id = ? AND checkpoint = ?",
                         (state, attempts, error, iso(retry_at), status_id, cp))
        return state

    def ingest_rows(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM ingest ORDER BY due_at").fetchall()

    # ------------------------------------------------------------ polls and runs

    def log_poll(self, at: datetime, profile: str, handle: str, *, ok: bool, n_posts: int = 0, new_posts: int = 0,
                 requests: int = 0, seconds: float = 0.0, error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO polls (at, profile, handle, ok, n_posts, new_posts, requests, seconds, error) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (iso(at), profile, handle, int(ok), n_posts, new_posts, requests, round(seconds, 2), error))

    def start_run(self, pid: int, at: datetime) -> int:
        with self._conn() as conn:
            return conn.execute("INSERT INTO runs (pid, started_at) VALUES (?, ?)", (pid, iso(at))).lastrowid

    def stop_run(self, run_id: int, at: datetime, requests: int, reason: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE runs SET stopped_at = ?, requests = ?, stop_reason = ? WHERE id = ?",
                         (iso(at), requests, reason, run_id))

    def recover(self, hot_path_for) -> dict:
        """After a crash: running hot jobs with a written file are done, the rest go back to the queue;
        running ingestion jobs go back to pending. Returns how many of each moved."""
        moved = {"hot_done": 0, "hot_requeued": 0, "ingest_requeued": 0}
        with self._conn() as conn:
            for row in conn.execute("SELECT status_id FROM hot WHERE state = ?", (HOT_RUNNING,)).fetchall():
                done = hot_path_for(row["status_id"]).is_file()
                conn.execute("UPDATE hot SET state = ? WHERE status_id = ?",
                             (HOT_DONE if done else HOT_QUEUED, row["status_id"]))
                moved["hot_done" if done else "hot_requeued"] += 1
            moved["ingest_requeued"] = conn.execute("UPDATE ingest SET state = ? WHERE state = ?",
                                                    (PENDING, RUNNING)).rowcount
        return moved

    def summary(self, since: datetime | None = None) -> dict:
        """Counts for `harness watch status` and the live-run report."""
        since_s = iso(since) if since else ""
        with self._conn() as conn:
            def q(sql: str, *args):
                return conn.execute(sql, args).fetchone()[0]

            polls = q("SELECT COUNT(*) FROM polls WHERE at >= ?", since_s)
            first_poll = q("SELECT MIN(at) FROM polls WHERE at >= ?", since_s)
            last_poll = q("SELECT MAX(at) FROM polls WHERE at >= ?", since_s)
            requests = q("SELECT COALESCE(SUM(requests), 0) FROM polls WHERE at >= ?", since_s)
            out = {
                "polls": polls,
                "poll_errors": q("SELECT COUNT(*) FROM polls WHERE ok = 0 AND at >= ?", since_s),
                "handles_polled": q("SELECT COUNT(DISTINCT profile || '/' || handle) FROM polls WHERE ok = 1 AND at >= ?",
                                    since_s),
                "poll_requests": requests,
                "first_poll": first_poll, "last_poll": last_poll,
                "posts_seen": q("SELECT COUNT(*) FROM posts WHERE first_seen_at >= ?", since_s),
                "fresh_posts": q("SELECT COUNT(*) FROM posts WHERE hot = 1 AND first_seen_at >= ?", since_s),
                "hot": {r[0]: r[1] for r in conn.execute(
                    "SELECT state, COUNT(*) FROM hot WHERE queued_at >= ? GROUP BY state", (since_s,))},
                "ingest": {r[0]: r[1] for r in conn.execute(
                    "SELECT state, COUNT(*) FROM ingest GROUP BY state")},
                "errors_by_handle": {f"{r[0]}/{r[1]}": r[2] for r in conn.execute(
                    "SELECT profile, handle, COUNT(*) FROM polls WHERE ok = 0 AND at >= ? GROUP BY profile, handle",
                    (since_s,))},
            }
        if first_poll and last_poll and first_poll != last_poll:
            span = (utc(last_poll) - utc(first_poll)).total_seconds()
            out["poll_requests_per_s"] = round(requests / span, 3) if span else None
        return out

