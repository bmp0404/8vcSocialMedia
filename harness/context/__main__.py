"""`python -m harness.context <command>`: context collection, enrichment and tracking.

Kept out of harness/cli.py so this package merges without touching a shared file.
"""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m harness.context")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backfill-x", help="fetch current X metrics for every dataset post, then author timelines")
    p.add_argument("--limit", type=int, help="fetch at most this many posts (testing)")
    p.add_argument("--no-timelines", action="store_true")

    sub.add_parser("enrich", help="write data/context/devtools_enriched.csv and accounts.csv")

    p = sub.add_parser("check", help="fetch one real data point from every keyless source")
    p.add_argument("only", nargs="*", help="run only these checks")

    p = sub.add_parser("track", help="snapshot tracker for engagement curves (read-only X calls)")
    p.add_argument("action", choices=["add", "watch", "poll", "loop", "report", "list"])
    p.add_argument("targets", nargs="*", help="post URLs/ids for add and report; handles for watch")
    p.add_argument("--every-min", type=int, help="watch: minutes between timeline reads")
    p.add_argument("--interval", type=float, default=60.0, help="loop: seconds between passes")
    p.add_argument("--replies", action="store_true", help="also sample replies, for amplifier attribution")
    p.add_argument("--max-passes", type=int, help="loop: stop after this many passes")
    p.add_argument("--metric", default="engagement")

    args = parser.parse_args()
    if args.cmd == "backfill-x":
        from harness.context import backfill

        backfill.run(limit=args.limit, timelines=not args.no_timelines)
    elif args.cmd == "enrich":
        from harness.context import enrich

        enrich.run()
    elif args.cmd == "track":
        _track(args)
    elif args.cmd == "check":
        from harness.context import live_check

        live_check.run(args.only or None)


def _track(args) -> None:
    import json

    from harness.context.tracker import Tracker
    from harness.context.xreader import FxTwitterReader

    tr = Tracker(FxTwitterReader())
    if args.action == "add":
        for t in args.targets:
            print(f"[tracker] tracking {tr.add(t)}")
    elif args.action == "watch":
        tr.add_watch(args.targets, args.every_min)
        print(f"[tracker] watching {tr.watch()['handles']}")
    elif args.action == "poll":
        print(f"[tracker] {tr.poll_once(with_replies=args.replies)}")
    elif args.action == "loop":
        tr.loop(args.interval, with_replies=args.replies, max_passes=args.max_passes)
    elif args.action == "list":
        snaps = tr.snapshots()
        for pid, meta in tr.tracked().items():
            print(f"{pid}  @{meta['author'] or '?':<16} {meta['created_at'][:16]}  {len(snaps.get(pid, []))} snapshots  {meta['reason']}")
    elif args.action == "report":
        for t in args.targets or list(tr.tracked()):
            pid = tr.add(t) if "/" in t else t
            print(json.dumps(tr.report(pid, args.metric), indent=1, default=str))


if __name__ == "__main__":
    main()
