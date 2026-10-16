"""The queue: what a person can decide on now, and the verdict route that records each decision.

Hot targets come first, newest post first, then the pending drafts of recent rounds, newest round first
and best predicted first within a round. A card leaves the queue once its action has a verdict.

The queue leaves out what a person can't post as written: follows, which carry no text, and drafts the
critic vetoed. Their records stay in memory.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from harness import config, memory
from harness.minds.gate import verify_markers
from harness.outcomes.handoff import status_id
from harness.review import check_approval, export_approved, hot
from harness.review.routes import HTTPError, Request, route
from harness.sources.watch import account_handle
from harness.xtext import MAX_WEIGHTED_CHARS

# How the page names each kill reason. The codes are memory.REVIEW_REASONS; a test keeps the two equal.
KILL_REASONS = {
    "off_message": "Off-message",
    "factually_shaky": "Shaky fact",
    "stale_story": "Stale story",
    "wrong_target": "Wrong target",
    "slop": "Reads as AI",
    "tone": "Wrong tone",
    "other": "Other",
}
TEXT_TYPES = {"post", "reply", "quote", "mention"}
# A mock round's drafts are placeholder text that nobody posts. On sam/platform's data/harness.db on 2026-10-16,
# 109 of the 110 pending drafts came from mock rounds and buried the one live draft, so they are shown only
# when the harness itself runs on the mock provider, as in the README's offline walkthrough.
MOCK = "mock"
# A round's pending drafts stay in the queue this long. No measurement chose it: replies land a median 28
# minutes after the post (action/DESIGN.md), so a reply draft a day old is almost always too late, and the
# window only keeps a week of unreviewed rounds from burying today's.
WINDOW = timedelta(hours=24)
_AUTHOR = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/(\w+)/status")


def author_of(url: str | None) -> str | None:
    m = _AUTHOR.match(url or "")
    return m[1] if m else None


def _target(url: str | None, handle: str | None) -> dict | None:
    sid = status_id(url)
    return {"status_id": sid, "url": url, "author": handle or author_of(url)} if sid else None


def _decided() -> set[str]:
    with memory.connect() as conn:
        return {r[0] for r in conn.execute("SELECT action_id FROM reviews")}


def hot_cards(decided: set[str]) -> tuple[list[dict], list[str]]:
    records, problems = hot.offered()
    cards = []
    for rec in records:
        aid = hot.action_id(str(rec["status_id"]))
        if aid in decided:
            continue
        target = rec.get("target") or {}
        draft = rec.get("draft") if rec["offer"] == "draft" else None
        cards.append({
            "id": aid, "source": "hot", "type": "reply", "arm": rec["arm"], "method": rec.get("method"),
            "profile": rec["profile"], "account": _account(rec["profile"]), "draft": draft,
            "verify": verify_markers(draft or ""),
            "target": {"status_id": str(rec["status_id"]), "url": rec["url"], "author": target.get("author")},
            "context": None, "at": target.get("created_at") or rec["finished_at"],
        })
    cards.sort(key=lambda c: c["at"], reverse=True)
    return cards, problems


def _account(profile: str) -> str | None:
    with config.use_profile(profile):
        return account_handle()[0] or None


def run_cards(decided: set[str], now: datetime | None = None) -> list[dict]:
    since = ((now or datetime.now(timezone.utc)) - WINDOW).isoformat(timespec="seconds")
    with memory.connect() as conn:
        rows = conn.execute(
            """SELECT a.id, a.run_id, a.data, r.started_at FROM actions a JOIN runs r ON r.id = a.run_id
               WHERE r.started_at >= ? AND COALESCE(r.sources, '') != 'watch' AND (? OR r.provider != ?)
               ORDER BY a.run_id DESC, a.predicted_score DESC""",
            (since, config.PROVIDER == MOCK, MOCK),
        ).fetchall()
    accounts: dict[int, str | None] = {}
    cards = []
    for r in rows:
        a = json.loads(r["data"])
        if r["id"] in decided or a.get("vetoed") or a.get("type") not in TEXT_TYPES or not (a.get("text") or "").strip():
            continue
        if r["run_id"] not in accounts:
            accounts[r["run_id"]] = memory.run_account(r["run_id"]) or account_handle()[0] or None
        kind = a["type"]
        target = _target(a.get("target_url"), a.get("target_handle")) if kind in ("reply", "quote") else None
        cards.append({
            "id": r["id"], "source": "run", "run_id": r["run_id"], "type": kind, "arm": None,
            "method": (a.get("draft_meta") or {}).get("method") or a.get("mind"), "profile": None,
            "account": accounts[r["run_id"]], "draft": a["text"], "verify": verify_markers(a["text"]),
            "target": target,
            # A post or mention answers a story, not an X post; target_text is that story as decide saw it.
            "context": None if target else (a.get("target_text") or None),
            "at": r["started_at"],
        })
    return cards


@route("GET", r"/api/queue")
def get_queue(req: Request):
    decided = _decided()
    hot_list, problems = hot_cards(decided)
    return {"cards": hot_list + run_cards(decided), "problems": problems, "reasons": KILL_REASONS,
            "limit": MAX_WEIGHTED_CHARS}


@route("POST", r"/api/actions/([\w-]+)/review")
def post_review(req: Request):
    """Record a verdict: `{status: approved|killed|pending, text?, note?, reasons?}`, the body the original
    dashboard sent. `text` is the words to post when they differ from the draft (an edit or a person's own
    take); `pending` undoes the last verdict. Every verdict rewrites the run's hand-off file."""
    aid, body = req.match[1], req.body
    status, text, note, reasons = body.get("status", ""), body.get("text"), body.get("note") or "", body.get("reasons") or []
    if text is not None and not isinstance(text, str):
        raise HTTPError(400, "text must be a string or null")
    if not isinstance(note, str):
        raise HTTPError(400, "note must be a string")
    if not isinstance(reasons, list) or not all(isinstance(r, str) and r in KILL_REASONS for r in reasons):
        raise HTTPError(400, f"reasons must be a list drawn from {sorted(KILL_REASONS)}")
    try:
        if sid := hot.status_id(aid):
            hot.ensure_action(sid)
        if status == "approved" and not (text or memory.action_text(aid)).strip():
            raise ValueError("there is nothing to post yet: write the reply first")
        check_approval(aid, status, text)
        run_id = memory.set_review(aid, status, text, note, reasons=reasons)
    except KeyError:
        raise HTTPError(404, f"unknown action {aid}") from None
    except ValueError as e:
        raise HTTPError(400, str(e)) from None
    return {"ok": True, "export": str(export_approved(run_id))}
