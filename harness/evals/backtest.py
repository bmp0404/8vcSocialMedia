"""Backtest predictors against real engagement in the dev-tool dataset.

The question is the one qualify answers in production: among one brand's posts, which will do well?
So the headline compares a viral post (100+ engagement) with a Deadweight post (10 or less) from the
same account, and asks how often the predictor scores the viral one higher (a within-account AUC).

Protocol (8vc-context/research/eval.md, section 13):
- Eligible posts: scorable text, not a thread continuation, at least 48 hours old (see dataset.py).
- Sample: every account with both tiers among eligible posts, up to `per_tier` posts of each tier.
- Nothing here is fitted, so there is no date holdout. The fitted baselines (TF-IDF, account rate)
  cross-fit by account, and the judge only sees references dated before each candidate.
- Model predictors score each post alone (see predictors.py on batch order).
- Intervals resample accounts. Predictor comparisons use a paired account bootstrap.

Every number is an upper bound for production. The CSV holds only the two tails, so each pair is a
10x-or-wider gap, while qualify picks among drafts that differ far less.
"""

import json
import math
import random
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from harness import config
from harness.evals import metrics as m
from harness.evals.dataset import (
    CATEGORIES,
    Post,
    is_eligible,
    is_launch,
    is_scorable,
    is_thread_continuation,
    is_young,
)
from harness.evals.leakage import words
from harness.evals.predictors import Prediction, Predictor
from harness.evals.settings import BacktestConfig

REPORT_DIR = config.ROOT / "eval" / "reports"
BASELINES = ("random", "account", "media", "length", "tfidf")
COMPARE_AGAINST = ("media", "length", "tfidf")  # plus every reader@<model>; see comparisons()
COMPARABLE_SEGMENTS = ("Smaller/closest", "Mid-size")


def eligible(posts: list[Post], cfg: BacktestConfig) -> tuple[list[Post], dict]:
    kept = [p for p in posts if is_eligible(p, cfg)]
    if all(p.context_eligible is not None for p in posts):
        return kept, {"unscorable_text_or_reply": sum(not is_scorable(p, cfg) for p in posts),
                      "context_label_ineligible": sum(is_scorable(p, cfg) and not p.context_eligible for p in posts)}
    dropped = {
        "unscorable_text_or_reply": sum(not is_scorable(p, cfg) for p in posts),
        "thread_continuation": sum(is_scorable(p, cfg) and is_thread_continuation(p, cfg) for p in posts),
        f"younger_than_{cfg.min_age_hours:g}h": sum(
            is_scorable(p, cfg) and not is_thread_continuation(p, cfg) and is_young(p, cfg) for p in posts),
    }
    return kept, dropped


def sample(pool: list[Post], per_tier: int, seed: int) -> list[Post]:
    """Up to `per_tier` viral and `per_tier` Deadweight posts from every account that has both,
    returned in a seeded shuffle so no later step can lean on input order."""
    rng = random.Random(seed)
    by_account: dict[str, dict[bool, list[Post]]] = defaultdict(lambda: {True: [], False: []})
    for p in pool:
        by_account[p.account][p.viral].append(p)
    out = []
    for account in sorted(by_account):
        tiers = by_account[account]
        if tiers[True] and tiers[False]:
            for tier in (True, False):
                out += rng.sample(tiers[tier], min(per_tier, len(tiers[tier])))
    rng.shuffle(out)
    return out


def _stat(value, ci, **extra) -> dict:
    return {"value": value, "ci95": list(ci) if ci else None, **extra}


