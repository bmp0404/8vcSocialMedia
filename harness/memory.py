"""Shared memory layer: the data table both lanes write (Eval) and read (Observe + Action).

One row per candidate action: context (what it responds to) + the post + its eval, a status, and
nullable actual-metrics columns the memory team fills from X public metrics (`record`). `learn`
turns predicted vs. actual into lessons; `recall(lane)` hands lessons, best/worst examples, recent
texts and already-engaged targets back to the lanes.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from harness import config
from harness.models import Action

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, started_at TEXT, provider TEXT, sources TEXT, lanes TEXT, mission TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    lane TEXT NOT NULL,                     -- reply | post
    created_at TEXT,
    -- context: the observed Signal (reply lane) or Narrative (post lane) this responds to
    narrative_id TEXT,
    context TEXT,                           -- JSON
    -- the post
    mind TEXT,
    type TEXT,                              -- post | reply | quote | mention | follow
    text TEXT,
    target_url TEXT,
    target_handle TEXT,
    rationale TEXT,
    -- eval
    predicted_score REAL,
    panel TEXT,                             -- JSON {panelist: 0-1}
    vetoed INTEGER DEFAULT 0,
    critique TEXT,
    rank INTEGER,                           -- position in out/recommendations-<run>.json, NULL if cut
    status TEXT DEFAULT 'recommended',      -- recommended | posted | rejected
    -- actuals, filled later from X public metrics
    post_id TEXT,
    post_url TEXT,
    metrics_at TEXT,
    actual_likes INTEGER,
    actual_reposts INTEGER,
    actual_replies INTEGER,
    actual_quotes INTEGER,
    actual_bookmarks INTEGER,
    actual_impressions INTEGER,
    actual_metrics TEXT                     -- JSON, everything recorded
);
CREATE INDEX IF NOT EXISTS actions_lane ON actions (lane, created_at);
CREATE TABLE IF NOT EXISTS reviews (
    action_id TEXT PRIMARY KEY, status TEXT, text TEXT, note TEXT, reviewed_at TEXT
);
"""

STATUSES = {"recommended", "posted", "rejected"}
REVIEW_STATUSES = {"approved", "killed", "pending"}

# record() accepts our short names or raw X public_metrics names.
METRIC_COLUMNS = {
    "actual_likes": ("likes", "like_count"),
    "actual_reposts": ("reposts", "retweets", "retweet_count"),
    "actual_replies": ("replies", "reply_count"),
    "actual_quotes": ("quotes", "quote_count"),
    "actual_bookmarks": ("bookmarks", "bookmark_count"),
    "actual_impressions": ("impressions", "views", "impression_count"),
}
JSON_COLUMNS = ("context", "panel")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- write (Eval)


def start_run(lanes: list[str], sources: list[str], provider: str | None = None) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO runs (started_at, provider, sources, lanes, mission) VALUES (?, ?, ?, ?, ?)",
            (now(), provider or config.PROVIDER, ",".join(sources), ",".join(lanes), config.mission_name()),
        ).lastrowid


