"""One sync pass, and the per-action view `harness outcomes status` prints.

A pass reads the approved hand-off files and the watcher's hot targets, reads the account's timeline
when that's due, matches actions to posts, marks actions nobody posted, fetches each matched post's
counts when a checkpoint falls due, ranks each matched reply in its thread at +1h and +24h, labels
the new snapshots and records every changed outcome in memory. Posts and quotes are labeled against
the account's median; replies only by their rank in the thread (rank.py).
"""

import json
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import memory
from harness.context import snowflake
from harness.outcomes import ledger
from harness.outcomes.handoff import load_approved, load_hot
from harness.outcomes.label import (CHECKPOINTS, FINAL, INSUFFICIENT, WINDOW_FACTOR, age_hours, baseline,
                                    checkpoint_for, checkpoint_name, label)
from harness.outcomes.match import UNMATCHABLE_TYPES, Match, match
from harness.outcomes.rank import RANK_CHECKPOINTS, UNRANKED, Rank, rank_in_thread
from harness.outcomes.reader import ReaderMissing, Timeline, fx_reader

# The task's definition of a skipped action: approved, and no matching post 24 hours later.
NOT_POSTED_AFTER = timedelta(hours=24)
# Half the +1h window (1-2 hours old), so a sync run every half hour reads each new post inside it.
TIMELINE_EVERY = timedelta(minutes=30)
# A post made this long after approval still counts as the action's post, and a matched post's
# counts are read for this long after posting. Uncalibrated.
TRACK_FOR = timedelta(days=7)
DEFAULT_LIMIT = 60  # feed entries per timeline read, about 3 fxtwitter pages


@dataclass
class SyncReport:
    handle: str
    actions: int = 0
    timeline: str = ""
    new_posts: int = 0
    matched: list[Match] = field(default_factory=list)
    not_posted: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)  # "action_id +6h"
    ranks: list[str] = field(default_factory=list)  # "action_id +1h: rank 3 of 40"
    hot_targets: int = 0
    timeline_reads: int = 0
    status_reads: int = 0
    conversation_reads: int = 0
    recorded: int = 0
    errors: list[str] = field(default_factory=list)


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _open_actions(conn, now: datetime) -> list:
    """Actions with no verdict yet, whose post may still appear, or whose post has a checkpoint ahead."""
    by_action = {m["action_id"]: m for m in ledger.matched(conn)}
    out = []
    for a in ledger.actions(conn):
        if a["type"] in UNMATCHABLE_TYPES:
            continue
        m = by_action.get(a["action_id"])
        if m is None:
            if not a["not_posted_at"] or now - utc(a["approved_at"]) < TRACK_FOR:
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
    if tl.reach is None:
        return
    read_at = utc(tl.fetched_at)
    done = {m["action_id"] for m in ledger.matched(conn)}
    for a in ledger.actions(conn):
        if a["action_id"] in done or a["type"] in UNMATCHABLE_TYPES or a["not_posted_at"]:
            continue
        approved = utc(a["approved_at"])
        if read_at - approved >= NOT_POSTED_AFTER and tl.reach <= approved:
            ledger.set_not_posted(conn, a["action_id"], tl.fetched_at)
            report.not_posted.append(a["action_id"])


def _record_reply_matches(conn, new: list[Match], tl: Timeline | None, now: datetime) -> None:
    """For each newly matched reply: its arm, minutes after the parent, and the account's followers then."""
    fresh = {m.action_id for m in new}
    if not fresh:
        return
    followers = {p.id: p.counts.author_followers for p in tl.posts if p.counts} if tl is not None else {}
    arms = {a: r["arm"] for a, r in ledger.hot_targets(conn).items()}
    for m in ledger.matched(conn):
        if m["action_id"] not in fresh or m["kind"] != "reply" or not m["replying_to_status"]:
            continue
        parent_at = snowflake.to_datetime(m["replying_to_status"])
        minutes = round((utc(m["created_at"]) - parent_at).total_seconds() / 60, 2)
        ledger.save_reply_match(conn, m["action_id"], m["status_id"], m["replying_to_status"], arms.get(m["action_id"]),
                                minutes, followers.get(m["status_id"]), now)


def _describe(r: Rank) -> str:
    where = f"rank {r.rank} of {r.thread_replies}" if r.rank else f"off the first page of {r.page_replies}"
    return f"{where}, {r.outcome}"