def evaluate(posts: list[Post], preds: dict[str, Prediction], relative: bool = False,
             n_boot: int = 1000, seed: int = 0) -> dict:
    missing = [p.id for p in posts if p.id not in preds]
    if missing:
        raise ValueError(f"predictor returned no score for {len(missing)} posts, e.g. {missing[:3]}")
    s = [preds[p.id].score for p in posts]
    viral = [p.viral for p in posts]
    log_eng = [math.log1p(p.engagement) for p in posts]
    accounts = [p.account for p in posts]
    # A relative predictor's scores only compare posts it ranked in the same call.
    calls = [preds[p.id].detail.get("call") if relative else None for p in posts]
    groups = list(zip(accounts, calls))

    def auc_within(key: list | None = None, keep=lambda i: True, target=viral):
        """AUC inside account (and call) groups, split further by `key`, on the posts `keep` selects."""
        def pairs_in(idx, group_of):
            sel = [j for j, i in enumerate(idx) if keep(i)]
            return m.pairwise_accuracy([s[idx[j]] for j in sel], [target[idx[j]] for j in sel],
                                       [(group_of(j), calls[idx[j]], key[idx[j]] if key else None) for j in sel])

        everything = list(range(len(posts)))
        value, n_pairs = pairs_in(everything, lambda j: accounts[j])
        ci = m.cluster_bootstrap(lambda idx, draw_ids: pairs_in(idx, lambda j: draw_ids[j])[0], accounts, n_boot, seed)
        return _stat(value, ci, n_pairs=n_pairs)

    media = [p.media != "none" for p in posts]
    launch = [is_launch(p) for p in posts]
    top, n_groups = m.top_pick_beats_median(s, [float(v) for v in viral], groups)
    return {
        "n_posts": len(posts),
        "n_accounts": len({p.account for p in posts}),
        "headline_viral_vs_deadweight_auc": auc_within(),
        "same_media_status": auc_within(key=media),
        "same_launch_type": auc_within(key=launch),
        "viral_vs_viral_by_engagement": auc_within(keep=lambda i: viral[i], target=log_eng),
        "deadweight_vs_deadweight_noise_check": auc_within(keep=lambda i: not viral[i], target=log_eng),
        "top_pick_is_viral": _stat(top, None, n_groups=n_groups,
                                   chance=m.top_pick_beats_median([0.0] * len(s), [float(v) for v in viral], groups)[0]),
        "confounded_by_account_size": {
            "spearman_vs_log_engagement": m.spearman(s, log_eng),
            "super_viral_vs_rest_auc": m.pairwise_accuracy(s, [p.category == "Super Viral" for p in posts])[0],
            # Across accounts, among viral posts. The context worker's past-only author floor gets 0.763 here.
            "super_vs_medium_auc": m.pairwise_accuracy(
                [x for x, p in zip(s, posts) if p.viral], [p.category == "Super Viral" for p in posts if p.viral])[0],
        },
    }


def paired_difference(posts: list[Post], a: dict[str, Prediction], b: dict[str, Prediction],
                      n_boot: int, seed: int, relative: bool = False) -> dict:
    """Headline AUC of a minus b on the same pairs, with an interval from resampling accounts once
    per draw for both. Overlapping marginal intervals can hide a real difference; this can't.
    If `a` is relative, both predictors are compared only on pairs `a` ranked in one call."""
    viral = [p.viral for p in posts]
    accounts = [p.account for p in posts]
    calls = [a[p.id].detail.get("call") if relative else None for p in posts]
    sa, sb = [a[p.id].score for p in posts], [b[p.id].score for p in posts]

    def diff(idx, draw_ids):
        groups = [(d, calls[i]) for i, d in zip(idx, draw_ids)]
        x = m.pairwise_accuracy([sa[i] for i in idx], [viral[i] for i in idx], groups)[0]
        y = m.pairwise_accuracy([sb[i] for i in idx], [viral[i] for i in idx], groups)[0]
        return None if x is None or y is None else x - y

    return _stat(diff(list(range(len(posts))), accounts), m.cluster_bootstrap(diff, accounts, n_boot, seed))


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def references_only(posts: list[Post], judge: dict[str, Prediction], lookup: dict[str, Post]) -> dict[str, Prediction]:
    """Score each post by word overlap with the judge's viral references minus its Deadweight ones.

    The judge sees labeled references picked by text similarity, and the CSV repeats templates, so
    it could be doing lookup. It earns its cost only if it beats this nearest-neighbour score."""
    out = {}
    for p in posts:
        if p.id not in judge:
            continue
        mine = set(words(p.clean_text))
        refs = [lookup[r] for r in judge[p.id].detail.get("references", []) if r in lookup]
        hit = [jaccard(mine, set(words(r.clean_text))) for r in refs if r.viral]
        flop = [jaccard(mine, set(words(r.clean_text))) for r in refs if not r.viral]
        if hit and flop:
            out[p.id] = Prediction(sum(hit) / len(hit) - sum(flop) / len(flop))
    return out


