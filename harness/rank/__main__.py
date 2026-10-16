"""python -m harness.rank replies [--labels] | posts

Scores the reply baselines and the learned ranker on the frozen reply benchmark and writes
data/bench/results/replies.json (or replies-labels.json with the label ablation).
"""

import argparse
import json
import math
import zlib
from collections import defaultdict
from datetime import datetime

from harness.corpus import load
from harness.corpus.bench import (BENCH_DIR, DEV_BEFORE, DEV_CUTOFFS, REPLY_MIN_LIKES, REPLY_RATIO, TEST_CUTOFFS,
                                  WEEK, WEEK0, Pair, author_split, load_pairs, reply_pairs, window)
from harness.corpus.stats import accuracy, credits_for, pair_credit, paired_difference
from harness.rank import post, reply
from harness.rank.baselines import fit_baselines

RESULTS_DIR = BENCH_DIR / "results"


def _subsets(pairs, corpus) -> dict[str, list[int]]:
    """Indices of pairs in the confound-controlled subsets."""
    out = {"all": list(range(len(pairs))), "high_coverage": [], "followers_within_3x": [], "delay_within_2x": [],
           "both_matched": []}
    for i, p in enumerate(pairs):
        w, l = corpus.get(p.winner), corpus.get(p.loser)
        root = corpus.get(p.group)
        fr = abs(math.log10((w.author_followers or 0) + 1) - math.log10((l.author_followers or 0) + 1))
        mw = max((w.created_at - root.created_at).total_seconds() / 60, 0.5)
        ml = max((l.created_at - root.created_at).total_seconds() / 60, 0.5)
        dr = abs(math.log(mw / ml))
        if (p.coverage or 0) >= 0.8:
            out["high_coverage"].append(i)
        if fr <= math.log10(3):
            out["followers_within_3x"].append(i)
        if dr <= math.log(2):
            out["delay_within_2x"].append(i)
        if fr <= math.log10(3) and dr <= math.log(2):
            out["both_matched"].append(i)
    return out


def score_baselines(train, test, corpus, b=2000) -> dict:
    scorers = fit_baselines(train, corpus)
    subsets = _subsets(test, corpus)
    out = {}
    for name, s in scorers.items():
        cr = credits_for(test, s)
        out[name] = {}
        for sub, idx in subsets.items():
            if len(idx) >= 20:
                out[name][sub] = accuracy([cr[i] for i in idx], [test[i].group for i in idx], b).to_dict()
    return out


