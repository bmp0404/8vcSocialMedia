"""python -m harness.corpus snapshot|verify|stats"""

import argparse
import json
from pathlib import Path

from harness.corpus import load
from harness.corpus.snapshot import RAW_DIR, snapshot, verify


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m harness.corpus")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="copy raw fetch files into data/corpus/raw as gzip")
    s.add_argument("--src", type=Path, required=True, help="the context worker's data/context directory")
    sub.add_parser("verify", help="check the snapshot against MANIFEST.json")
    sub.add_parser("stats", help="print corpus counts by kind and source")
    args = p.parse_args()
    if args.cmd == "snapshot":
        m = snapshot(args.src)
        print(json.dumps({k: {"lines": v["lines"], "gz_bytes": v["gz_bytes"]} for k, v in m["files"].items()}, indent=2))
    elif args.cmd == "verify":
        problems = verify(RAW_DIR)
        if problems:
            raise SystemExit("\n".join(problems))
        print("snapshot matches MANIFEST.json")
    else:
        print(json.dumps(load().counts(), indent=2))


if __name__ == "__main__":
    main()
