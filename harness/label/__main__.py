"""python -m harness.label items|pilot|run|calls|analyze|audit

  items   write data/labels/items.jsonl and schema.json from the corpus
  pilot   choose the pilot items (data/labels/pilot.json)
  run     annotate a set of items with one or more models through the gateway
  calls   calls, tokens and errors per model, from data/labels/calls/
  analyze agreement and validity per field for a run prefix -> data/labels/reports/<prefix>.json
  audit   freeze the 200-item audit set behind queue("audit")
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
    if not args.fresh_window:
        guard.seed()
    print(f"[label] {len(items)} items, {used} calls used so far, {guard.budget_left} allowed this run")
    if guard.stopped:
        print(f"[label] already stopped from the call logs: {guard.stopped}")
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


def call_totals() -> dict:
    per = defaultdict(lambda: Counter())
    for path in sorted(annotate.CALLS_DIR.glob("*.jsonl")):
        for row in map(json.loads, path.open(encoding="utf-8")):
            c = per[row["model"]]
            c["calls"] += 1
            c["ok"] += bool(row.get("ok"))
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                c[k] += row.get(k) or 0
            if not row.get("ok"):
                # A row with a response model got an answer that failed validation; the rest are HTTP or network errors.
                c["error:" + ("invalid_answer" if row.get("response_model") else str(row.get("error"))[:40])] += 1
    return {m: dict(c) for m, c in sorted(per.items())}


def _analyze(prefix: str, reference: list[str], version: int) -> None:
    from harness.label import analyze
    from harness.label.schema import SCHEMA, SCHEMA_V1
    schema = SCHEMA_V1 if version == 1 else SCHEMA
    runs = analyze.load_runs(prefix)
    report = {"prefix": prefix, "schema_version": schema["version"],
              "items_per_model": {m: len(v) for m, v in runs.items()},
              "reference": reference, "agreement": analyze.agreement_table(runs, schema),
              "validity_reference": analyze.validity(runs, reference, schema),
              "distributions": analyze.distributions(runs, schema), "calls": call_totals()}
    out = annotate.LLM_DIR.parent / "reports" / f"{prefix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"[label] wrote {out}")


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
    r.add_argument("--fresh-window", action="store_true",
                   help="ignore logged failures; use only after checking that a stopped model answers again")
    sub.add_parser("calls")
    a = sub.add_parser("analyze")
    a.add_argument("--prefix", required=True)
    a.add_argument("--reference", default="claude-sonnet-5-5,gpt-6.1-sol", help="the two models validity averages")
    a.add_argument("--schema-version", type=int, default=2, help="1 for the pilot's original nine fields")
    sub.add_parser("audit")
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
    elif args.cmd == "analyze":
        _analyze(args.prefix, args.reference.split(","), args.schema_version)
    elif args.cmd == "audit":
        from harness.label.store import Store
        print(json.dumps({k: v for k, v in Store().build_audit().items() if k != "items"}, indent=1))
    else:
        print(json.dumps(call_totals(), indent=2))


if __name__ == "__main__":
    main()
