"""`harness outcomes sync` and `harness outcomes status`."""

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
        r = sync(handle, args.approved_dir, args.ledger, limit=args.limit)
    except (HandleError, HandoffError, ReaderMissing, ledger.LedgerHandleError) as e:
        raise SystemExit(f"[outcomes] {e}") from e
    print(f"[outcomes] {r.actions} approved actions in {args.approved_dir}")
    print(f"[outcomes] timeline: {r.timeline}" + (f"; {r.new_posts} posts not seen before" if r.new_posts else ""))
    for m in r.matched:
        print(f"[outcomes] matched {m.action_id} -> status {m.status_id} "
              f"({'reply/quote target and text' if m.anchored else 'text'}, similarity {m.score})")
    for action_id in r.not_posted:
        print(f"[outcomes] NOT POSTED: {action_id} has no matching post 24h after approval")
    if r.snapshots:
        print(f"[outcomes] snapshots: {', '.join(r.snapshots)}")
    print(f"[outcomes] {r.requests} fxtwitter requests; recorded {r.recorded} changed outcomes in {config.DB_PATH}")
    for e in r.errors:
        print(f"[outcomes] ERROR {e}")
    if r.errors:
        raise SystemExit(1)


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
        print(f"{row['action_id']}  run {row['run_id']}  {row['type']:<7} {row['state'].upper()}  {row['detail']}")
        if row["state"] == "posted":
            print(f"    {row['url']} posted {row['posted_at']}, {row['hours_to_post']}h after approval")
            print(f"    {_fmt_latest(row)}")
            extra = [f"missed {', '.join(row['missed'])}" if row["missed"] else "", f"next {row['next']}" if row["next"] else ""]
            if any(extra):
                print(f"    {'; '.join(e for e in extra if e)}")
            if row["error"]:
                print(f"    ERROR on last fetch: {row['error']}")
        print(f"    {row['text'][:100]!r}")


def add_commands(sub) -> None:
    p = sub.add_parser("outcomes", help="find approved actions on the account's X timeline and record how they did")
    osub = p.add_subparsers(dest="outcomes_cmd", required=True)

    s = osub.add_parser("sync", help="match approved actions to real posts, snapshot due checkpoints, record outcomes")
    s.add_argument("--handle", help="read this X account instead of mission/observe.toml or action/account.json")
    s.add_argument("--approved-dir", type=Path, default=config.OUT_DIR, help="where approved-run<id>.json files are")
    s.add_argument("--ledger", type=Path, default=ledger.DEFAULT_PATH, help="the sync state, one file per account")
    s.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="own posts to read from the timeline")
    s.set_defaults(func=_sync)

    s = osub.add_parser("status", help="print each approved action: posted or not, latest metrics, label")
    s.add_argument("--ledger", type=Path, default=ledger.DEFAULT_PATH)
    s.add_argument("--json", action="store_true", help="print the rows as JSON")
    s.set_defaults(func=_status)
