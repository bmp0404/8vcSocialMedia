"""One sync pass, and the per-action view `harness outcomes status` prints.

A pass reads the approved hand-off files, reads the account's timeline when that's due, matches
approved actions to posts, marks actions the bot skipped, fetches each matched post's counts when a
checkpoint falls due, labels the new snapshots and records every changed outcome in memory.
"""

import json
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import memory
from harness.outcomes import ledger
from harness.outcomes.handoff import load_approved
from harness.outcomes.label import (CHECKPOINTS, FINAL, INSUFFICIENT, WINDOW_FACTOR, age_hours, baseline,
                                    checkpoint_for, checkpoint_name, label)
from harness.outcomes.match import UNMATCHABLE_TYPES, Match, match
from harness.outcomes.reader import ReaderMissing, Timeline, fx_reader

# The task's definition of a skipped action: approved, and no matching post 24 hours later.
NOT_POSTED_AFTER = timedelta(hours=24)
# Half the +1h window (1-2 hours old), so a sync run every half hour reads each new post inside it.
TIMELINE_EVERY = timedelta(minutes=30)
# How long to keep looking for an approved action's post, and to keep reading the timeline for a
# matched post's checkpoints. Uncalibrated.
TRACK_FOR = timedelta(days=7)
DEFAULT_LIMIT = 60  # own posts per timeline read, about 3 fxtwitter pages


@dataclass
class SyncReport:
    handle: str
    actions: int = 0
    timeline: str = ""
    new_posts: int = 0
    matched: list[Match] = field(default_factory=list)
    not_posted: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)  # "action_id +6h"
    requests: int = 0
    recorded: int = 0
    errors: list[str] = field(default_factory=list)


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _open_actions(conn, now: datetime) -> list:
    """Actions whose post may still appear, or whose post still has a checkpoint ahead."""
    by_action = {m["action_id"]: m for m in ledger.matched(conn)}
    out = []
    for a in ledger.actions(conn):
        if a["type"] in UNMATCHABLE_TYPES:
            continue
        m = by_action.get(a["action_id"])
        if m is None:
            if now - utc(a["approved_at"]) < TRACK_FOR:
                out.append(a)
        elif not ledger.has_snapshot(conn, m["status_id"], CHECKPOINTS[-1]) and now - utc(m["created_at"]) < TRACK_FOR:
            out.append(a)
    return out


def _timeline_due(conn, now: datetime) -> str | None:
    """None when a read is due, else why not."""
    if not _open_actions(conn, now):
        return "no approved action is waiting to be found or measured"
    last = ledger.last_read(conn)
    if last and now - utc(last["at"]) < TIMELINE_EVERY:
        return f"last read {(now - utc(last['at'])).total_seconds() / 60:.0f} min ago"
    return None


def _save_timeline(conn, tl: Timeline) -> int:
    new = ledger.save_posts(conn, tl.posts)
    for p in tl.posts:
        age = age_hours(p.created_at, utc(p.counts.fetched_at))
        cp = checkpoint_for(age)
        if cp is not None:
            ledger.save_baseline(conn, p.id, cp, age, p.counts)
    ledger.save_read(conn, tl)
    return new


def _mark_not_posted(conn, tl: Timeline, report: SyncReport) -> None:
    """Unmatched after 24h, and this read reached back past the approval, so the post would be in it."""
    read_at = utc(tl.fetched_at)
    oldest = min((p.created_at for p in tl.posts), default=None)
    done = {m["action_id"] for m in ledger.matched(conn)}
    for a in ledger.actions(conn):
        if a["action_id"] in done or a["type"] in UNMATCHABLE_TYPES or a["not_posted_at"]:
            continue
        approved = utc(a["approved_at"])
        covered = tl.exhausted or (oldest is not None and oldest <= approved)
        if read_at - approved >= NOT_POSTED_AFTER and covered:
            ledger.set_not_posted(conn, a["action_id"], tl.fetched_at)
            report.not_posted.append(a["action_id"])


def _refresh_labels(conn, now: datetime) -> None:
    """Label every snapshot. success and failure are final; insufficient_baseline is retried each pass."""
    points: dict[int, list] = {}
    for m in ledger.matched(conn):
        existing = ledger.labels(conn, m["action_id"])
        for s in ledger.snapshots(conn, m["status_id"]):
            cp = s["checkpoint"]
            old = existing.get(cp)
            if old is not None and old["outcome"] in FINAL:
                continue
            if cp not in points:
                points[cp] = ledger.baseline_points(conn, cp)
            values = baseline(points[cp], m["status_id"], utc(m["created_at"]), m["kind"])
            lab = label(s["likes"] + s["reposts"] + s["replies"] + s["quotes"], values)
            if old is None or (old["outcome"], old["baseline_n"]) != (lab.outcome, lab.baseline_n):
                ledger.save_label(conn, m["action_id"], cp, lab, now)