def save_actions(actions: list[Action]) -> None:
    """Eval's write: one row per evaluated candidate. Vetoed drafts are stored as `rejected`."""
    rows = [
        (a.id, a.run_id, a.lane, now(), a.narrative_id, json.dumps(a.context), a.mind, a.type, a.text,
         a.target_url, a.target_handle, a.rationale, a.predicted_score, json.dumps(a.panel), int(a.vetoed),
         a.critique, "rejected" if a.vetoed else "recommended")
        for a in actions
    ]
    with connect() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO actions (id, run_id, lane, created_at, narrative_id, context, mind, type,
               text, target_url, target_handle, rationale, predicted_score, panel, vetoed, critique, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def mark_ranked(run_id: int, actions: list[Action]) -> None:
    """Stamp the final ranking; candidates that didn't make the cut become `rejected`."""
    with connect() as conn:
        conn.execute(
            "UPDATE actions SET rank = NULL, status = 'rejected' WHERE run_id = ? AND status = 'recommended'",
            (run_id,),
        )
        conn.executemany(
            "UPDATE actions SET rank = ?, status = 'recommended' WHERE id = ?",
            [(i, a.id) for i, a in enumerate(actions, 1)],
        )


def record_outcomes(outcomes: list[dict]) -> int:
    """outcomes: [{"action_id": "...", "metrics": {"likes": 3, ...}, "post_id"?: "...", "post_url"?: "...",
    "status"?: "posted"|"rejected"}]. Metrics may use X public_metrics names (like_count, ...)."""
    n = 0
    with connect() as conn:
        for o in outcomes:
            m = o.get("metrics") or {}
            cols = {col: next((m[k] for k in keys if k in m), None) for col, keys in METRIC_COLUMNS.items()}
            status = o.get("status", "posted")
            if status not in STATUSES:
                raise ValueError(f"status must be one of {sorted(STATUSES)}")
            cur = conn.execute(
                f"""UPDATE actions SET status = ?, post_id = COALESCE(?, post_id), post_url = COALESCE(?, post_url),
                    metrics_at = ?, actual_metrics = ?, {", ".join(f"{c} = ?" for c in cols)} WHERE id = ?""",
                (status, o.get("post_id"), o.get("post_url"), now(), json.dumps(m), *cols.values(),
                 o["action_id"]),
            )
            if not cur.rowcount:
                print(f"[record] unknown action_id {o['action_id']}, skipping")
            n += cur.rowcount
    return n


# --------------------------------------------------------------------------- read (Observe + Action)


def _action(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in JSON_COLUMNS:
        d[k] = json.loads(d[k]) if d.get(k) else {}
    d["vetoed"] = bool(d.get("vetoed"))
    if d.get("actual_metrics"):
        d["actual_metrics"] = json.loads(d["actual_metrics"])
    return d


def engagement(row: dict) -> float:
    """One number for 'how did it actually do'; replies and reposts weigh more than likes."""
    g = lambda k: row.get(k) or 0  # noqa: E731
    return g("actual_likes") + 2 * g("actual_replies") + 2 * g("actual_reposts") + 3 * g("actual_quotes")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 3}


def _lane_lessons(text: str, lane: str) -> str:
    """If lessons.md has `## ...` sections, keep the general ones and the ones for this lane."""
    parts = re.split(r"(?m)^(?=## )", text)
    if len(parts) == 1:
        return text.strip()
    other = {"reply": "post", "post": "reply"}.get(lane, "")
    keep = [p for p in parts if not p.startswith("## ") or other not in p.splitlines()[0].lower()]
    return "".join(keep).strip()


def recall(lane: str, context_text: str | None = None, k: int = 3, days: int = 7) -> dict:
    """What a lane should remember before observing and drafting.

    Returns {"lane", "lessons", "best", "worst", "recent_texts", "engaged_handles", "engaged_urls"}:
    best/worst are this lane's measured actions (biased toward ones similar to `context_text`),
    recent_texts are what either lane recommended or posted in the last `days` days, and
    engaged_* are targets we've already recommended or hit in that window.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        scored = [_action(r) for r in conn.execute(
            "SELECT * FROM actions WHERE lane = ? AND metrics_at IS NOT NULL ORDER BY metrics_at DESC LIMIT 500",
            (lane,),
        )]
        recent = [_action(r) for r in conn.execute(
            """SELECT * FROM actions WHERE status IN ('recommended', 'posted') AND created_at >= ?
               AND (status = 'posted' OR rank IS NOT NULL) ORDER BY created_at DESC LIMIT 200""",
            (since,),
        )]

    ctx = _words(context_text or "")
    sim = lambda a: len(ctx & _words(a["text"] + " " + json.dumps(a["context"]))) / (len(ctx) or 1)  # noqa: E731
    by_eng = sorted(scored, key=lambda a: engagement(a) * (1 + sim(a)), reverse=True)

    def example(a: dict) -> dict:
        return {
            "type": a["type"], "text": a["text"], "mind": a["mind"],
            "target": a["target_url"] or a["target_handle"], "predicted_score": a["predicted_score"],
            "engagement": engagement(a),
            "actual": {c.removeprefix("actual_"): a[c] for c in METRIC_COLUMNS if a.get(c) is not None},
        }

    best = by_eng[:k]
    worst = [a for a in reversed(by_eng) if a not in best][:k]
    handles = {a["target_handle"].lstrip("@").lower() for a in recent if a.get("target_handle")}
    return {
        "lane": lane,
        "lessons": _lane_lessons(config.lessons(), lane),
        "best": [example(a) for a in best],
        "worst": [example(a) for a in worst],
        "recent_texts": list(dict.fromkeys(a["text"] for a in recent if a["text"]))[:15],
        "engaged_handles": sorted(handles),
        "engaged_urls": sorted({a["target_url"] for a in recent if a.get("target_url")}),
    }


def format_recall(mem: dict) -> str:
    """Render a recall() dict as a prompt block."""

    def ex(rows: list[dict]) -> str:
        return "\n".join(
            f"- [{r['type']}, engagement {r['engagement']:g}, predicted {r['predicted_score']:.2f}] {r['text']}"
            for r in rows
        ) or "(none yet)"

    recent = "\n".join(f"- {t}" for t in mem["recent_texts"]) or "(nothing yet)"
    engaged = ", ".join(f"@{h}" for h in mem["engaged_handles"]) or "(none)"
    return f"""LESSONS:
{mem["lessons"]}

BEST PAST {mem["lane"].upper()} ACTIONS (do more like these):
{ex(mem["best"])}

WORST PAST {mem["lane"].upper()} ACTIONS (avoid):
{ex(mem["worst"])}

ALREADY SAID RECENTLY (don't repeat these or close paraphrases):
{recent}

ALREADY ENGAGED RECENTLY (don't re-follow or re-mention): {engaged}"""


# --------------------------------------------------------------------------- learn + review dashboard


def scored_history(limit: int = 200) -> list[dict]:
    """Predicted vs. actual for every measured action, newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions WHERE metrics_at IS NOT NULL ORDER BY metrics_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in map(_action, rows):
        out.append({
            "lane": r["lane"], "mind": r["mind"], "type": r["type"], "text": r["text"],
            "target": r["target_url"] or r["target_handle"], "context": r["context"].get("text")
            or r["context"].get("title"), "predicted_score": r["predicted_score"], "panel": r["panel"],
            "actual": r["actual_metrics"], "engagement": engagement(r),
        })
    return out


def list_runs(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.started_at, r.provider, r.sources, r.lanes, r.mission, COUNT(a.id) AS n_actions,
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
            """SELECT a.*, rv.status AS rv_status, rv.text AS rv_text, rv.note AS rv_note,
                      rv.reviewed_at AS rv_at
               FROM actions a LEFT JOIN reviews rv ON rv.action_id = a.id
               WHERE a.run_id = ? ORDER BY a.predicted_score DESC""",
            (run_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = _action(r)
        d["review"] = {
            "status": d.pop("rv_status") or "pending",
            "text": d.pop("rv_text"),
            "note": d.pop("rv_note") or "",
            "reviewed_at": d.pop("rv_at"),
        }
        out.append(d)
    return out


def set_review(action_id: str, status: str, text: str | None = None, note: str = "") -> int:
    """Record a human verdict on an action; killing it marks the row `rejected`. Returns the run id."""
    if status not in REVIEW_STATUSES:
        raise ValueError(f"status must be one of {sorted(REVIEW_STATUSES)}")
    with connect() as conn:
        row = conn.execute("SELECT run_id, vetoed, rank, status FROM actions WHERE id = ?", (action_id,)).fetchone()
        if not row:
            raise KeyError(action_id)
        if status == "pending":
            conn.execute("DELETE FROM reviews WHERE action_id = ?", (action_id,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?)",
                (action_id, status, text, note, now()),
            )
        if row["status"] != "posted":
            default = "recommended" if row["rank"] is not None and not row["vetoed"] else "rejected"
            new = {"killed": "rejected", "approved": "recommended"}.get(status, default)
            conn.execute("UPDATE actions SET status = ? WHERE id = ?", (new, action_id))
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
