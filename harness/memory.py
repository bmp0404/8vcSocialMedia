"""SQLite memory: runs, recommendations, human reviews, real outcomes, and mind variants."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from harness import config
from harness.models import Action

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at TEXT, provider TEXT, sources TEXT);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY, run_id INTEGER, mind TEXT, type TEXT, predicted_score REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (action_id TEXT PRIMARY KEY, recorded_at TEXT, metrics TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    action_id TEXT PRIMARY KEY, status TEXT, text TEXT, note TEXT, reviewed_at TEXT, reasons TEXT
);
CREATE TABLE IF NOT EXISTS variants (
    id TEXT PRIMARY KEY, mind TEXT, version INTEGER, prompt TEXT, parent TEXT,
    status TEXT DEFAULT 'active', note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

# snoozed = "not now"; it carries no reward either way.
REVIEW_STATUSES = {"approved", "killed", "snoozed", "pending"}


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "reasons" not in {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")}:
        conn.execute("ALTER TABLE reviews ADD COLUMN reasons TEXT")  # databases from before reason codes
    return conn


def _reasons(raw: str | None) -> list[str]:
    return json.loads(raw) if raw else []


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- runs & actions -------------------------------------------------------------------------


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


def scored_actions() -> list[dict]:
    """Every action that has a human review or a real outcome, with both attached."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.data, rv.status, rv.text AS edited_text, rv.note, rv.reasons, o.metrics
               FROM actions a
               LEFT JOIN reviews rv ON rv.action_id = a.id
               LEFT JOIN outcomes o ON o.action_id = a.id
               WHERE rv.status IN ('approved', 'killed') OR o.metrics IS NOT NULL"""
        ).fetchall()
    return [
        {
            "action": json.loads(r["data"]),
            "review": r["status"],
            "edited_text": r["edited_text"],
            "note": r["note"] or "",
            "reasons": _reasons(r["reasons"]),
            "metrics": json.loads(r["metrics"]) if r["metrics"] else None,
        }
        for r in rows
    ]


# --- review dashboard -----------------------------------------------------------------------


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
    """Every action from a run, with its review state merged in, in final rank order (unranked last)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.data, rv.status, rv.text AS edited_text, rv.note, rv.reviewed_at, rv.reasons
               FROM actions a LEFT JOIN reviews rv ON rv.action_id = a.id
               WHERE a.run_id = ? ORDER BY a.predicted_score DESC""",
            (run_id,),
        ).fetchall()
    actions = [
        {
            **json.loads(r["data"]),
            "review": {
                "status": r["status"] or "pending",
                "text": r["edited_text"],
                "note": r["note"] or "",
                "reasons": _reasons(r["reasons"]),
                "reviewed_at": r["reviewed_at"],
            },
        }
        for r in rows
    ]
    return sorted(actions, key=lambda a: (a.get("rank") is None, a.get("rank") or 0))


def get_action(action_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT data FROM actions WHERE id = ?", (action_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def action_history(days: int = 365) -> list[dict]:
    """Approved actions from the last `days`, newest first, each with `reviewed_at`.

    Approved is the best proxy for "done": the posting bot and the team act on the approved hand-off.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.data, rv.reviewed_at, rv.text AS edited_text FROM actions a
               JOIN reviews rv ON rv.action_id = a.id
               WHERE rv.status = 'approved' AND rv.reviewed_at >= ? ORDER BY rv.reviewed_at DESC""",
            (cutoff,),
        ).fetchall()
    return [{**json.loads(r["data"]), "reviewed_at": r["reviewed_at"],
             **({"text": r["edited_text"]} if r["edited_text"] else {})} for r in rows]


def set_review(
    action_id: str, status: str, text: str | None = None, note: str = "", reasons: list[str] | None = None
) -> int:
    """Record a human verdict (and the reason codes behind it) on an action; returns its run id."""
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
                """INSERT OR REPLACE INTO reviews (action_id, status, text, note, reviewed_at, reasons)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (action_id, status, text, note, now(), json.dumps(reasons or [])),
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
        a["review_note"], a["review_reasons"], a["approved_at"] = rv["note"], rv["reasons"], rv["reviewed_at"]
        out.append(a)
    return out


# --- mind variants --------------------------------------------------------------------------


def sync_seed_minds(seeds: dict[str, str]) -> None:
    """Make sure each harness/minds/prompts/*.md prompt exists as a variant; an edited file becomes a new one."""
    with connect() as conn:
        for mind, prompt in seeds.items():
            known = conn.execute(
                "SELECT 1 FROM variants WHERE mind = ? AND prompt = ?", (mind, prompt)
            ).fetchone()
            if not known:
                _insert_variant(conn, mind, prompt, parent="seed", note="from minds/prompts")


def _insert_variant(conn, mind: str, prompt: str, parent: str, note: str) -> str:
    version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM variants WHERE mind = ?", (mind,)).fetchone()[0]
    vid = f"{mind}@v{version}"
    conn.execute(
        "INSERT INTO variants (id, mind, version, prompt, parent, status, note, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (vid, mind, version, prompt, parent, note, now()),
    )
    return vid


def add_variant(mind: str, prompt: str, parent: str, note: str = "") -> str:
    with connect() as conn:
        return _insert_variant(conn, mind, prompt, parent, note)


def variants(status: str | None = "active") -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute("SELECT * FROM variants WHERE status = ? ORDER BY mind, version", (status,))
        else:
            rows = conn.execute("SELECT * FROM variants ORDER BY mind, version")
        return [dict(r) for r in rows]


def retire_variant(variant_id: str, note: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE variants SET status = 'retired', note = ? WHERE id = ?", (note, variant_id))


# --- key/value ------------------------------------------------------------------------------


def kv_get(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key: str, value) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