def _rank_replies(conn, get_reader, now: datetime, report: SyncReport) -> None:
    """Rank each matched reply in its parent's likes-ordered conversation when +1h or +24h falls due."""
    for m in ledger.matched(conn):
        parent = m["replying_to_status"]
        if m["kind"] != "reply" or not parent:
            continue
        created = utc(m["created_at"])
        cp = checkpoint_for(age_hours(created, now))
        if cp not in RANK_CHECKPOINTS or ledger.has_rank(conn, m["action_id"], cp):
            continue
        try:
            page = get_reader().conversation(parent)
            parent_counts = get_reader().status(parent)
        except ReaderMissing:
            raise
        except RuntimeError as e:
            report.errors.append(f"rank of {m['status_id']} under {parent} (action {m['action_id']}): {e}")
            continue
        age = age_hours(created, utc(parent_counts.fetched_at))
        fetched_cp = checkpoint_for(age)
        r = rank_in_thread(page, parent, m["status_id"], parent_counts.replies)
        with conn:
            if fetched_cp in RANK_CHECKPOINTS and ledger.save_rank(conn, m["action_id"], fetched_cp,
                                                                  parent_counts.fetched_at, age, r):
                report.ranks.append(f"{m['action_id']} {checkpoint_name(fetched_cp)}: {_describe(r)}")


def _refresh_labels(conn, now: datetime) -> None:
    """Label every snapshot. success and failure are final; insufficient_baseline is retried each pass.

    Replies are skipped: they are ranked within their thread instead (rank.py), never against the
    account's own median, which for a new account is 0.
    """
    points: dict[int, list] = {}
    for m in ledger.matched(conn):
        if m["kind"] == "reply":
            continue
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


def _counts_point(s) -> dict:
    return {
        "checkpoint": checkpoint_name(s["checkpoint"]),
        "age_hours": s["age_hours"],
        "fetched_at": s["fetched_at"],
        **{k: s[k] for k in ("likes", "reposts", "replies", "quotes", "views", "author_followers")},
        "engagement": s["likes"] + s["reposts"] + s["replies"] + s["quotes"],
    }


def _point(s, lab) -> dict:
    return {
        **_counts_point(s),
        "outcome": lab["outcome"] if lab else INSUFFICIENT,
        **{k: lab[k] if lab else None for k in ("baseline_n", "baseline_median", "baseline_p10", "baseline_p90")},
    }


def _rank_point(r) -> dict:
    return {"checkpoint": checkpoint_name(r["checkpoint"]), "age_hours": r["age_hours"], "fetched_at": r["fetched_at"],
            **{k: r[k] for k in ("rank", "likes_rank", "page_replies", "thread_replies", "percentile", "percentile_max",
                                 "likes", "followers", "outcome")}}


def reply_payload(conn, action, m, handle: str) -> dict:
    """A matched reply's metrics: counts at each checkpoint, and an outcome from its rank in the thread.

    The top level holds the latest counts snapshot and the outcome of the latest rank with a success
    or failure label, else the latest rank, else `posted`.
    """
    ranked = [_rank_point(r) for r in ledger.ranks(conn, action["action_id"])]
    by_cp = {r["checkpoint"]: r["outcome"] for r in ranked}
    history = [{**_counts_point(s), "outcome": by_cp.get(checkpoint_name(s["checkpoint"]), UNRANKED)}
               for s in ledger.snapshots(conn, m["status_id"])]
    top_rank = next((r for r in reversed(ranked) if r["outcome"] in FINAL), ranked[-1] if ranked else None)
    rm = ledger.reply_match(conn, action["action_id"])
    return {**(history[-1] if history else {}),
            "outcome": top_rank["outcome"] if top_rank else "posted", "label_basis": "rank_in_thread",
            "rank_in_thread": ranked, "parent_status": m["replying_to_status"],
            **{k: rm[k] if rm else None for k in ("arm", "minutes_after_parent", "followers_at_match")},
            "status_id": m["status_id"], "url": f"https://x.com/{handle}/status/{m['status_id']}",
            "posted_at": m["created_at"], "approved_at": action["approved_at"], "history": history}