def derived(posts: list[Post], all_preds: dict[str, dict[str, Prediction]], lookup: dict[str, Post]) -> dict:
    """Predictors computed from others at no cost: the mean over model families, and the
    references-only baseline for the judge."""
    out = {}
    for base in ("panel", "reader"):
        family = [n for n in all_preds if n.startswith(f"{base}@")]
        if len(family) >= 2:
            ids = set.intersection(*(set(all_preds[n]) for n in family))
            out[f"{base}@mean"] = {i: Prediction(sum(all_preds[n][i].score for n in family) / len(family))
                                   for i in ids}
    if "post_judge" in all_preds:
        out["post_judge_refs_only"] = references_only(posts, all_preds["post_judge"], lookup)
    return out


def comparisons(posts: list[Post], all_preds: dict[str, dict[str, Prediction]], n_boot: int, seed: int,
                relative: set[str] = frozenset()) -> dict:
    """Planned comparisons: each model predictor against media, length, TF-IDF and the single reader."""
    against = [b for b in COMPARE_AGAINST if b in all_preds] + [n for n in all_preds if n.startswith("reader")]
    out = {}
    for name in all_preds:
        if name in BASELINES or name.startswith("reader") or name.endswith("_memory_shuffled"):
            continue
        refs = against + (["post_judge_refs_only"] if name == "post_judge" and "post_judge_refs_only" in all_preds
                          else [])
        refs += [f"{name}_memory_shuffled"] if f"{name}_memory_shuffled" in all_preds else []
        out[name] = {f"minus_{b}": paired_difference(posts, all_preds[name], all_preds[b], n_boot, seed,
                                                     relative=name in relative)
                     for b in refs}
    for name in all_preds:  # the reader itself, against the cheap baselines
        if name.startswith("reader"):
            out[name] = {f"minus_{b}": paired_difference(posts, all_preds[name], all_preds[b], n_boot, seed)
                         for b in COMPARE_AGAINST if b in all_preds}
    return out