def _point(s, lab) -> dict:
    return {
        "checkpoint": checkpoint_name(s["checkpoint"]),
        "age_hours": s["age_hours"],
        "fetched_at": s["fetched_at"],
        **{k: s[k] for k in ("likes", "reposts", "replies", "quotes", "views", "author_followers")},
        "engagement": s["likes"] + s["reposts"] + s["replies"] + s["quotes"],
        "outcome": lab["outcome"] if lab else INSUFFICIENT,
        **{k: lab[k] if lab else None for k in ("baseline_n", "baseline_median", "baseline_p10", "baseline_p90")},
    }


def payload(conn, action, m, handle: str) -> dict | None:
    """The metrics dict memory stores for one action, or None when there's nothing to record yet.

    The top level describes one checkpoint: the latest one with a success or failure label, else the
    latest snapshot. `history` keeps every checkpoint, because memory's outcomes table holds one row
    per action. A post found before its first checkpoint records `outcome: posted`, so a late post
    replaces an earlier `not_posted` at once.
    """
    if m is not None:
        labs = ledger.labels(conn, action["action_id"])
        history = [_point(s, labs.get(s["checkpoint"])) for s in ledger.snapshots(conn, m["status_id"])]
        top = next((h for h in reversed(history) if h["outcome"] in FINAL), history[-1] if history else
                   {"outcome": "posted"})
        return {**top, "status_id": m["status_id"], "url": f"https://x.com/{handle}/status/{m['status_id']}",
                "posted_at": m["created_at"], "approved_at": action["approved_at"], "history": history}
    if action["not_posted_at"]:
        return {"outcome": "not_posted", "approved_at": action["approved_at"], "checked_at": action["not_posted_at"]}
    return None


def _stored_outcomes(action_ids: list[str]) -> dict[str, dict]:
    """What memory's outcomes table holds now. Compared against, so a deleted harness.db gets refilled."""
    if not action_ids:
        return {}
    with closing(memory.connect()) as conn:
        rows = conn.execute(
            f"SELECT action_id, metrics FROM outcomes WHERE action_id IN ({','.join('?' * len(action_ids))})",
            action_ids,
        ).fetchall()
    return {r["action_id"]: json.loads(r["metrics"]) for r in rows}


def sync(handle: str, approved_dir: Path, ledger_path: Path = ledger.DEFAULT_PATH, *, reader_factory=fx_reader,
         now: datetime | None = None, limit: int = DEFAULT_LIMIT) -> SyncReport:
    now = now or datetime.now(timezone.utc)
    approved = load_approved(approved_dir)
    report = SyncReport(handle, actions=len(approved))
    reader = None

    def get_reader():
        nonlocal reader
        if reader is None:
            reader = reader_factory()
        return reader

    with closing(ledger.connect(ledger_path)) as conn:
        with conn:
            ledger.claim_handle(conn, handle, ledger_path)
            ledger.save_actions(conn, approved)

        tl = None
        why_not = _timeline_due(conn, now)
        if why_not:
            report.timeline = f"not read: {why_not}"
        else:
            try:
                tl = get_reader().timeline(handle, limit)
            except ReaderMissing:
                raise
            except RuntimeError as e:  # the reader's FetchError
                report.errors.append(f"timeline @{handle}: {e}")
            if tl is not None:
                with conn:
                    report.new_posts = _save_timeline(conn, tl)
                oldest = min((p.created_at for p in tl.posts), default=None)
                reach = "the account's first post" if tl.exhausted else (ledger.iso(oldest) if oldest else "nothing")
                report.timeline = f"read {len(tl.posts)} posts in {tl.requests} requests, back to {reach}"

        with conn:
            done = {m["action_id"] for m in ledger.matched(conn)}
            waiting = [ledger.to_action(a) for a in ledger.actions(conn)
                       if a["action_id"] not in done and now - utc(a["approved_at"]) < TRACK_FOR]
            report.matched = match(waiting, ledger.unmatched_posts(conn))
            ledger.save_matches(conn, report.matched, now)
            if tl is not None:
                _mark_not_posted(conn, tl, report)

        for m in ledger.matched(conn):
            created = utc(m["created_at"])
            cp = checkpoint_for(age_hours(created, now))
            if cp is None or ledger.has_snapshot(conn, m["status_id"], cp):
                continue
            try:
                counts = get_reader().status(m["status_id"])
            except ReaderMissing:
                raise
            except RuntimeError as e:
                with conn:
                    ledger.set_error(conn, m["action_id"], str(e), now)
                report.errors.append(f"status {m['status_id']} (action {m['action_id']}): {e}")
                continue
            age = age_hours(created, utc(counts.fetched_at))
            fetched_cp = checkpoint_for(age)
            with conn:
                if fetched_cp is not None and ledger.save_snapshot(conn, m["status_id"], fetched_cp, age, counts):
                    report.snapshots.append(f"{m['action_id']} {checkpoint_name(fetched_cp)}")
                ledger.set_error(conn, m["action_id"], None, now)

        with conn:
            _refresh_labels(conn, now)
        by_action = {m["action_id"]: m for m in ledger.matched(conn)}
        payloads = {a["action_id"]: payload(conn, a, by_action.get(a["action_id"]), handle)
                    for a in ledger.actions(conn)}

    payloads = {k: v for k, v in payloads.items() if v is not None}
    stored = _stored_outcomes(sorted(payloads))
    changed = [{"action_id": k, "metrics": v} for k, v in payloads.items() if stored.get(k) != v]
    if changed:
        memory.record_outcomes(changed)
    report.recorded = len(changed)
    report.requests = reader.requests if reader is not None else 0
    return report


