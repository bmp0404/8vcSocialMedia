"""Backtest predictors against real engagement in the dev-tool dataset.

Every predictor scores the same holdout: posts dated on or after `holdout_from`, minus thread fragments
and replies. Posts before the cutoff form the train split, the only place a reference-based judge may
draw examples from. The headline numbers compare posts within one account, because the dataset has no
follower counts and raw engagement mostly measures how big the account is.
"""

import json
import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from harness import config
from harness.evals import metrics as m
from harness.evals.dataset import CATEGORIES, Post, is_scorable
from harness.evals.predictors import Prediction, Predictor

REPORT_DIR = config.ROOT / "eval" / "reports"


@dataclass
class Split:
    holdout_from: date
    train: list[Post]
    holdout: list[Post]
    dropped_unscorable: int


def split(posts: list[Post], holdout_from: date) -> Split:
    train = [p for p in posts if p.date < holdout_from]
    later = [p for p in posts if p.date >= holdout_from]
    holdout = [p for p in later if is_scorable(p)]
    return Split(holdout_from, train, holdout, len(later) - len(holdout))


def sample(holdout: list[Post], per_account: int, max_accounts: int | None, seed: int) -> list[Post]:
    """Up to `per_account` posts from each account, rotating through its categories.

    Rotating means an account with 2 Super Viral and 40 Deadweight posts contributes both kinds,
    which is where within-account comparisons get their signal. Accounts with one scorable post
    are kept for the cross-account numbers.
    """
    rng = random.Random(seed)
    by_account: dict[str, dict[str, list[Post]]] = defaultdict(lambda: defaultdict(list))
    for p in holdout:
        by_account[p.account][p.category].append(p)
    accounts = sorted(by_account)
    rng.shuffle(accounts)
    if max_accounts:  # a capped run is for within-account numbers, so skip accounts with one post
        accounts = [a for a in accounts if sum(map(len, by_account[a].values())) >= 2][:max_accounts]
    out = []
    for a in sorted(accounts):
        queues = [rng.sample(by_account[a][c], len(by_account[a][c])) for c in CATEGORIES if by_account[a][c]]
        picked = []
        while len(picked) < per_account and any(queues):
            for q in queues:
                if q and len(picked) < per_account:
                    picked.append(q.pop())
        out += picked
    return out


def _stat(value, ci, **extra) -> dict:
    return {"value": value, "ci95": list(ci) if ci else None, **extra}


def evaluate(posts: list[Post], preds: dict[str, Prediction], probabilistic: bool,
             n_boot: int = 1000, seed: int = 0) -> dict:
    missing = [p.id for p in posts if p.id not in preds]
    if missing:
        raise ValueError(f"predictor returned no score for {len(missing)} posts, e.g. {missing[:3]}")
    s = [preds[p.id].score for p in posts]
    t = [p.target for p in posts]
    accounts = [p.account for p in posts]
    log_eng = [math.log1p(p.engagement) for p in posts]
    super_viral = [p.category == "Super Viral" for p in posts]
    beat = [p.target > 0 for p in posts]

    def boot(fn):
        return m.cluster_bootstrap(fn, accounts, n_boot, seed)

    def sub(xs, idx):
        return [xs[i] for i in idx]

    pa, n_pairs = m.pairwise_accuracy(s, t, accounts)
    top, n_groups = m.top_pick_beats_median(s, t, accounts)
    report = {
        "n_posts": len(posts),
        "n_accounts": len(set(accounts)),
        "within_account": {
            "pairwise_accuracy": _stat(pa, boot(lambda i, d: m.pairwise_accuracy(sub(s, i), sub(t, i), d)[0]),
                                       n_pairs=n_pairs, chance=0.5),
            "top_pick_beats_median": _stat(top, boot(lambda i, d: m.top_pick_beats_median(sub(s, i), sub(t, i), d)[0]),
                                           n_accounts=n_groups,
                                           # constant scores tie every post, which is the random-order expectation
                                           chance=m.top_pick_beats_median([0.0] * len(t), t, accounts)[0]),
            "spearman_vs_target": _stat(m.spearman(s, t), boot(lambda i, d: m.spearman(sub(s, i), sub(t, i)))),
        },
        "cross_account": {
            "spearman_vs_log_engagement": _stat(m.spearman(s, log_eng),
                                                boot(lambda i, d: m.spearman(sub(s, i), sub(log_eng, i)))),
            "super_viral_auc": _stat(m.pairwise_accuracy(s, super_viral)[0],
                                     boot(lambda i, d: m.pairwise_accuracy(sub(s, i), sub(super_viral, i))[0]),
                                     n_super_viral=sum(super_viral)),
            "super_viral_base_rate": sum(super_viral) / len(posts) if posts else None,
            "precision_at_k": {
                str(k): _stat(m.precision_at_k(s, super_viral, k),
                              boot(lambda i, d, k=k: m.precision_at_k(sub(s, i), sub(super_viral, i), k)))
                for k in (10, 25) if k <= len(posts)
            },
        },
        "calibration": None,
    }
    if probabilistic:
        base = sum(beat) / len(beat)
        report["calibration"] = {
            "event": "engagement strictly above the account's median",
            "base_rate": base,
            "brier": m.brier(s, beat),
            "brier_constant_base_rate": m.brier([base] * len(beat), beat),
            "ece": m.ece(s, beat),
            "reliability": m.reliability(s, beat),
        }
    return report


