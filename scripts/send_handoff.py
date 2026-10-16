"""Email the latest (or a given) recommendations file to the posting bot.

Usage:
    python scripts/send_handoff.py [path/to/recommendations-<runid>.json] [--dry-run] [--top N]

Dry-run (writes out/handoff-<runid>.eml + .txt) is automatic when SMTP is not configured.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from harness.handoff import dry_run, load_recommendations, render, send, smtp_configured  # noqa: E402

OUT_DIR = ROOT / "out"
MISSION_PATH = ROOT / "mission" / "mission.md"


def newest_recommendations() -> Path:
    files = sorted(OUT_DIR.glob("recommendations-*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        sys.exit(f"No out/recommendations-*.json found in {OUT_DIR}")
    return files[-1]


def mission_name() -> str:
    try:
        for line in MISSION_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                name = line.lstrip("#").strip()
                return re.sub(r"^mission:\s*", "", name, flags=re.I) or "mission"
    except OSError:
        pass
    return "mission"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="recommendations JSON (default: newest out/recommendations-*.json)")
    ap.add_argument("--dry-run", action="store_true", help="write .eml/.txt to out/ instead of sending")
    ap.add_argument("--top", type=int, default=10, help="max actions to include (default 10)")
    args = ap.parse_args()

    path = Path(args.path) if args.path else newest_recommendations()
    actions, run_id = load_recommendations(path)
    run_id = run_id or path.stem.removeprefix("recommendations-")
    actions = actions[: max(args.top, 0)]
    if not actions:
        sys.exit(f"No non-vetoed actions in {path}")

    subject, text, html = render(actions, run_id, mission_name())
    attachments = [
        (f"recommendations-{run_id}.json", json.dumps(actions, indent=2).encode("utf-8"), "application/json")
    ]

    if args.dry_run or not smtp_configured():
        reason = "--dry-run" if args.dry_run else "SMTP not configured"
        eml, txt = dry_run(subject, text, html, attachments, OUT_DIR, f"handoff-{run_id}")
        print(f"[handoff] dry run ({reason}); {len(actions)} actions")
        print(f"[handoff] eml:     {eml}")
        print(f"[handoff] preview: {txt}")
        return

    msg = send(subject, text, html, attachments)
    rcpts = ", ".join(x for x in (msg.get("To"), msg.get("Cc")) if x)
    print(f"[handoff] sent {len(actions)} actions to {rcpts}: {subject}")


if __name__ == "__main__":
    main()