def _missed_and_next(conn, status_id: str, age: float) -> tuple[list[str], str]:
    taken = {s["checkpoint"] for s in ledger.snapshots(conn, status_id)}
    missed = [checkpoint_name(t) for t in CHECKPOINTS[:-1] if t not in taken and age >= t * WINDOW_FACTOR]
    ahead = [t for t in CHECKPOINTS if t not in taken and (t == CHECKPOINTS[-1] or age < t * WINDOW_FACTOR)]
    if not ahead:
        return missed, ""
    t = ahead[0]
    return missed, f"{checkpoint_name(t)} due now" if age >= t else f"{checkpoint_name(t)} in {t - age:.1f}h"


def status_rows(ledger_path: Path = ledger.DEFAULT_PATH, now: datetime | None = None) -> tuple[dict, list[dict]]:
    """(summary, one row per approved action) from the ledger alone. Makes no request."""
    now = now or datetime.now(timezone.utc)
    with closing(ledger.connect(ledger_path)) as conn:
        handle = ledger.handle(conn) or ""
        last = ledger.last_read(conn)
        by_action = {m["action_id"]: m for m in ledger.matched(conn)}
        rows = []
        for a in ledger.actions(conn):
            m = by_action.get(a["action_id"])
            approved = utc(a["approved_at"])
            row = {"action_id": a["action_id"], "run_id": a["run_id"], "type": a["type"], "text": a["text"],
                   "approved_at": a["approved_at"], "status_id": None, "url": None, "posted_at": None,
                   "hours_to_post": None, "latest": None, "label": None, "missed": [], "next": "", "error": None}
            if a["type"] in UNMATCHABLE_TYPES:
                row.update(state="untracked", detail=f"a {a['type']} leaves no post; sync can't check it yet")
            elif m is not None:
                created = utc(m["created_at"])
                p = payload(conn, a, m, handle)
                row.update(state="posted", status_id=m["status_id"], url=f"https://x.com/{handle}/status/{m['status_id']}",
                           posted_at=m["created_at"], hours_to_post=round(age_hours(approved, created), 1),
                           error=m["error"],
                           detail=f"matched {'by target and text' if m['anchored'] else 'by text'}, similarity {m['score']}")
                if p["history"]:
                    row["latest"] = {k: p[k] for k in ("checkpoint", "age_hours", "likes", "reposts", "replies", "quotes",
                                                       "views", "author_followers", "engagement")}
                    row["label"] = {k: p[k] for k in ("outcome", "checkpoint", "baseline_n", "baseline_median")}
                row["missed"], row["next"] = _missed_and_next(conn, m["status_id"], age_hours(created, now))
            elif a["not_posted_at"]:
                row.update(state="not_posted", label={"outcome": "not_posted"},
                           detail=f"no matching post by {a['not_posted_at']}, {NOT_POSTED_AFTER.total_seconds() / 3600:.0f}h+ after approval")
            elif now - approved < NOT_POSTED_AFTER:
                row.update(state="waiting", detail=f"approved {age_hours(approved, now):.1f}h ago, not on the timeline yet")
            else:
                reach = f"the last read reached back to {last['oldest']}" if last else "the timeline was never read"
                row.update(state="unverified", detail=f"approved {age_hours(approved, now):.1f}h ago and unmatched, "
                                                      f"but {reach}; run sync, with a larger --limit if needed")
            rows.append(row)
    summary = {"handle": handle, "ledger": str(ledger_path), "last_read": dict(last) if last else None,
               "counts": {s: sum(r["state"] == s for r in rows)
                          for s in ("posted", "not_posted", "waiting", "unverified", "untracked")}}
    return summary, rows
