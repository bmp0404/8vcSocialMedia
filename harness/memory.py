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
"""

REVIEW_STATUSES = {"approved", "killed", "pending"}


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_run(actions: list[Action]) -> int:
    with connect() as conn:
        run_id = conn.execute(
            "INSERT INTO runs (started_at, provider, sources) VALUES (?, ?, ?)",
            (now(), config.PROVIDER, ",".join(config.SOURCES)),
        ).lastrowid
        conn.executemany(
            "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?)",
            [(a.id, run_id, a.mind, a.type, a.predicted_score, json.dumps(a.to_dict())) for a in actions],
        )
    return run_id


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


def set_review(action_id: str, status: str, text: str | None = None, note: str = "") -> int:
    """Record a human verdict on an action; returns the action's run id."""
    if status not in REVIEW_STATUSES:
        raise ValueError(f"status must be one of {sorted(REVIEW_STATUSES)}")
    with connect() as conn:
        row = conn.execute("SELECT run_id FROM actions WHERE id = ?", (action_id,)).fetchone()
        if not row:
            raise KeyError(action_id)
        if status == "pending":
            conn.execute("DELETE FROM reviews WHERE action_id = ?", (action_id,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?)",
                (action_id, status, text, note, now()),
            )
    return row["run_id"]


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