BASELINES = ("random", "media", "length")


def versus_baselines(posts: list[Post], preds: dict[str, dict[str, Prediction]], n_boot: int, seed: int) -> dict:
    """Does a model predictor know anything that text length and media type don't?

    For each non-baseline predictor: its within-account pair accuracy minus each baseline's, on the
    same pairs, with an interval from resampling accounts; and whether adding it to a length+media
    rank-sum moves that combination's accuracy.
    """
    t = [p.target for p in posts]
    accounts = [p.account for p in posts]

    def scores(name):
        return [preds[name][p.id].score for p in posts]

    def norm_ranks(xs):
        return [r / len(xs) for r in m.ranks(xs)]

    def acc(s, idx, d):
        return m.pairwise_accuracy([s[i] for i in idx], [t[i] for i in idx], d)[0]

    def paired(a, b):
        def diff(idx, d):
            x, y = acc(a, idx, d), acc(b, idx, d)
            return None if x is None or y is None else x - y
        return {"value": diff(range(len(posts)), accounts), "ci95": m.cluster_bootstrap(diff, accounts, n_boot, seed)}

    available = [b for b in ("length", "media") if b in preds]
    combo = None
    if len(available) == 2:
        combo = [x + y for x, y in zip(norm_ranks(scores("length")), norm_ranks(scores("media")))]
    out = {}
    for name in preds:
        if name in BASELINES:
            continue
        s = scores(name)
        row = {f"minus_{b}": paired(s, scores(b)) for b in available}
        if combo:
            with_it = [c + r for c, r in zip(combo, norm_ranks(s))]
            row["length_plus_media"] = acc(combo, range(len(posts)), accounts)
            row["length_plus_media_plus_this"] = acc(with_it, range(len(posts)), accounts)
            row["gain_over_length_plus_media"] = paired(with_it, combo)
        out[name] = row
    return out


def retest(predictor: Predictor, posts: list[Post], first: dict[str, Prediction]) -> dict:
    """Score the same posts again. Spearman between runs bounds how well any metric can reproduce."""
    again = predictor.predict(posts)
    a = [first[p.id].score for p in posts]
    b = [again[p.id].score for p in posts]
    return {"n_posts": len(posts), "spearman_between_runs": m.spearman(a, b),
            "mean_abs_diff": sum(abs(x - y) for x, y in zip(a, b)) / len(posts) if posts else None}


def run(predictors: list[Predictor], data: Split, picked: list[Post], *, seed: int, n_boot: int,
        retest_posts: int, meta: dict) -> dict:
    flags_known = all(p.boost_suspect is not None for p in picked)
    results, all_preds = {}, {}
    for pred in predictors:
        print(f"[backtest] {pred.name}: scoring {len(picked)} posts")
        started = datetime.now()
        preds = pred.predict(picked)
        entry = {
            "probabilistic": pred.probabilistic,
            "seconds": round((datetime.now() - started).total_seconds(), 1),
            "all": evaluate(picked, preds, pred.probabilistic, n_boot, seed),
            "without_boost_suspect": (
                evaluate([p for p in picked if not p.boost_suspect], preds, pred.probabilistic, n_boot, seed)
                if flags_known else "boost_suspect flags not loaded; see data/context/devtools_enriched.csv"
            ),
        }
        all_preds[pred.name] = preds
        if hasattr(pred, "model_info"):
            entry["model"] = pred.model_info
        if hasattr(pred, "calls"):
            entry["llm_calls"] = pred.calls
            entry["approx_input_tokens"] = pred.prompt_chars // 4
        if retest_posts and pred.probabilistic:
            entry["retest"] = retest(pred, picked[:retest_posts], preds)
        entry["predictions"] = {pid: asdict(p) for pid, p in preds.items()}
        results[pred.name] = entry
    return {
        **meta,
        "holdout_from": data.holdout_from.isoformat(),
        "n_train": len(data.train),
        "n_holdout_scorable": len(data.holdout),
        "n_holdout_dropped_unscorable": data.dropped_unscorable,
        "n_sampled": len(picked),
        "sampled_categories": {c: sum(p.category == c for p in picked) for c in CATEGORIES},
        "target_source": "enriched" if flags_known else "local author median",
        "seed": seed,
        "results": results,
        "versus_baselines": versus_baselines(picked, all_preds, n_boot, seed),
    }


