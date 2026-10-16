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

    args = parser.parse_args()
    if args.cmd == "backfill-x":
        from harness.context import backfill

        backfill.run(limit=args.limit, timelines=not args.no_timelines)


if __name__ == "__main__":
    main()