def category_balanced(posts: list[Post], preds: dict[str, Prediction], per_category: int = 10,
                      segments: tuple[str, ...] = COMPARABLE_SEGMENTS, draws: int = 500, seed: int = 0) -> dict:
    """The protocol of harness/backtest.py on aarav/self-improving, rerun on our predictions.

    It draws an equal number of posts from each category in the smaller segments and correlates
    scores with log engagement across accounts. Equal draws from the two tails spread engagement far
    apart and reward anything that tracks account size or news size. Repeating the draw shows how
    much one 30-post sample varies.
    """
    rng = random.Random(seed)
    pool = [p for p in posts if p.segment in segments and p.id in preds]
    by_cat = {c: [p for p in pool if p.category == c] for c in CATEGORIES}
    k = min([per_category] + [len(v) for v in by_cat.values()])
    if k < 2:
        return {"error": f"too few posts per category in {segments}: {[len(v) for v in by_cat.values()]}"}
    values = []
    for _ in range(draws):
        drawn = [p for c in CATEGORIES for p in rng.sample(by_cat[c], k)]
        v = m.spearman([preds[p.id].score for p in drawn], [math.log1p(p.engagement) for p in drawn])
        if v is not None:
            values.append(v)
    values.sort()
    return {
        "segments": list(segments), "per_category": k, "draws": len(values),
        "cross_account_spearman_median": values[len(values) // 2] if values else None,
        "cross_account_spearman_range95": [values[int(0.025 * (len(values) - 1))],
                                           values[int(0.975 * (len(values) - 1))]] if values else None,
    }


def retest(predictor: Predictor, posts: list[Post], first: dict[str, Prediction]) -> dict:
    """Score the same posts again with a fresh seed. With one post per call this is sampling noise;
    with batches it also re-permutes the order."""
    again = predictor.predict(posts, seed="retest") if hasattr(predictor, "batch") else predictor.predict(posts)
    both = [p for p in posts if p.id in first and p.id in again]
    a = [first[p.id].score for p in both]
    b = [again[p.id].score for p in both]
    return {"n_posts": len(both), "spearman_between_runs": m.spearman(a, b),
            "mean_abs_diff": sum(abs(x - y) for x, y in zip(a, b)) / len(both) if both else None}


def run(predictors: list[Predictor], picked: list[Post], lookup: dict[str, Post], *, seed: int, n_boot: int,
        retest_posts: int, meta: dict, out: Path | None = None) -> dict:
    """Score the sample with every predictor (the part that spends LLM calls), then summarize.
    With `out`, the report is rewritten after each predictor so a crash keeps finished work."""
    if not all(p.boost_suspect is not None for p in picked):
        print("[backtest] warning: no boost_suspect flags (data/context/devtools_enriched.csv is missing), "
              "so boosted launches stay in every number")
    all_preds, extras = {}, {}
    report: dict = {}
    for pred in predictors:
        print(f"[backtest] {pred.name}: scoring {len(picked)} posts")
        started = datetime.now()
        preds = pred.predict(picked)
        extra = {"relative": getattr(pred, "relative", False),
                 "seconds": round((datetime.now() - started).total_seconds(), 1),
                 "failures": list(getattr(pred, "failures", []))}
        for attr in ("model_info", "calls"):
            if hasattr(pred, attr):
                extra[attr] = getattr(pred, attr)
        if retest_posts and hasattr(pred, "calls"):
            extra["retest"] = retest(pred, picked[:retest_posts], preds)
        all_preds[pred.name], extras[pred.name] = preds, extra
        report = summarize(picked, all_preds, extras, lookup, seed=seed, n_boot=n_boot, meta=meta)
        if out:
            write(report, out)
    return report


def summarize(picked: list[Post], all_preds: dict[str, dict[str, Prediction]], extras: dict[str, dict],
              lookup: dict[str, Post], *, seed: int, n_boot: int, meta: dict) -> dict:
    """All metrics from saved predictions. `eval rescore` calls this without any LLM calls.

    A post any predictor failed on drops out of every predictor's numbers, so they stay comparable."""
    all_preds = {**all_preds, **derived(picked, all_preds, lookup)}
    scored = [p for p in picked if all(p.id in preds for preds in all_preds.values())]
    flags_known = all(p.boost_suspect is not None for p in scored)
    results = {}
    for name, preds in all_preds.items():
        extra = extras.get(name, {"derived": True})
        relative = extra.get("relative", False)
        results[name] = {
            **extra,
            "all": evaluate(scored, preds, relative, n_boot, seed),
            "without_boost_suspect": (
                evaluate([p for p in scored if not p.boost_suspect], preds, relative, n_boot, seed)
                if flags_known else "boost_suspect flags not loaded; see data/context/devtools_enriched.csv"
            ),
            "category_balanced": None if relative else category_balanced(scored, preds, seed=seed),
            "predictions": {pid: asdict(p) for pid, p in preds.items()},
        }
    return {
        **meta,
        "protocol": "v2: within-account viral vs Deadweight, one post per model call",
        "n_sampled": len(picked),
        "n_scored_by_all": len(scored),
        "sample_ids": [p.id for p in picked],
        "sampled_categories": {c: sum(p.category == c for p in scored) for c in CATEGORIES},
        "boost_flags": "loaded from the context worker's enriched CSV" if flags_known else "not loaded",
        "seed": seed,
        "results": results,
        "comparisons": comparisons(scored, all_preds, n_boot, seed,
                                   {n for n, e in extras.items() if e.get("relative")}),
    }


def _fmt(stat: dict | None) -> str:
    if not stat or stat.get("value") is None:
        return "n/a"
    ci = stat.get("ci95")
    return f"{stat['value']:.3f} [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else f"{stat['value']:.3f}"


def markdown(report: dict) -> str:
    first = next(iter(report["results"].values()))["all"]
    lines = [
        f"# Backtest {report['at']}",
        "",
        f"Provider `{report['provider']}`, models `{', '.join(report['models'])}`, panel `{report['panel_path']}`, "
        f"judge memory `{report.get('memory', 'on')}`. {report['protocol']}.",
        "",
        f"Eligible posts: {report['n_eligible']} (dropped: {report['dropped']}). Sample: {report['n_sampled']} posts, "
        f"{report['n_scored_by_all']} scored by every predictor, from {first['n_accounts']} accounts with both "
        f"tiers ({report['sampled_categories']}). Headline pairs: "
        f"{first['headline_viral_vs_deadweight_auc']['n_pairs']}. Brackets are 95% intervals from resampling "
        "accounts. Every number is an upper bound for production (see eval/DESIGN.md).",
        "",
        "| Predictor | Viral vs Deadweight, same account (headline) | Viral vs viral, same account (second headline) "
        "| Same media status | Same launch type | Deadweight vs Deadweight (noise) | Top pick is viral |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, r in report["results"].items():
        a = r["all"]
        lines.append(
            f"| {name} | {_fmt(a['headline_viral_vs_deadweight_auc'])} | {_fmt(a['viral_vs_viral_by_engagement'])} "
            f"| {_fmt(a['same_media_status'])} | {_fmt(a['same_launch_type'])} "
            f"| {_fmt(a['deadweight_vs_deadweight_noise_check'])} | {_fmt(a['top_pick_is_viral'])} |"
        )
    unboosted = {n: r["without_boost_suspect"] for n, r in report["results"].items()
                 if isinstance(r.get("without_boost_suspect"), dict)}
    if unboosted:
        ex = next(iter(unboosted.values()))
        lines += ["", f"Without the {first['n_posts'] - ex['n_posts']} boost-suspect posts ({ex['n_posts']} posts, "
                  f"{ex['headline_viral_vs_deadweight_auc']['n_pairs']} pairs), headline: "
                  + "; ".join(f"`{n}` {_fmt(u['headline_viral_vs_deadweight_auc'])}" for n, u in unboosted.items())]
    lines += ["", "Paired differences in the headline AUC (positive means the row predictor wins):", ""]
    for name, row in report["comparisons"].items():
        parts = [f"minus {k[6:]} {_fmt(v)}" for k, v in row.items()]
        lines.append(f"- `{name}`: {'; '.join(parts)}")
    lines += ["", "Confounded by account size (cross-account; a model that knows which brands are big scores well "
              "here without judging the writing):", "",
              "| Predictor | Spearman vs log engagement | Super Viral vs rest AUC | Super vs Medium Viral AUC "
              "(author floor 0.763) | Category-balanced protocol (aarav/self-improving) |",
              "| --- | --- | --- | --- | --- |"]
    for name, r in report["results"].items():
        c = r["all"]["confounded_by_account_size"]
        cb = r.get("category_balanced") or {}
        bal = ("n/a" if cb.get("cross_account_spearman_median") is None else
               f"{cb['cross_account_spearman_median']:.3f} ({cb['cross_account_spearman_range95'][0]:.2f} to "
               f"{cb['cross_account_spearman_range95'][1]:.2f})")
        sp = "n/a" if c["spearman_vs_log_engagement"] is None else f"{c['spearman_vs_log_engagement']:.3f}"
        sv = "n/a" if c["super_viral_vs_rest_auc"] is None else f"{c['super_viral_vs_rest_auc']:.3f}"
        sm = "n/a" if c.get("super_vs_medium_auc") is None else f"{c['super_vs_medium_auc']:.3f}"
        lines.append(f"| {name} | {sp} | {sv} | {sm} | {bal} |")
    notes = []
    for name, r in report["results"].items():
        if r.get("retest"):
            rt = r["retest"]
            sp = "n/a" if rt["spearman_between_runs"] is None else f"{rt['spearman_between_runs']:.3f}"
            notes.append(f"- Retest of `{name}` on {rt['n_posts']} posts: Spearman between runs {sp}.")
        if r.get("failures"):
            notes.append(f"- `{name}` failed on {len(r['failures'])} posts, dropped from every predictor.")
        if r.get("calls"):
            notes.append(f"- `{name}`: {r['calls']} model calls ({r.get('model_info', {}).get('model', '?')}).")
    if notes:
        lines += ["", *notes]
    return "\n".join(lines) + "\n"


def default_path(report: dict, out_dir: Path = REPORT_DIR) -> Path:
    return out_dir / f"backtest-{report['at'].replace(':', '').replace('-', '')}-{report['provider']}.json"


def write(report: dict, path: Path) -> tuple[Path, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    md = path.with_suffix(".md")
    md.write_text(markdown(report), encoding="utf-8")
    return path, md


def fitness(predictor: Predictor, cfg: BacktestConfig = BacktestConfig(), posts: list[Post] | None = None) -> dict:
    """The eval protocol as one call, for the self-iteration loop (sam/iterate).

    Scores `predictor` on the protocol's sample and returns the headline within-account viral vs
    Deadweight AUC with its account-bootstrap interval. Compare two candidates with
    `paired_difference` on the same sample rather than by their intervals.
    """
    from harness.evals.dataset import load

    posts = posts if posts is not None else load()
    pool, _ = eligible(posts, cfg)
    picked = sample(pool, cfg.per_tier, cfg.seed)
    preds = predictor.predict(picked)
    scored = [p for p in picked if p.id in preds]
    head = evaluate(scored, preds, getattr(predictor, "relative", False), cfg.n_boot, cfg.seed)
    return {**head["headline_viral_vs_deadweight_auc"], "n_posts": len(scored),
            "n_accounts": head["n_accounts"], "failures": len(picked) - len(scored)}