def run_replies(with_labels: bool = False, label_models: tuple[str, ...] | None = None) -> dict:
    corpus = load()
    pairs = load_pairs("replies")
    dev, test = [p for p in pairs if p.split == "dev"], [p for p in pairs if p.split == "test"]
    results = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "pairs": {"dev": len(dev), "test": len(test)},
               "threads": {"dev": len({p.group for p in dev}), "test": len({p.group for p in test})}}
    results["baselines_test"] = score_baselines(dev, test, corpus)
    windows = {}
    for cutoff in DEV_CUTOFFS + TEST_CUTOFFS:
        train, ev = window(pairs, end=cutoff), window(pairs, start=cutoff, end=cutoff + WEEK)
        windows[cutoff.date().isoformat()] = {
            "train_pairs": len(train), "train_threads": len({p.group for p in train}),
            "eval_pairs": len(ev), "eval_threads": len({p.group for p in ev}),
            "split": "dev" if cutoff < DEV_BEFORE else "test",
            "baselines": {k: v["all"] for k, v in score_baselines(train, ev, corpus).items()},
        }
    results["baselines_by_cutoff"] = windows

    # Margin sensitivity: the two strongest baselines on test pairs built under other margins.
    sens = {}
    for min_likes, ratio in ((3, 2.0), (REPLY_MIN_LIKES, REPLY_RATIO), (10, 5.0)):
        alt = reply_pairs(corpus, min_likes, ratio)
        a_dev, a_test = [p for p in alt if p.split == "dev"], [p for p in alt if p.split == "test"]
        sc = fit_baselines(a_dev, corpus)
        sens[f">={min_likes} likes, >={ratio}x"] = {
            "test_pairs": len(a_test), "test_threads": len({p.group for p in a_test}),
            **{n: accuracy(credits_for(a_test, sc[n]), [p.group for p in a_test]).estimate for n in ("delay", "followers", "length")}}
    results["margin_sensitivity"] = {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                     for k, v in sens.items()}

    labels = schema = None
    if with_labels:
        from harness.label.features import REFERENCE_MODELS, reply_label_votes
        label_models = label_models or REFERENCE_MODELS
        labels, schema = reply_label_votes(label_models)
        bench_items = {f"x:{s}" for p in pairs for s in (p.winner, p.loser)}
        results["labels"] = {"models": list(label_models), "benchmark_replies": len(bench_items),
                             "benchmark_replies_labeled": len(bench_items & labels.keys())}
    variants = {"timing": ("timing",), "text": ("text",), "full": ("timing", "text"),
                "full+ngrams": ("timing", "text", "ngrams")}
    if with_labels:
        variants |= {"labels": ("labels",), "timing+labels": ("timing", "labels"),
                     "full+labels": ("timing", "text", "labels")}
    ranker = {}
    pooled = {"groups": [], "credits": {n: [] for n in variants}}
    for cutoff in DEV_CUTOFFS + TEST_CUTOFFS:
        models = {name: reply.fit(cutoff, blocks, corpus, labels=labels, schema=schema)
                  for name, blocks in variants.items()}
        entry = {"l2": {n: m.l2 for n, m in models.items()}}
        entry["full_vs_timing"] = reply.benchmark(models["full"], cutoff, models["timing"], corpus, labels).to_dict()
        ev = window(pairs, start=cutoff, end=cutoff + WEEK)
        inputs = reply.reply_inputs(corpus, labels)
        groups = [p.group for p in ev]
        cr = {n: reply.credits(m, ev, inputs) for n, m in models.items()}
        entry["accuracy"] = {n: accuracy(c, groups).to_dict() for n, c in cr.items()}
        entry["full+ngrams_vs_timing"] = paired_difference(cr["full+ngrams"], cr["timing"], groups).to_dict()
        if with_labels:
            entry["full+labels_vs_full"] = paired_difference(cr["full+labels"], cr["full"], groups).to_dict()
            entry["timing+labels_vs_timing"] = paired_difference(cr["timing+labels"], cr["timing"], groups).to_dict()
            entry["labels_vs_text"] = paired_difference(cr["labels"], cr["text"], groups).to_dict()
        ranker[cutoff.date().isoformat()] = entry
        if cutoff in TEST_CUTOFFS:
            pooled["groups"] += groups
            for n in variants:
                pooled["credits"][n] += cr[n]
    # Both test weeks together, each scored by the model fitted at its own cutoff.
    pg, pc = pooled["groups"], pooled["credits"]
    pooled_out = {"accuracy": {n: accuracy(c, pg).to_dict() for n, c in pc.items()},
                  "full_vs_timing": paired_difference(pc["full"], pc["timing"], pg).to_dict(),
                  "full+ngrams_vs_timing": paired_difference(pc["full+ngrams"], pc["timing"], pg).to_dict()}
    if with_labels:
        pooled_out["full+labels_vs_full"] = paired_difference(pc["full+labels"], pc["full"], pg).to_dict()
        pooled_out["timing+labels_vs_timing"] = paired_difference(pc["timing+labels"], pc["timing"], pg).to_dict()
        pooled_out["labels_vs_text"] = paired_difference(pc["labels"], pc["text"], pg).to_dict()
    results["ranker"] = ranker
    results["ranker_pooled_test"] = pooled_out
    return results


def _pair_set(posts_by_acct: dict, min_hi: int = 100, max_lo: int = 10) -> list:
    """Same-account pairs of a post with >= min_hi engagement against one with <= max_lo (the CSV's tiers)."""
    out = []
    for acct, posts in posts_by_acct.items():
        hi = [p for p in posts if p.engagement >= min_hi]
        lo = [p for p in posts if p.engagement <= max_lo]
        out += [Pair(f"{acct}:{h.status_id}:{l.status_id}", "posts", acct, h.status_id, l.status_id,
                     h.engagement, l.engagement, author_split(acct)) for h in hi for l in lo]
    return out


