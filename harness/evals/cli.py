"""`python -m harness eval <backtest|leakage|fault>`. Reports go to eval/reports/."""

import json
from datetime import date, datetime
from pathlib import Path

from harness import config
from harness.evals import backtest, dataset, fault, leakage
from harness.evals.predictors import build
from harness.llm import get_llm

DEVTOOLS_PANEL = config.ROOT / "eval" / "panels" / "devtools.json"


def _model(provider: str | None) -> str:
    from harness.llm import DEFAULT_MODELS

    p = (provider or config.PROVIDER).lower()
    return "mock" if p == "mock" else config.MODEL or DEFAULT_MODELS.get(p, "")


def cmd_backtest(a) -> None:
    llm = get_llm(a.provider)
    posts = dataset.load()
    data = backtest.split(posts, date.fromisoformat(a.holdout_from))
    picked = backtest.sample(data.holdout, a.per_account, a.max_accounts, a.seed)
    predictors = build(a.predictors.split(","), llm, Path(a.panel), data.holdout_from, a.seed)
    meta = {"at": datetime.now().isoformat(timespec="seconds"), "provider": (a.provider or config.PROVIDER).lower(),
            "model": _model(a.provider), "panel_path": a.panel}
    report = backtest.run(predictors, data, picked, seed=a.seed, n_boot=a.boot, retest_posts=a.retest, meta=meta)
    j, md = backtest.write(report)
    print(backtest.markdown(report))
    print(f"[backtest] report -> {md} and {j}")


def cmd_leakage(a) -> None:
    llm = get_llm(a.provider)
    posts = [p for p in dataset.load() if dataset.is_scorable(p)]
    result = leakage.probe(llm, leakage.pick(posts, a.n, a.seed), seed=a.seed)
    result.update({"at": datetime.now().isoformat(timespec="seconds"),
                   "provider": (a.provider or config.PROVIDER).lower(), "model": _model(a.provider)})
    backtest.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = backtest.REPORT_DIR / f"leakage-{result['at'].replace(':', '').replace('-', '')}-{result['provider']}.json"
    out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    ctl = result["mean_control_overlap"]
    print(f"[leakage] {result['n_posts']} posts ({result['n_super_viral']} Super Viral): "
          f"mean overlap {result['mean_overlap'] or 0:.3f} vs control {ctl if ctl is None else round(ctl, 3)}; "
          f"verbatim first {leakage.VERBATIM_WORDS} words: {result['n_verbatim']}; "
          f"overlap > 0.5: {result['n_overlap_over_half']}; claimed recognized: {result['n_recognized']}")
    print(f"[leakage] report -> {out}")


def cmd_fault(a) -> None:
    rows = json.loads(Path(a.file).read_text(encoding="utf-8"))
    verdicts = [fault.attribute(fault.FaultEvidence(**r)) for r in rows]
    for v in verdicts:
        stage = v.stage_to_fix or "-"
        print(f"{v.action_id:<12} {v.verdict:<22} fix: {stage:<8} {'; '.join(v.reasons)}"
              + (f"  missing: {', '.join(v.missing)}" if v.missing else ""))
    if a.out:
        Path(a.out).write_text(json.dumps([v.to_dict() for v in verdicts], indent=1), encoding="utf-8")
        print(f"[fault] verdicts -> {a.out}")


def register(sub) -> None:
    p = sub.add_parser("eval", help="measure the evaluators: backtest, leakage probe, fault attribution")
    ev = p.add_subparsers(dest="eval_cmd", required=True)

    b = ev.add_parser("backtest", help="score real dev-tool posts blind and compare with their engagement")
    b.add_argument("--provider", help="override HARNESS_PROVIDER (anthropic|xai|openai|mock)")
    b.add_argument("--predictors", default="random,media,length,panel",
                   help="comma-separated: random, media, length, panel, post_judge, post_judge_rank")
    b.add_argument("--panel", default=str(DEVTOOLS_PANEL), help="audience panel JSON for the panel predictor")
    b.add_argument("--holdout-from", default="2026-10-06", help="posts on or after this date form the holdout")
    b.add_argument("--per-account", type=int, default=8, help="max holdout posts per account")
    b.add_argument("--max-accounts", type=int, default=None, help="cap accounts for a cheap first run")
    b.add_argument("--retest", type=int, default=0, help="rescore this many posts to measure run-to-run noise")
    b.add_argument("--boot", type=int, default=1000, help="bootstrap resamples for the intervals")
    b.add_argument("--seed", type=int, default=0)
    b.set_defaults(func=cmd_backtest)

    lk = ev.add_parser("leakage", help="probe whether the model can recite dataset posts from memory")
    lk.add_argument("--provider")
    lk.add_argument("--n", type=int, default=40, help="posts to probe; Super Viral ones go first")
    lk.add_argument("--seed", type=int, default=0)
    lk.set_defaults(func=cmd_leakage)

    f = ev.add_parser("fault", help="attribute underperforming actions to observe, action or eval")
    f.add_argument("file", help="JSON list of FaultEvidence objects (see eval/fixtures/fault_evidence.json)")
    f.add_argument("--out", help="write verdicts as JSON here")
    f.set_defaults(func=cmd_fault)
