"""SQLite memory: every run, every recommendation, and every observed outcome."""

import json
import sqlite3
from datetime import datetime, timezone

from harness import config
from harness.models import Action

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at TEXT, provider TEXT, sources TEXT);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY, run_id INTEGER, mind TEXT, type TEXT, predicted_score REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (action_id TEXT PRIMARY KEY, recorded_at TEXT, metrics TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    action_id TEXT PRIMARY KEY, status TEXT, text TEXT, note TEXT, reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY, action_id TEXT, run_id INTEGER, status TEXT,
    original_text TEXT, edited_text TEXT, note TEXT, reviewed_at TEXT, reasons TEXT
);
-- The account a run was made for. A hand-off exported later names this account, not whatever
-- [brand] x_handle or HARNESS_ACCOUNT_HANDLE say at export time. A new table, not a runs column,
-- because CREATE TABLE IF NOT EXISTS never adds a column to an existing data/harness.db.
CREATE TABLE IF NOT EXISTS run_accounts (run_id INTEGER PRIMARY KEY, account TEXT);
-- The mission profile a run was made for (config.profile_name), so learn reads one brand's runs only.
CREATE TABLE IF NOT EXISTS run_profiles (run_id INTEGER PRIMARY KEY, profile TEXT);
"""

REVIEW_STATUSES = {"approved", "killed", "pending"}
# Kill reason codes the review UI sends. stale_story and wrong_target point at observe; the rest at the draft.
REVIEW_REASONS = {"off_message", "factually_shaky", "stale_story", "wrong_target", "slop", "tone", "other"}


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_run(actions: list[Action], account: str | None = None) -> int:
    with connect() as conn:
        run_id = conn.execute(
            "INSERT INTO runs (started_at, provider, sources) VALUES (?, ?, ?)",
            (now(), config.PROVIDER, ",".join(config.SOURCES)),
        ).lastrowid
        conn.executemany(
            "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?)",
            [(a.id, run_id, a.mind, a.type, a.predicted_score, json.dumps(a.to_dict())) for a in actions],
        )
        if account:
            conn.execute("INSERT INTO run_accounts VALUES (?, ?)", (run_id, account))
        conn.execute("INSERT INTO run_profiles VALUES (?, ?)", (run_id, config.profile_name()))
    return run_id


def profile_run_ids(profile: str) -> set[int]:
    """Runs made for `profile`. Runs saved before profiles existed have no row; every one of them used the
    Caitlin Clark mission, so they count as the default profile's."""
    with connect() as conn:
        tagged = conn.execute("SELECT run_id FROM run_profiles WHERE profile = ?", (profile,)).fetchall()
        untagged = conn.execute(
            "SELECT id FROM runs WHERE id NOT IN (SELECT run_id FROM run_profiles)").fetchall() \
            if profile == config.DEFAULT_PROFILE else []
    return {r[0] for r in tagged} | {r[0] for r in untagged}


def run_account(run_id: int) -> str | None:
    """The account save_run recorded for this run; None for runs saved without one."""
    with connect() as conn:
        row = conn.execute("SELECT account FROM run_accounts WHERE run_id = ?", (run_id,)).fetchone()
    return row["account"] if row else None


def record_outcomes(outcomes: list[dict]) -> int:
    """outcomes: [{"action_id": "...", "metrics": {"likes": 3, "replies": 1, ...}}]"""
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?)",
            [(o["action_id"], now(), json.dumps(o["metrics"])) for o in outcomes],
        )
    return len(outcomes)


def list_runs(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.started_at, r.provider, r.sources, COUNT(a.id) AS n_actions,
                      SUM(rv.status = 'approved') AS n_approved, SUM(rv.status = 'killed') AS n_killed
               FROM runs r LEFT JOIN actions a ON a.run_id = r.id
               LEFT JOIN reviews rv ON rv.action_id = a.id
               GROUP BY r.id ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def run_actions(run_id: int) -> list[dict]:
    """Every action from a run, with its review state merged in, best predicted first."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.data, rv.status, rv.text AS edited_text, rv.note, rv.reviewed_at
               FROM actions a LEFT JOIN reviews rv ON rv.action_id = a.id
               WHERE a.run_id = ? ORDER BY a.predicted_score DESC""",
            (run_id,),
        ).fetchall()
    return [
        {
            **json.loads(r["data"]),
            "review": {
                "status": r["status"] or "pending",
                "text": r["edited_text"],
                "note": r["note"] or "",
                "reviewed_at": r["reviewed_at"],
            },
        }
        for r in rows
    ]


