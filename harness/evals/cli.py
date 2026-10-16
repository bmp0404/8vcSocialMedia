"""`python -m harness eval <backtest|rescore|leakage|fault>`. Reports go to eval/reports/."""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from harness import config
from harness.evals import backtest, dataset, fault, leakage
from harness.evals.predictors import Prediction, build
from harness.evals.settings import BacktestConfig, FaultConfig
from harness.llm import DEFAULT_MODELS, get_llm

DEVTOOLS_PANEL = config.ROOT / "eval" / "panels" / "devtools.json"


def _provider(a) -> str:
    return (a.provider or config.PROVIDER).lower()


def _models(a) -> list[str]:
    if _provider(a) == "mock":
        return ["mock"]
    if a.models:
        return a.models.split(",")
    model = config.MODEL or DEFAULT_MODELS.get(_provider(a), "")
    if not model:
        raise SystemExit("pass --models or set HARNESS_MODEL")
    return [model]


def cmd_backtest(a) -> None:
    cfg = BacktestConfig(per_tier=a.per_tier, min_age_hours=a.min_age_hours, n_boot=a.boot, seed=a.seed,
                         models=tuple(_models(a)), batch=a.batch, memory=a.memory)
    posts = dataset.load()
    pool, dropped = backtest.eligible(posts, cfg)
    picked = backtest.sample(pool, cfg.per_tier, cfg.seed)
    if a.max_accounts:
        keep = sorted({p.account for p in picked})[: a.max_accounts]
        picked = [p for p in picked if p.account in keep]
    models = _models(a)
    predictors = build(a.predictors.split(","), _provider(a), models, Path(a.panel), pool, a.seed, a.memory, a.batch)
    meta = {"at": datetime.now().isoformat(timespec="seconds"), "provider": _provider(a), "models": models,
            "panel_path": a.panel, "memory": a.memory, "config": asdict(cfg), "n_eligible": len(pool),
            "dropped": dropped}
    out = backtest.default_path(meta)
    report = backtest.run(predictors, picked, {p.id: p for p in posts}, seed=a.seed, n_boot=a.boot,
                          retest_posts=a.retest, meta=meta, out=out)
    print(backtest.markdown(report))
    print(f"[backtest] report -> {out.with_suffix('.md')} and {out}")


def cmd_rescore(a) -> None:
    """Recompute every metric from a saved report's predictions; no LLM calls."""
    old = json.loads(Path(a.report).read_text(encoding="utf-8"))
    if "sample_ids" not in old or "protocol" not in old:
        raise SystemExit(f"{a.report} predates protocol v2; rerun the backtest instead")
    posts = dataset.load()
    by_id = {p.id: p for p in posts}
    picked = [by_id[i] for i in old["sample_ids"]]
    preds, extras = {}, {}
    sources = [old] + [json.loads(Path(f).read_text(encoding="utf-8")) for f in a.merge or []]
    for src in sources:
        if src["sample_ids"] != old["sample_ids"]:
            raise SystemExit("--merge reports must come from the same sample (same seed, per-tier and filters)")
        for name, r in src["results"].items():
            if r.get("derived"):
                continue  # recomputed from the others
            preds[name] = {pid: Prediction(**v) for pid, v in r["predictions"].items()}
            extras[name] = {k: v for k, v in r.items()
                            if k not in {"all", "without_boost_suspect", "category_balanced", "predictions"}}
    meta = {k: v for k, v in old.items() if k not in
            {"protocol", "n_sampled", "n_scored_by_all", "sample_ids", "sampled_categories", "target_source", "seed",
             "results", "comparisons"}}
    meta["rescored_at"] = datetime.now().isoformat(timespec="seconds")
    report = backtest.summarize(picked, preds, extras, by_id, seed=old["seed"], n_boot=a.boot, meta=meta)
    backtest.write(report, Path(a.report))
    print(backtest.markdown(report))
    print(f"[rescore] rewrote {a.report} and its .md")


