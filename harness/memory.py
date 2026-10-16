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
"""


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


def scored_history(limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.data, o.metrics FROM actions a JOIN outcomes o ON o.action_id = a.id
               ORDER BY o.recorded_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"action": json.loads(r["data"]), "actual": json.loads(r["metrics"])} for r in rows]
