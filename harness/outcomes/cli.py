"""`harness outcomes sync` and `harness outcomes status`."""

import argparse
import json
from pathlib import Path

from harness import config
from harness.outcomes import ledger
from harness.outcomes.handoff import HandleError, HandoffError, resolve_handle
from harness.outcomes.reader import ReaderMissing
from harness.outcomes.sync import DEFAULT_LIMIT, status_rows, sync


def _sync(args) -> None:
    try:
        handle, source = resolve_handle(args.handle)
        print(f"[outcomes] @{handle} (from {source}), ledger {args.ledger}")
        r = sync(handle, args.approved_dir, args.ledger, limit=args.limit, hot_dir=args.hot_dir)
    except (HandleError, HandoffError, ReaderMissing, ledger.LedgerHandleError) as e:
        raise SystemExit(f"[outcomes] {e}") from e
    print(f"[outcomes] {r.actions - r.hot_targets} approved actions in {args.approved_dir}, "
          f"{r.hot_targets} hot targets in {args.hot_dir}")
    print(f"[outcomes] timeline: {r.timeline}" + (f"; {r.new_posts} posts not seen before" if r.new_posts else ""))
    for m in r.matched:
        print(f"[outcomes] matched {m.action_id} -> status {m.status_id} "
              f"({'reply/quote target and text' if m.anchored else 'text'}, similarity {m.score})")
    for action_id in r.not_posted:
        print(f"[outcomes] NOT POSTED: {action_id} has no matching post 24h after approval")
    if r.snapshots:
        print(f"[outcomes] snapshots: {', '.join(r.snapshots)}")
    for line in r.ranks:
        print(f"[outcomes] rank_in_thread {line}")
    print(f"[outcomes] {r.timeline_reads} timeline reads, {r.status_reads} status reads, "
          f"{r.conversation_reads} conversation reads; "
          f"recorded {r.recorded} changed outcomes in {config.DB_PATH}")
    for e in r.errors:
        print(f"[outcomes] ERROR {e}")
    if r.errors:
        raise SystemExit(1)


def _fmt_rank(row: dict) -> str:
    r, reply = row["rank"], row["reply"]
    when = (f"{reply['minutes_after_parent']:g} min after the parent, {reply['followers_at_match']} followers at match"
            if reply and reply["minutes_after_parent"] is not None else "")
    if not r:
        return "not ranked yet" + (f" ({when})" if when else "")
    where = (f"rank {r['rank']} of {r['thread_replies']} (percentile {r['percentile']:g})" if r["rank"]
             else f"off the first {r['page_replies']} replies (percentile at most {r['percentile_max']:g})")
    return f"{r['checkpoint']}: {where} -> {r['outcome'].upper()}" + (f"; {when}" if when else "")


def _fmt_latest(row: dict) -> str:
    lt, lab = row["latest"], row["label"]
    if not lt:
        return "no checkpoint snapshot yet"
    views = f", {lt['views']} views" if lt["views"] is not None else ""
    text = (f"{lt['checkpoint']} ({lt['age_hours']}h): {lt['likes']} likes, {lt['reposts']} reposts, "
            f"{lt['replies']} replies, {lt['quotes']} quotes{views}; engagement {lt['engagement']}")
    if lab["outcome"] in ("success", "failure"):
        return f"{text} -> {lab['outcome'].upper()} vs median {lab['baseline_median']:g} of {lab['baseline_n']} earlier posts"
    return f"{text} -> {lab['outcome']} ({lab['baseline_n'] or 0} earlier posts at {lab['checkpoint']})"


def _status(args) -> None:
    if not args.ledger.exists():
        raise SystemExit(f"[outcomes] no ledger at {args.ledger}; run `python -m harness outcomes sync` first")
    summary, rows = status_rows(args.ledger)
    if args.json:
        print(json.dumps({"summary": summary, "actions": rows}, indent=2))
        return
    if not rows:
        print(f"[outcomes] {args.ledger} has no approved actions; run `python -m harness outcomes sync` first")
        return
    c, last = summary["counts"], summary["last_read"]
    read = f"last timeline read {last['at']}" if last else "timeline never read"
    print(f"@{summary['handle']}: {len(rows)} approved actions, {c['posted']} posted, {c['not_posted']} NOT POSTED, "
          f"{c['waiting']} waiting, {c['unverified']} unverified, {c['untracked']} untracked; {read}\n")
    for row in rows:
        origin = f"arm {row['arm']}" if row["arm"] else f"run {row['run_id']}"
        print(f"{row['action_id']}  {origin}  {row['type']:<7} {row['state'].upper()}  {row['detail']}")
        if row["state"] == "posted":
            print(f"    {row['url']} posted {row['posted_at']}, {row['hours_to_post']}h after approval")
            print(f"    {_fmt_rank(row) if row['type'] == 'reply' and row['reply'] is not None else _fmt_latest(row)}")
            extra = [f"missed {', '.join(row['missed'])}" if row["missed"] else "", f"next {row['next']}" if row["next"] else ""]
            if any(extra):
                print(f"    {'; '.join(e for e in extra if e)}")
            if row["error"]:
                print(f"    ERROR on last fetch: {row['error']}")
        print(f"    {row['text'][:100]!r}")


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def add_commands(sub) -> None:
    p = sub.add_parser("outcomes", help="find approved actions on the account's X timeline and record how they did")
    osub = p.add_subparsers(dest="outcomes_cmd", required=True)

    s = osub.add_parser("sync", help="match approved actions to real posts, snapshot due checkpoints, record outcomes")
    s.add_argument("--handle", help="read this X account instead of HARNESS_ACCOUNT_HANDLE or [brand] x_handle")
    s.add_argument("--approved-dir", type=Path, default=config.OUT_DIR, help="where approved-run<id>.json files are")
    s.add_argument("--hot-dir", type=Path, default=config.OUT_DIR / "hot",
                   help="the watcher's hot targets, out/hot/<status_id>.json; answered by hand, matched by reply link")
    s.add_argument("--ledger", type=Path, default=ledger.DEFAULT_PATH, help="the sync state, one file per account")
    s.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT,
                   help="feed entries per timeline read; raise it when status says unverified")
    s.set_defaults(func=_sync)

    s = osub.add_parser("status", help="print each approved action: posted or not, latest metrics, label")
    s.add_argument("--ledger", type=Path, default=ledger.DEFAULT_PATH)
    s.add_argument("--json", action="store_true", help="print the rows as JSON")
    s.set_defaults(func=_status)