def set_review(action_id: str, status: str, text: str | None = None, note: str = "",
               reasons: list[str] | None = None) -> int:
    """Record a human verdict on an action; returns the action's run id.

    `reasons` are codes from REVIEW_REASONS. They are kept only in `review_events`, as a JSON list.
    """
    if status not in REVIEW_STATUSES:
        raise ValueError(f"status must be one of {sorted(REVIEW_STATUSES)}")
    unknown = sorted(set(reasons or []) - REVIEW_REASONS)
    if unknown:
        raise ValueError(f"unknown review reasons {unknown}; allowed: {sorted(REVIEW_REASONS)}")
    with connect() as conn:
        row = conn.execute("SELECT run_id, data FROM actions WHERE id = ?", (action_id,)).fetchone()
        if not row:
            raise KeyError(action_id)
        # `reviews` keeps only the latest verdict; `review_events` keeps every one for learning.
        conn.execute(
            "INSERT INTO review_events (action_id, run_id, status, original_text, edited_text, note, reviewed_at,"
            " reasons) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (action_id, row["run_id"], status, json.loads(row["data"]).get("text", ""), text, note, now(),
             json.dumps(sorted(set(reasons or [])))),
        )
        if status == "pending":
            conn.execute("DELETE FROM reviews WHERE action_id = ?", (action_id,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?)",
                (action_id, status, text, note, now()),
            )
    return row["run_id"]


def action_text(action_id: str) -> str:
    """The draft's text as decide wrote it, before any review edit. KeyError for an unknown id."""
    with connect() as conn:
        row = conn.execute("SELECT data FROM actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        raise KeyError(action_id)
    return json.loads(row["data"]).get("text", "")


def approved_actions(run_id: int) -> list[dict]:
    """Approved actions for a run in hand-off shape, with any human edits applied."""
    out = []
    for a in run_actions(run_id):
        rv = a.pop("review")
        if rv["status"] != "approved":
            continue
        if rv["text"]:
            a["original_text"], a["text"] = a["text"], rv["text"]
        a["review_note"], a["approved_at"] = rv["note"], rv["reviewed_at"]
        out.append(a)
    return out


def scored_history(limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.data, o.metrics FROM actions a JOIN outcomes o ON o.action_id = a.id
               ORDER BY o.recorded_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"action": json.loads(r["data"]), "actual": json.loads(r["metrics"])} for r in rows]


def review_history() -> list[dict]:
    """Every review verdict in order, oldest first.

    Verdicts saved before `review_events` existed only survive as the latest row in `reviews`; they are
    returned with `id` None so learning still sees them.
    """
    with connect() as conn:
        events = conn.execute("SELECT * FROM review_events ORDER BY id").fetchall()
        legacy = conn.execute(
            """SELECT rv.action_id, a.run_id, rv.status, a.data, rv.text AS edited_text, rv.note, rv.reviewed_at
               FROM reviews rv JOIN actions a ON a.id = rv.action_id
               WHERE rv.action_id NOT IN (SELECT action_id FROM review_events)
               ORDER BY rv.reviewed_at"""
        ).fetchall()
    old = [
        {"id": None, "action_id": r["action_id"], "run_id": r["run_id"], "status": r["status"],
         "original_text": json.loads(r["data"]).get("text", ""), "edited_text": r["edited_text"],
         "note": r["note"], "reviewed_at": r["reviewed_at"], "reasons": []}
        for r in legacy
    ]
    return old + [{**dict(r), "reasons": json.loads(r["reasons"] or "[]")} for r in events]


def run_log() -> list[dict]:
    """Every action with its run's start time, latest review and recorded metrics."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.run_id, r.started_at, r.provider, a.data, rv.status, rv.text AS edited_text, rv.note,
                      rv.reviewed_at, o.metrics, o.recorded_at
               FROM actions a JOIN runs r ON r.id = a.run_id
               LEFT JOIN reviews rv ON rv.action_id = a.id
               LEFT JOIN outcomes o ON o.action_id = a.id
               ORDER BY a.run_id, a.id"""
        ).fetchall()
    return [
        {
            "run_id": r["run_id"],
            "started_at": r["started_at"],
            "provider": r["provider"],
            "action": json.loads(r["data"]),
            "review": {"status": r["status"], "text": r["edited_text"], "note": r["note"] or "",
                       "reviewed_at": r["reviewed_at"]} if r["status"] else None,
            "metrics": json.loads(r["metrics"]) if r["metrics"] else None,
            "recorded_at": r["recorded_at"],
        }
        for r in rows
    ]