def _fmt(stat: dict | None) -> str:
    if not stat or stat.get("value") is None:
        return "n/a"
    ci = stat.get("ci95")
    return f"{stat['value']:.3f} [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else f"{stat['value']:.3f}"


def markdown(report: dict) -> str:
    lines = [
        f"# Backtest {report['at']}",
        "",
        f"Provider `{report['provider']}`, model `{report['model']}`, panel `{report['panel_path']}`. "
        f"Holdout: posts from {report['holdout_from']} on; {report['n_sampled']} sampled of "
        f"{report['n_holdout_scorable']} scorable ({report['n_holdout_dropped_unscorable']} fragments and replies "
        f"dropped). Sampled categories: {report['sampled_categories']}. Target: {report['target_source']}. "
        "Brackets are 95% intervals from resampling accounts.",
        "",
        "| Predictor | Within-account pair accuracy | Top pick beats account median | Within-account Spearman "
        "| Cross-account Spearman (log engagement) | Super Viral AUC | P@10 Super Viral | Brier (constant) | ECE |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, r in report["results"].items():
        a = r["all"]
        w, c, cal = a["within_account"], a["cross_account"], a["calibration"]
        brier = f"{cal['brier']:.3f} ({cal['brier_constant_base_rate']:.3f})" if cal else "n/a"
        ece = f"{cal['ece']:.3f}" if cal else "n/a"
        lines.append(
            f"| {name} | {_fmt(w['pairwise_accuracy'])} | {_fmt(w['top_pick_beats_median'])} "
            f"| {_fmt(w['spearman_vs_target'])} | {_fmt(c['spearman_vs_log_engagement'])} "
            f"| {_fmt(c['super_viral_auc'])} | {_fmt(c['precision_at_k'].get('10'))} | {brier} | {ece} |"
        )
    first = next(iter(report["results"].values()))["all"]
    lines += [
        "",
        f"Pairs compared within accounts: {first['within_account']['pairwise_accuracy']['n_pairs']}. "
        f"Accounts with 2+ sampled posts: {first['within_account']['top_pick_beats_median']['n_accounts']}. "
        f"Super Viral base rate in the sample: {first['cross_account']['super_viral_base_rate']:.3f}.",
    ]
    for name, row in report.get("versus_baselines", {}).items():
        parts = [f"minus {k[6:]}: {_fmt(v)}" for k, v in row.items() if k.startswith("minus_")]
        if "gain_over_length_plus_media" in row:
            parts.append(f"length+media {row['length_plus_media']:.3f}, adding `{name}` "
                         f"{row['length_plus_media_plus_this']:.3f} (gain {_fmt(row['gain_over_length_plus_media'])})")
        lines.append(f"\n`{name}` within-account pair accuracy versus baselines on the same pairs: {'; '.join(parts)}.")
    for name, r in report["results"].items():
        if "retest" in r:
            rt = r["retest"]
            sp = "n/a" if rt["spearman_between_runs"] is None else f"{rt['spearman_between_runs']:.3f}"
            lines.append(f"\nRetest of `{name}` on {rt['n_posts']} posts: Spearman between runs {sp}, "
                         f"mean absolute change {rt['mean_abs_diff']:.3f}.")
        cal = r["all"]["calibration"]
        if cal:
            lines += ["", f"Calibration of `{name}` ({cal['event']}, base rate {cal['base_rate']:.3f}):", "",
                      "| Score bin | Posts | Mean score | Observed rate |", "| --- | --- | --- | --- |"]
            for b in cal["reliability"]:
                if b["n"]:
                    lines.append(f"| {b['lo']:.1f}-{b['hi']:.1f} | {b['n']} | {b['mean_pred']:.3f} | {b['observed']:.3f} |")
    return "\n".join(lines) + "\n"


def write(report: dict, out_dir: Path = REPORT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"backtest-{report['at'].replace(':', '').replace('-', '')}-{report['provider']}"
    j, md = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    j.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    md.write_text(markdown(report), encoding="utf-8")
    return j, md
