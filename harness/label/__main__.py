"""python -m harness.label items|pilot|run|calls

  items   write data/labels/items.jsonl and schema.json from the corpus
  pilot   choose the pilot items (data/labels/pilot.json)
  run     annotate a set of items with one or more models through the gateway
  calls   calls, tokens and errors per model, from data/labels/calls/
"""

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path

from harness.label import annotate, pilot
from harness.label.items import ITEMS_PATH, export_items
from harness.label.schema import write_schema


def _items_by_id(path: Path = ITEMS_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        return {row["item_id"]: row for row in map(json.loads, f)}


def _id_set(name: str, by_id: dict) -> list[str]:
    if name == "pilot":
        return pilot.load()
    if name == "all":
        return list(by_id)
    path = Path(name)
    if path.is_file():
        return [line.strip() for line in path.open(encoding="utf-8") if line.strip()]
    raise SystemExit(f"unknown item set {name!r}: use pilot, all, or a file of item IDs")


async def _run(args) -> None:
    by_id = _items_by_id()
    ids = _id_set(args.set, by_id)
    if args.limit:
        ids = ids[: args.limit]
    items = [by_id[i] for i in ids]
    used = annotate.calls_used()
    guard = annotate.Guard(budget_left=min(annotate.CALL_BUDGET - used, args.max_calls or annotate.CALL_BUDGET))
    print(f"[label] {len(items)} items, {used} calls used so far, {guard.budget_left} allowed this run")
    lanes = dict(part.split("=") for part in args.concurrency.split(",")) if args.concurrency else {}
    tasks, names = [], []
    for model in args.models.split(","):
        run_id = f"{args.run_prefix}-{model}"
        ann = annotate.Annotator(model=model, effort=args.effort or None)
        tasks.append(annotate.run_annotator(ann, run_id, items, by_id, guard, int(lanes.get(model, 8))))
        names.append(run_id)
    results = await asyncio.gather(*tasks)
    for name, stats in zip(names, results):
        print(f"[label] {name}: {json.dumps(stats.to_dict())}")
    if guard.stopped:
        raise SystemExit(f"[label] stopped: {guard.stopped}")


def _calls() -> None:
    per = defaultdict(lambda: Counter())
    for path in sorted(annotate.CALLS_DIR.glob("*.jsonl")):
        for row in map(json.loads, path.open(encoding="utf-8")):
            c = per[row["model"]]
            c["calls"] += 1
            c["ok"] += bool(row.get("ok"))
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                c[k] += row.get(k) or 0
            if not row.get("ok"):
                c[f"error:{row.get('error')}"[:60]] += 1
    print(json.dumps({m: dict(c) for m, c in per.items()}, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m harness.label")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("items")
    sub.add_parser("pilot")
    r = sub.add_parser("run")
    r.add_argument("--set", default="pilot", help="pilot, all, or a file with one item ID per line")
    r.add_argument("--models", required=True, help="comma-separated gateway model IDs")
    r.add_argument("--run-prefix", required=True, help="run_id is <prefix>-<model>")
    r.add_argument("--effort", default="low", help="output_config.effort; empty to omit")
    r.add_argument("--concurrency", default="", help="per-model lanes, e.g. claude-sonnet-5-5=6,gpt-6.1-sol=10")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--max-calls", type=int, default=0, help="cap for this run, below the global budget")
    sub.add_parser("calls")
    args = p.parse_args()
    if args.cmd == "items":
        from harness.corpus import load
        print(f"[label] wrote {export_items(load())} items")
        write_schema()
    elif args.cmd == "pilot":
        from harness.corpus import load
        sel = pilot.select(load())
        pilot.write(sel)
        print(json.dumps(sel["counts"], indent=1))
    elif args.cmd == "run":
        asyncio.run(_run(args))
    else:
        _calls()


if __name__ == "__main__":
    main()
