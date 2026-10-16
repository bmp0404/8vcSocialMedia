"""`python -m harness iterate ...`: run experiment batches, inspect knobs and the champion, revert."""

import argparse
import json
from pathlib import Path

from harness.iterate import knobs, ledger


def _fitnesses(args, provider: str) -> dict:
    from harness.iterate.fitness.action import DEFAULT_THREADS, ActionLineup

    paths = [Path(t) for t in getattr(args, "threads", None) or []] or [DEFAULT_THREADS]
    out = {}
    missing = [p for p in paths if not p.exists()]
    if missing:  # the default fixture lands with sam/action
        print(f"[iterate] action_lineup unavailable: {', '.join(map(str, missing))} not found; pass --threads")
    else:
        out["action_lineup"] = ActionLineup(paths, limit=getattr(args, "limit", None))
    try:
        from harness.iterate.fitness.qualify import QualifyAUC
    except ImportError as e:  # harness.evals lands with sam/eval
        print(f"[iterate] qualify_auc unavailable: {e}")
    else:
        from harness.iterate.fitness.qualify import CACHE_REPORT

        # Never mix the eval worker's real scores into a mock run.
        out["qualify_auc"] = QualifyAUC(per_tier=getattr(args, "per_tier", 4),
                                        cache_report=None if provider == "mock" else CACHE_REPORT)
    return out


def run(args) -> None:
    from harness.iterate.plans import PLANS
    from harness.iterate.runner import run_batch

    if args.plan not in PLANS:
        raise SystemExit(f"unknown plan {args.plan!r}; choose from {sorted(PLANS)}")
    provider = (args.provider or "mock").lower()
    if provider not in ("mock", "anthropic"):
        raise SystemExit("iterate runs on --provider mock or anthropic (the gateway serves every model it tests)")
    recs = run_batch(PLANS[args.plan], _fitnesses(args, provider), provider=provider, max_calls=args.max_calls, seed=args.seed,
                     directory=Path(args.ledger_dir) if args.ledger_dir else None, run_note=args.note or f"plan {args.plan}")
    for r in recs:
        m = next((x for x in r["eval"]["metrics"] if x.get("primary")), None)
        line = f"{r['id']}: {r['decision']['status']}"
        if m:
            line += (f" | {m['name']} champion {m['champion']['value']:.3f} challenger {m['challenger']['value']:.3f} "
                     f"delta {m['delta']['value']:+.3f} {m['delta']['ci95']}")
        print(line + f"\n    {r['decision']['reason']}")


def propose(args) -> None:
    """Ask the rule or LLM proposer for the next batch, record what it proposed and why anything was
    rejected, and optionally run the accepted proposals."""
    from harness.iterate import propose as prop
    from harness.iterate.plans import PLANS
    from harness.iterate.runner import run_batch

    provider = (args.provider or "mock").lower()
    directory = Path(args.ledger_dir) if args.ledger_dir else ledger.ledger_dir(provider)
    fits = _fitnesses(args, provider)
    fit_knobs = {name: f.knobs for name, f in fits.items()}
    datasets = {name: f.instrument().get("dataset_sha256") for name, f in fits.items()}
    champ, _ = ledger.champion(directory)
    recs, cfgs = ledger.records(directory), ledger.configs(directory)
    if args.llm:
        from harness.iterate.llm import Meter, MeteredLLM, MockMeteredLLM

        meter = Meter(3)
        llm = (MockMeteredLLM(meter, "proposer", lambda s, p: {}) if provider == "mock"
               else MeteredLLM(args.model, meter, "proposer", max_tokens=3000))
        batch = prop.LLMProposer(llm, prop.findings_from_plans(), limit=args.k).propose(champ, recs, fit_knobs, cfgs,
                                                                                       datasets)
        print(f"[iterate] proposer used {meter.calls} call(s)")
    else:
        priors = [p for ps in PLANS.values() for p in ps]
        batch = prop.RuleProposer(priors, limit=args.k).propose(champ, recs, fit_knobs, cfgs, datasets)
    out = prop.to_json(batch)
    path = directory / "proposals" / f"{ledger.now()[:19].replace('-', '').replace(':', '')}-{'llm' if args.llm else 'rule'}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"champion": champ.ref, **out}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for p in out["accepted"]:
        print(f"accept  {p['fitness']}: {p['changes']}  ({p['proposer'].get('source')})\n        {p['hypothesis']}")
    for r in out["rejected"]:
        print(f"reject  {r['proposal'].get('changes')}: {r['reason']}")
    print(f"[iterate] wrote {path}")
    if args.run and batch.accepted:
        run_batch(batch.accepted, fits, provider=provider, max_calls=args.max_calls, seed=args.seed,
                  directory=directory, run_note=f"proposals from {path.name}")


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
    r.add_argument("--threads", action="append",
                   help="thread fixture for action_lineup; repeat to combine (default action/fixtures/x_threads.json)")
    r.add_argument("--ledger-dir", help="write records here instead of eval/experiments/ (or out/iterate/ on mock)")
    r.add_argument("--limit", type=int, help="use only the first N threads")
    r.add_argument("--per-tier", type=int, default=4, help="qualify_auc: posts per tier per account")
    r.add_argument("--note")
    r.set_defaults(func=run)

    pr = isub.add_parser("propose", help="propose the next experiments (rule-based, or --llm), optionally run them")
    pr.add_argument("--provider", help="mock (default) or anthropic")
    pr.add_argument("--llm", action="store_true", help="ask a model instead of the rule-based search")
    pr.add_argument("--model", default="claude-opus-5-5", help="model for --llm")
    pr.add_argument("--k", type=int, default=3, help="most comparisons to propose (one BH family)")
    pr.add_argument("--run", action="store_true", help="run the accepted proposals")
    pr.add_argument("--max-calls", type=int, default=150)
    pr.add_argument("--seed", type=int, default=0)
    pr.add_argument("--threads", action="append")
    pr.add_argument("--limit", type=int)
    pr.add_argument("--per-tier", type=int, default=4)
    pr.add_argument("--ledger-dir")
    pr.set_defaults(func=propose)

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
