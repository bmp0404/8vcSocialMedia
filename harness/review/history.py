"""History: every past verdict, newest first, with what happened to it on X once `harness outcomes sync`
has found out.

The outcome comes from the outcomes ledger (`data/outcomes.db`, `harness/outcomes/sync.py` status_rows),
which covers hot targets as well as rounds' drafts; memory's outcomes table holds only the latter. A reply
is judged by its rank among the other replies to the same post, so its outcome is that rank; a post's is
its likes. Both carry the time they were read.
"""

import json
from datetime import datetime, timedelta

from harness import memory
from harness.outcomes import ledger
from harness.outcomes.handoff import status_id
from harness.outcomes.sync import status_rows
from harness.review.routes import Request, route

LIMIT = 200


def _checked_at(row: dict) -> str | None:
    """When the latest counts were read: the rank's own read time, else the snapshot's age after posting."""
    if row.get("rank"):
        return row["rank"]["fetched_at"]
    latest, posted = row.get("latest"), row.get("posted_at")
    if latest and posted and latest.get("age_hours") is not None:
        return (datetime.fromisoformat(posted) + timedelta(hours=latest["age_hours"])).isoformat(timespec="seconds")
    return None


def outcome(row: dict | None) -> dict | None:
    """What the page shows beside a verdict, from one status_rows row; None when the ledger never saw it."""
    if row is None:
        return None
    rank, latest = row.get("rank"), row.get("latest") or {}
    return {
        "state": row["state"],  # posted, not_posted, waiting, unverified or untracked
        "url": row.get("url"),
        "posted_at": row.get("posted_at"),
        "rank": rank["rank"] if rank else None,  # 1 is the top reply; None when below the first page
        "page_replies": rank["page_replies"] if rank else None,
        "thread_replies": rank["thread_replies"] if rank else None,
        "likes": rank["likes"] if rank and rank["likes"] is not None else latest.get("likes"),
        "checked_at": _checked_at(row),
    }


def _ledger_rows() -> dict[str, dict] | None:
    if not ledger.DEFAULT_PATH.exists():
        return None
    _, rows = status_rows(ledger.DEFAULT_PATH)
    return {r["action_id"]: r for r in rows}


def items(limit: int = LIMIT) -> tuple[list[dict], bool]:
    """(verdicts newest first, whether an outcomes ledger exists)."""
    last_event = {}
    for ev in memory.review_history():  # oldest first, so the last one per action wins
        last_event[ev["action_id"]] = ev
    with memory.connect() as conn:
        rows = conn.execute(
            """SELECT rv.action_id, rv.status, rv.text, rv.note, rv.reviewed_at, a.data FROM reviews rv
               JOIN actions a ON a.id = rv.action_id ORDER BY rv.reviewed_at DESC, rv.action_id LIMIT ?""",
            (limit,),
        ).fetchall()
    outcomes = _ledger_rows()
    out = []
    for r in rows:
        a = json.loads(r["data"])
        sid = status_id(a.get("target_url"))
        out.append({
            "id": r["action_id"], "type": a.get("type"), "status": r["status"],
            "text": r["text"] or a.get("text", ""), "edited": bool(r["text"]),
            "target": {"status_id": sid, "url": a.get("target_url"), "author": a.get("target_handle")} if sid else None,
            "reasons": (last_event.get(r["action_id"]) or {}).get("reasons") or [],
            "note": r["note"] or "", "reviewed_at": r["reviewed_at"],
            "outcome": outcome(outcomes.get(r["action_id"])) if outcomes is not None else None,
        })
    return out, outcomes is not None


@route("GET", r"/api/history")
def get_history(req: Request):
    found, has_ledger = items()
    return {"items": found, "ledger": has_ledger}