def cmd_leakage(a) -> None:
    llm = get_llm(a.provider)
    posts = [p for p in dataset.load() if dataset.is_scorable(p)]
    result = leakage.probe(llm, leakage.pick(posts, a.n, a.seed))
    result.update({"at": datetime.now().isoformat(timespec="seconds"), "provider": _provider(a),
                   "model": "mock" if _provider(a) == "mock" else config.MODEL or DEFAULT_MODELS.get(_provider(a))})
    backtest.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = backtest.REPORT_DIR / f"leakage-{result['at'].replace(':', '').replace('-', '')}-{result['provider']}.json"
    out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(f"[leakage] {result['n_posts']} posts ({result['n_super_viral']} Super Viral): "
          f"{result['n_verbatim']} continued verbatim for {leakage.VERBATIM_WORDS}+ words, "
          f"{result['n_recognized']} claimed recognized. Posts date from 2026-09-15 to 2026-10-16.")
    print(f"[leakage] report -> {out}")


def cmd_fault(a) -> None:
    rows = json.loads(Path(a.file).read_text(encoding="utf-8"))
    verdicts = [fault.attribute(fault.FaultEvidence(**r)) for r in rows]
    for v in verdicts:
        stage = v.stage_to_fix or "-"
        print(f"{v.action_id:<12} {v.verdict:<22} fix: {stage:<8} {'; '.join(v.reasons)}"
              + (f"  missing: {', '.join(v.missing)}" if v.missing else ""))
    summary = fault.aggregate(verdicts)
    print(f"[fault] stages with {FaultConfig.min_repeats}+ matching verdicts: {summary['fix'] or 'none'}; "
          f"counts {summary['counts']}")
    if a.out:
        Path(a.out).write_text(json.dumps({"verdicts": [v.to_dict() for v in verdicts], "summary": summary},
                                          indent=1), encoding="utf-8")
        print(f"[fault] verdicts -> {a.out}")


def register(sub) -> None:
    p = sub.add_parser("eval", help="measure the evaluators: backtest, leakage probe, fault attribution")
    ev = p.add_subparsers(dest="eval_cmd", required=True)

    b = ev.add_parser("backtest", help="score real dev-tool posts blind and compare with their engagement")
    b.add_argument("--provider", help="override HARNESS_PROVIDER (anthropic|xai|openai|mock)")
    b.add_argument("--predictors", default="random,account,media,length,tfidf,panel,reader",
                   help="comma-separated: random, account, media, length, tfidf, panel, reader, post_judge, "
                        "post_judge_rank")
    b.add_argument("--models", help="comma-separated models for panel and reader, e.g. "
                                    "claude-sonnet-5-5,gpt-6.1-sol (default: HARNESS_MODEL)")
    b.add_argument("--memory", default="on", help="judge references: on, or shuffled (same posts, permuted labels)")
    b.add_argument("--panel", default=str(DEVTOOLS_PANEL), help="audience panel JSON for the panel predictor")
    b.add_argument("--per-tier", type=int, default=BacktestConfig.per_tier,
                   help="max viral and max Deadweight posts per account")
    b.add_argument("--min-age-hours", type=float, default=BacktestConfig.min_age_hours)
    b.add_argument("--max-accounts", type=int, default=None, help="cap accounts for a cheap smoke run")
    b.add_argument("--batch", type=int, default=1, help="posts per panel call; above 1, two orders are averaged")
    b.add_argument("--retest", type=int, default=0, help="rescore this many posts to measure run-to-run noise")
    b.add_argument("--boot", type=int, default=2000, help="bootstrap resamples for the intervals")
    b.add_argument("--seed", type=int, default=0)
    b.set_defaults(func=cmd_backtest)

    r = ev.add_parser("rescore", help="recompute a backtest report's metrics from its saved predictions")
    r.add_argument("report", help="path to a backtest-*.json report")
    r.add_argument("--boot", type=int, default=2000)
    r.add_argument("--merge", action="append", help="add the predictors from another report on the same sample")
    r.set_defaults(func=cmd_rescore)

    lk = ev.add_parser("leakage", help="probe whether the model can recite dataset posts from memory")
    lk.add_argument("--provider")
    lk.add_argument("--n", type=int, default=40, help="posts to probe; Super Viral ones go first")
    lk.add_argument("--seed", type=int, default=0)
    lk.set_defaults(func=cmd_leakage)

    f = ev.add_parser("fault", help="attribute underperforming actions to observe, action or eval")
    f.add_argument("file", help="JSON list of FaultEvidence objects (see eval/fixtures/fault_evidence.json)")
    f.add_argument("--out", help="write verdicts as JSON here")
    f.set_defaults(func=cmd_fault)