def run_posts(alpha: float = 10.0, folds: int = 5, with_labels: bool = False) -> dict:
    corpus = load()
    pairs = load_pairs("posts")
    test = [p for p in pairs if p.split == "test"]
    res = {"pairs": {"dev": len(pairs) - len(test), "test": len(test)},
           "authors": {"dev": len({p.group for p in pairs if p.split == "dev"}), "test": len({p.group for p in test})}}
    res["format_logistic"] = post.format_baseline(pairs, corpus)
    if with_labels:
        from harness.label.features import reply_label_votes
        votes, schema = reply_label_votes()
        res["label_logistic"] = post.label_baseline(pairs, votes, schema)

    targets = post.within_account_targets(corpus)
    accounts = sorted({a for a, _ in targets.values()})
    # Every in-window own post of the dataset accounts, including thread tails and immature ones,
    # so the "easy pairs" question can be scored with the same out-of-fold predictions.
    acct_set = set(accounts)
    window_posts = defaultdict(list)
    for it in corpus.items:
        a = it.author.lower()
        if a in acct_set and (it.kind == "post" or it.is_thread_continuation) and not it.reposted_by \
                and WEEK0 <= it.created_at and len(post.clean_text(it.text)) >= 10:
            window_posts[a].append(it)
    oof = {}
    fold_of = {a: i % folds for i, a in enumerate(sorted(accounts, key=lambda a: zlib.crc32(a.encode())))}
    for f in range(folds):
        train_ids = [sid for sid, (a, _) in targets.items() if fold_of[a] != f]
        model = post.ridge_fit([post.clean_text(corpus.get(s).text) for s in train_ids],
                               [targets[s][1] for s in train_ids], alpha)
        for a in accounts:
            if fold_of[a] == f:
                for it in window_posts[a]:
                    oof[it.status_id] = model.predict(post.clean_text(it.text))
        print(f"[posts] tfidf fold {f + 1}/{folds} done")

    def score_on(ps):
        ps = [p for p in ps if p.winner in oof and p.loser in oof]
        if not ps:
            return None
        return accuracy([pair_credit(oof[p.winner], oof[p.loser]) for p in ps], [p.group for p in ps]).to_dict()
    eligible = {a: [p for p in ps if p.status_id in targets] for a, ps in window_posts.items()}
    dirty = {a: [p for p in ps if p.boost_suspect is not True] for a, ps in window_posts.items()}
    def vs_length(ps):
        ps = [p for p in ps if p.winner in oof and p.loser in oof]
        cr = [pair_credit(oof[p.winner], oof[p.loser]) for p in ps]
        cl = [pair_credit(len(post.clean_text(corpus.get(p.winner).text)), len(post.clean_text(corpus.get(p.loser).text)))
              for p in ps]
        g = [p.group for p in ps]
        return {"length": accuracy(cl, g).to_dict(), "tfidf_minus_length": paired_difference(cr, cl, g).to_dict()}
    full = post.ridge_fit([post.clean_text(corpus.get(s).text) for s in targets], [targets[s][1] for s in targets], alpha)
    words = sorted(((k[2:], v) for k, v in full.weights.items() if k.startswith("w:")), key=lambda kv: kv[1])
    res["tfidf_ridge"] = {
        "vs_length_all": vs_length(pairs), "vs_length_test": vs_length(test),
        "top_word_weights": {"lift": [w for w, _ in words[-25:]][::-1], "sink": [w for w, _ in words[:25]]},
        "alpha": alpha, "folds": folds, "posts_fit": len(targets), "accounts": len(accounts),
        "clean_pairs_all": score_on(pairs), "clean_pairs_test": score_on(test),
        "viral_vs_deadweight_clean": score_on(_pair_set(eligible)),
        "viral_vs_deadweight_with_thread_tails_and_immature": score_on(_pair_set(dirty)),
        "length_on_viral_vs_deadweight_clean": accuracy(
            [pair_credit(len(corpus.get(p.winner).text), len(corpus.get(p.loser).text)) for p in _pair_set(eligible)],
            [p.group for p in _pair_set(eligible)]).to_dict(),
    }
    return res


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m harness.rank")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replies")
    r.add_argument("--labels", action="store_true", help="add the LLM label ablation")
    r.add_argument("--label-models", default="", help="comma-separated annotators to average (default: both reference models)")
    ps = sub.add_parser("posts")
    ps.add_argument("--labels", action="store_true", help="add a logistic model on the LLM label fields")
    args = p.parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.cmd == "posts":
        res = run_posts(with_labels=args.labels)
        out = RESULTS_DIR / ("posts-labels.json" if args.labels else "posts.json")
        out.write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(res, indent=1))
        return
    models = tuple(m for m in args.label_models.split(",") if m) or None
    res = run_replies(args.labels, models)
    name = "replies.json" if not args.labels else f"replies-labels-{'+'.join(models)}.json" if models else "replies-labels.json"
    out = RESULTS_DIR / name
    out.write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    for c, e in res["ranker"].items():
        d = e["full_vs_timing"]
        print(c, "full", d["model"]["estimate"], "timing", d["baseline"]["estimate"], "delta", d["delta"]["estimate"],
              d["delta"]["ci95"], f"({d['delta']['pairs']} pairs, {d['delta']['clusters']} threads)")


if __name__ == "__main__":
    main()
