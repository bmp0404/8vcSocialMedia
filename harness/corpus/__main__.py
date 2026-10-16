"""python -m harness.corpus snapshot|verify|stats|bench|rebuild"""

import argparse
import json
from pathlib import Path

from harness.corpus import bench, load
from harness.corpus.snapshot import RAW_DIR, snapshot, verify


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m harness.corpus")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="copy raw fetch files into data/corpus/raw as gzip")
    s.add_argument("--src", type=Path, required=True, help="the context worker's data/context directory")
    sub.add_parser("verify", help="check the snapshot against MANIFEST.json")
    sub.add_parser("stats", help="print corpus counts by kind and source")
    sub.add_parser("bench", help="rebuild data/bench/ pairs and manifest from the corpus")
    rb = sub.add_parser("rebuild", help="rebuild every gitignored local file: the snapshot, items.jsonl, rationales")
    rb.add_argument("--src", type=Path, help="the context worker's data/context; omit to keep the current snapshot")
    rb.add_argument("--rationales-from", default="", help="local git ref with the unfiltered LLM rows, e.g. sam/corpus")
    args = p.parse_args()
    if args.cmd == "snapshot":
        m = snapshot(args.src)
        print(json.dumps({k: {"lines": v["lines"], "gz_bytes": v["gz_bytes"]} for k, v in m["files"].items()}, indent=2))
    elif args.cmd == "verify":
        problems = verify(RAW_DIR)
        if problems:
            raise SystemExit("\n".join(problems))
        print("snapshot matches MANIFEST.json")
    elif args.cmd == "rebuild":
        from harness.label.items import export_items
        if args.src:
            snapshot(args.src)
        problems = verify(RAW_DIR)
        if problems:
            raise SystemExit("snapshot does not match MANIFEST.json:\n" + "\n".join(problems))
        print(f"snapshot ok; wrote {export_items(load())} items to data/labels/items.jsonl")
        if args.rationales_from:
            from harness.label.local import rationales_from_ref
            n = rationales_from_ref(args.rationales_from)
            print(f"restored {sum(n.values())} rationales from {args.rationales_from} into data/labels/rationales/")
    elif args.cmd == "bench":
        m = bench.build(load())
        for name in ("replies", "posts"):
            groups = m[name].get("threads") or m[name].get("authors")
            by_split = {s: sum(1 for g in groups.values() if g["split"] == s) for s in ("dev", "test")}
            print(f"{name}: {m[name]['pairs']} pairs over {len(groups)} groups {by_split}")
    else:
        print(json.dumps(load().counts(), indent=2))


if __name__ == "__main__":
    main()
