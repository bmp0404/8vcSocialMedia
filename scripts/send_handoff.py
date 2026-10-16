"""Email the latest (or a given) recommendations file to the posting bot.

Usage:
    python scripts/send_handoff.py [path/to/recommendations-<runid>.json] [--send] [--top N]

The mission name in the subject comes from HARNESS_MISSION (harness.config), same as `harness run`.
Dry run by default (writes out/handoff-<runid>.eml + .txt); it only emails with --send AND SMTP
configured, matching `python -m harness run --email [--send]`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import config  # noqa: E402  (loads .env)
from harness.handoff import deliver, load_recommendations  # noqa: E402


def newest_recommendations() -> Path:
    files = sorted(config.OUT_DIR.glob("recommendations-*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        sys.exit(f"No out/recommendations-*.json found in {config.OUT_DIR}")
    return files[-1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="recommendations JSON (default: newest out/recommendations-*.json)")
    ap.add_argument("--send", action="store_true", help="actually send via SMTP (needs SMTP_* and HANDOFF_EMAIL_TO)")
    ap.add_argument("--dry-run", action="store_true", help="write .eml/.txt to out/ (the default; kept for old scripts)")
    ap.add_argument("--top", type=int, default=10, help="max actions to include (default 10)")
    args = ap.parse_args()

    path = Path(args.path) if args.path else newest_recommendations()
    actions, run_id = load_recommendations(path)
    run_id = run_id or path.stem.removeprefix("recommendations-")
    if not actions:
        sys.exit(f"No non-vetoed actions in {path}")
    if args.send and args.dry_run:
        sys.exit("--send and --dry-run are mutually exclusive")
    deliver(actions, run_id, config.mission_name(), config.OUT_DIR, send_mail=args.send, top=args.top)


if __name__ == "__main__":
    main()