def payload(conn, action, m, handle: str) -> dict | None:
    """The metrics dict memory stores for one action, or None when there's nothing to record yet.

    The top level describes one checkpoint: the latest one with a success or failure label, else the
    latest snapshot. `history` keeps every checkpoint, because memory's outcomes table holds one row
    per action. A post found before its first checkpoint records `outcome: posted`, so a late post
    replaces an earlier `not_posted` at once.
    """
    if m is not None and m["kind"] == "reply":
        return reply_payload(conn, action, m, handle)
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
         now: datetime | None = None, limit: int = DEFAULT_LIMIT, hot_dir: Path | None = None) -> SyncReport:
    """One pass. `hot_dir` holds the watcher's out/hot/<status_id>.json files; None leaves hot targets out."""
    now = now or datetime.now(timezone.utc)
    approved = load_approved(approved_dir, handle)
    hot = load_hot(hot_dir, handle) if hot_dir is not None else []
    report = SyncReport(handle, actions=len(approved) + len(hot), hot_targets=len(hot))
    reader = None

    def get_reader():
        nonlocal reader
        if reader is None:
            reader = reader_factory()
        return reader

    with closing(ledger.connect(ledger_path)) as conn:
        with conn:
            ledger.claim_handle(conn, handle, ledger_path)
            ledger.save_actions(conn, approved + [a for a, _ in hot])
            ledger.save_hot_targets(conn, [t for _, t in hot])

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
                reach = ledger.iso(tl.reach) if tl.reach else "nothing"
                report.timeline = f"read {len(tl.posts)} of the account's posts, back to {reach}"

        with conn:
            done = {m["action_id"] for m in ledger.matched(conn)}
            waiting = [ledger.to_action(a) for a in ledger.actions(conn) if a["action_id"] not in done]
            report.matched = match(waiting, ledger.unmatched_posts(conn), TRACK_FOR)
            ledger.save_matches(conn, report.matched, now)
            _record_reply_matches(conn, report.matched, tl, now)
            if tl is not None:
                _mark_not_posted(conn, tl, report)

        for m in ledger.matched(conn):
            created = utc(m["created_at"])
            cp = checkpoint_for(age_hours(created, now))
            # The age limit stops a deleted post from being refetched on every sync.
            if cp is None or now - created >= TRACK_FOR or ledger.has_snapshot(conn, m["status_id"], cp):
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

        _rank_replies(conn, get_reader, now, report)
        with conn:
            _refresh_labels(conn, now)
        by_action = {m["action_id"]: m for m in ledger.matched(conn)}
        # Hot targets have no row in memory's actions table, so their outcomes stay in this ledger.
        hot_ids = set(ledger.hot_targets(conn))
        payloads = {a["action_id"]: payload(conn, a, by_action.get(a["action_id"]), handle)
                    for a in ledger.actions(conn) if a["action_id"] not in hot_ids}

    payloads = {k: v for k, v in payloads.items() if v is not None}
    stored = _stored_outcomes(sorted(payloads))
    changed = [{"action_id": k, "metrics": v} for k, v in payloads.items() if stored.get(k) != v]
    if changed:
        memory.record_outcomes(changed)
    report.recorded = len(changed)
    if reader is not None:
        report.timeline_reads, report.status_reads = reader.timeline_reads, reader.status_reads
        report.conversation_reads = reader.conversation_reads
    return report


def _missed_and_next(conn, status_id: str, age: float) -> tuple[list[str], str]:
    taken = {s["checkpoint"] for s in ledger.snapshots(conn, status_id)}
    tracking = age < TRACK_FOR.total_seconds() / 3600

    def closed(t: int) -> bool:
        return not tracking or (t != CHECKPOINTS[-1] and age >= t * WINDOW_FACTOR)

    missed = [checkpoint_name(t) for t in CHECKPOINTS if t not in taken and closed(t)]
    ahead = [t for t in CHECKPOINTS if t not in taken and not closed(t)]
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
        arms = {a: r["arm"] for a, r in ledger.hot_targets(conn).items()}
        rows = []
        for a in ledger.actions(conn):
            m = by_action.get(a["action_id"])
            approved = utc(a["approved_at"])
            row = {"action_id": a["action_id"], "run_id": a["run_id"], "type": a["type"], "text": a["text"],
                   "approved_at": a["approved_at"], "status_id": None, "url": None, "posted_at": None,
                   "hours_to_post": None, "latest": None, "label": None, "missed": [], "next": "", "error": None,
                   "arm": arms.get(a["action_id"]), "rank": None, "reply": None}
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
                if m["kind"] == "reply":
                    row["reply"] = {k: p[k] for k in ("parent_status", "minutes_after_parent", "followers_at_match")}
                    row["rank"] = p["rank_in_thread"][-1] if p["rank_in_thread"] else None
                    row["label"] = {"outcome": p["outcome"], "basis": "rank_in_thread",
                                    "checkpoint": row["rank"]["checkpoint"] if row["rank"] else None}
                elif p["history"]:
                    row["label"] = {k: p[k] for k in ("outcome", "checkpoint", "baseline_n", "baseline_median")}
                row["missed"], row["next"] = _missed_and_next(conn, m["status_id"], age_hours(created, now))
            elif a["not_posted_at"]:
                row.update(state="not_posted", label={"outcome": "not_posted"},
                           detail=f"no matching post by {a['not_posted_at']}, {NOT_POSTED_AFTER.total_seconds() / 3600:.0f}h+ after approval")
            elif now - approved < NOT_POSTED_AFTER:
                row.update(state="waiting", detail=f"approved {age_hours(approved, now):.1f}h ago, not on the timeline yet")
            else:
                reach = f"the last read reached back to {last['reach']}" if last else "the timeline was never read"
                row.update(state="unverified", detail=f"approved {age_hours(approved, now):.1f}h ago and unmatched, "
                                                      f"but {reach}; run sync, with a larger --limit if needed")
            rows.append(row)
    summary = {"handle": handle, "ledger": str(ledger_path), "last_read": dict(last) if last else None,
               "counts": {s: sum(r["state"] == s for r in rows)
                          for s in ("posted", "not_posted", "waiting", "unverified", "untracked")}}
    return summary, rows
