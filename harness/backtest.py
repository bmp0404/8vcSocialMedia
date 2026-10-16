"""Backtest the evaluators against real engagement from the dev-tool dataset.

1. Panel: score a stratified sample of real posts, then measure how well each panelist's predictions
   rank-correlate with actual engagement. With --calibrate, those correlations become the panel weights
   qualify uses (until we have 5+ outcomes of our own, when evolve recalibrates on those instead).
2. Judge: show the tournament judge pairs of posts from the same account-size segment, one that went
   viral and one that flopped, and measure how often it picks the winner (50% = coin flip).

Writes eval/backtest-<timestamp>.json. Uses comparable-size accounts only, since a huge account's reach
says little about the writing.
"""

import json
import random
from collections import defaultdict
from datetime import datetime

from harness import config, dataset, memory, virality
from harness.llm import LLM
from harness.metrics import spearman
from harness.models import Action
from harness.qualify import panel_scores, weighted_score
from harness.tournament import judge
from harness.tune import _pairs
from harness.virality import training_set


def _as_action(p: dataset.Post) -> Action:
    return Action(type="post", text=p.clean_text, mind="dataset", narrative_id=p.account)


def backtest(llm: LLM, per_category: int = 10, pairs: int = 20, seed: int = 0, calibrate: bool = False) -> dict:
    rng = random.Random(seed)
    posts = [p for p in dataset.load() if p.segment in dataset.COMPARABLE_SEGMENTS and len(p.clean_text) > 20]
    by_cat = defaultdict(list)
    for p in posts:
        by_cat[p.category].append(p)

    # 1. Panel vs. reality
    sample = [p for cat in sorted(by_cat) for p in rng.sample(by_cat[cat], min(per_category, len(by_cat[cat])))]
    acts = [_as_action(p) for p in sample]
    scores = panel_scores(llm, acts)
    actual = [p.log_engagement for p in sample]
    per_panelist = {
        name: spearman([scores.get(a.id, {}).get(name, 0.5) for a in acts], actual) for name in config.panel()
    }
    weights = {name: round(max(0.2, 1 + rho), 2) for name, rho in per_panelist.items()}
    before = spearman([weighted_score(scores.get(a.id, {}), {}) for a in acts], actual)
    after = spearman([weighted_score(scores.get(a.id, {}), weights) for a in acts], actual)

    # 2. Judge vs. reality: viral vs. flop from the same segment
    match_ups = []
    for _ in range(pairs):
        seg = rng.choice(dataset.COMPARABLE_SEGMENTS)
        hits = [p for p in posts if p.segment == seg and p.category in dataset.VIRAL]
        flops = [p for p in posts if p.segment == seg and p.category == "Deadweight"]
        if hits and flops:
            match_ups.append((rng.choice(hits), rng.choice(flops)))
    pair_acts = [(_as_action(h), _as_action(f)) for h, f in match_ups]
    picks = judge(llm, config.mission(), pair_acts, rng)
    judged = [(w is h) for (h, _), w in zip(pair_acts, picks) if w is not None]
    judge_acc = round(sum(judged) / len(judged), 3) if judged else None

    # 3. Same-account pairs isolate the writing (no account fame); compare with and without retrieved evidence.
    rows = [(p, 0.0) for p in training_set()[3] if len(p.clean_text) >= 20]
    same = _pairs(rows, pairs, rng)
    same_acc = {}
    for evidence in (False, True):
        sp = judge(llm, config.mission(), [(h, f) for h, f, _, _ in same], random.Random(seed), evidence=evidence)
        hits = [w is h for (h, _, _, _), w in zip(same, sp) if w is not None]
        same_acc["with_evidence" if evidence else "no_evidence"] = round(sum(hits) / len(hits), 3) if hits else None
    model = virality.load()

    report = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "provider": config.PROVIDER,
        "panel": {"n_posts": len(sample), "spearman_unweighted": before, "spearman_calibrated": after,
                  "per_panelist_spearman": per_panelist, "suggested_weights": weights},
        "judge": {"n_pairs": len(judged), "accuracy": judge_acc,
                  "same_account": {"n_pairs": len(same), **same_acc}},
        "virality_model": model.meta if model else None,
        "calibrated": calibrate,
    }
    if calibrate:
        memory.kv_set("panel_weights", weights)

    config.EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out = config.EVAL_DIR / f"backtest-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[backtest] panel on {len(sample)} real posts: spearman {before:+.2f} unweighted, {after:+.2f} calibrated")
    for name, rho in sorted(per_panelist.items(), key=lambda kv: -kv[1]):
        print(f"           {name:<26} {rho:+.2f}  -> weight {weights[name]}")
    print(f"[backtest] judge picked the viral post in {judge_acc if judge_acc is not None else 'n/a'} of {len(judged)} pairs (0.5 = chance)")
    print(f"[backtest] same-account pairs ({len(same)}): judge {same_acc['no_evidence']} without evidence, "
          f"{same_acc['with_evidence']} with similar real posts")
    if model:
        print(f"[backtest] virality model (held-out accounts): pair accuracy {model.meta['pair_accuracy']}, "
              f"spearman {model.meta['spearman']:+.3f}")
    else:
        print("[backtest] no virality model yet: `python -m harness train`")
    print(f"[backtest] {'calibrated panel weights' if calibrate else 'weights NOT applied (use --calibrate)'}; report -> {out}")
    return report
