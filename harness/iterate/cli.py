"""`python -m harness iterate ...`: run experiment batches, inspect knobs and the champion, revert."""

import argparse
import json
from pathlib import Path

from harness.iterate import knobs, ledger


def _fitnesses(args) -> dict:
    from harness.iterate.fitness.action import DEFAULT_THREADS, ActionLineup

    out = {"action_lineup": ActionLineup(Path(args.threads) if getattr(args, "threads", None) else DEFAULT_THREADS,
                                         limit=getattr(args, "limit", None))}
    try:
        from harness.iterate.fitness.qualify import QualifyAUC
    except ImportError as e:  # harness.evals lands with sam/eval
        print(f"[iterate] qualify_auc unavailable: {e}")
    else:
        out["qualify_auc"] = QualifyAUC(per_tier=getattr(args, "per_tier", 4))
    return out


def run(args) -> None:
    from harness.iterate.plans import PLANS
    from harness.iterate.runner import run_batch

    if args.plan not in PLANS:
        raise SystemExit(f"unknown plan {args.plan!r}; choose from {sorted(PLANS)}")
    provider = (args.provider or "mock").lower()
    if provider not in ("mock", "anthropic"):
        raise SystemExit("iterate runs on --provider mock or anthropic (the gateway serves every model it tests)")
    recs = run_batch(PLANS[args.plan], _fitnesses(args), provider=provider, max_calls=args.max_calls, seed=args.seed,
                     run_note=args.note or f"plan {args.plan}")
    for r in recs:
        m = next((x for x in r["eval"]["metrics"] if x.get("primary")), None)
        line = f"{r['id']}: {r['decision']['status']}"
        if m:
            line += (f" | {m['name']} champion {m['champion']['value']:.3f} challenger {m['challenger']['value']:.3f} "
                     f"delta {m['delta']['value']:+.3f} {m['delta']['ci95']}")
        print(line + f"\n    {r['decision']['reason']}")


def champion(args) -> None:
    cfg, history = ledger.champion(ledger.ledger_dir(args.provider or "anthropic"))
    if args.json:
        print(json.dumps({"ref": cfg.ref, "config": cfg.to_json(), "history": history}, indent=1))
        return
    print(f"champion {cfg.ref} ({len(history)} promotions)")
    for p, v in cfg.values.items():
        mark = "*" if v != knobs.DEFAULT.values[p] else " "
        print(f" {mark} {p:<40} {v!r}")


def list_knobs(args) -> None:
    for k in knobs.REGISTRY:
        print(f"{k.path:<40} {k.default!r:<32} {json.dumps(k.domain())}  reader: {k.reader or '(none yet)'}")


def revert(args) -> None:
    directory = ledger.ledger_dir(args.provider or "anthropic")
    rec = ledger.revert_record(directory, args.experiment, args.reason)
    ledger.write(directory, [rec])
    print(f"wrote {rec['id']}; champion is now {rec['challenger']['ref']}")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("iterate", help="self-iteration: test knob changes against the champion and keep what helps")
    isub = p.add_subparsers(dest="iterate_cmd", required=True)

    r = isub.add_parser("run", help="run a named batch of experiments and write the ledger")
    r.add_argument("--plan", default="first")
    r.add_argument("--provider", help="mock (default) or anthropic")
    r.add_argument("--max-calls", type=int, default=150, help="hard cap on LLM calls for the whole batch")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--threads", help="thread fixture for action_lineup (default action/fixtures/x_threads.json)")
    r.add_argument("--limit", type=int, help="use only the first N threads")
    r.add_argument("--per-tier", type=int, default=4, help="qualify_auc: posts per tier per account")
    r.add_argument("--note")
    r.set_defaults(func=run)

    c = isub.add_parser("champion", help="print the current champion config")
    c.add_argument("--provider", help="mock reads out/iterate/; anything else eval/experiments/")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=champion)

    k = isub.add_parser("knobs", help="list every registered knob, its default, domain and reader")
    k.set_defaults(func=list_knobs)

    v = isub.add_parser("revert", help="undo a promoted experiment with a new, human-decided record")
    v.add_argument("experiment")
    v.add_argument("--reason", required=True)
    v.add_argument("--provider")
    v.set_defaults(func=revert)
