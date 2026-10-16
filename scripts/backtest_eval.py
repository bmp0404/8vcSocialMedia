"""Backtest the evaluator by retrodiction: do eval methods rank real posts the way real engagement did?

    python scripts/backtest_eval.py --feed data/devtools_virality_30d.csv
    python scripts/backtest_eval.py --feed data/backtest_fixture.json
    python scripts/backtest_eval.py --feed <path> --methods rubric,pairwise --deciders claude,jev

Methods: panel (current qualify.py audience panel), rubric, pairwise (Swiss/round-robin + Bradley-Terry,
within each account), triage (P(engage), a side metric), z-score combos, and text-only baselines
(length, has_media, random). Each decision method runs once per decider backend that has a key
(HARNESS_DECIDER backends: claude always; jev with JEV_API_KEY; openai_decisions with OPENAI_API_KEY).
All LLM / decision calls are cached in out/eval_cache, so reruns are cheap. Post text is passed to the
models as data, never as instructions.
"""

import argparse
import dataclasses
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.evaluator import backtest as B  # noqa: E402
from harness.evaluator import cache  # noqa: E402
from harness.evaluator.candidates import EvalContext  # noqa: E402
from harness.evaluator.decide import available_backends  # noqa: E402
from harness.llm import get_llm  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed", required=True, help="CSV / JSON / JSONL of posts with engagement")
    ap.add_argument("--target", default="auto", choices=["auto", "account", "impressions", "followers", "raw"])
    ap.add_argument("--segments", default="Mid-size,Smaller/closest",
                    help="comma list of Segment values to keep ('' = all); ignored if the feed has none")
    ap.add_argument("--accounts", type=int, default=10, help="accounts to sample (account target)")
    ap.add_argument("--skip-accounts", type=int, default=0,
                    help="skip the first N sampled accounts (e.g. --skip-accounts 10 = a disjoint holdout)")
    ap.add_argument("--per-account", type=int, default=8, help="posts per sampled account")
    ap.add_argument("--limit", type=int, default=120, help="hard cap on posts evaluated")
    ap.add_argument("--methods", default="panel,rubric,pairwise,triage")
    ap.add_argument("--deciders", default="auto", help="comma list or 'auto' (every backend with a key)")
    ap.add_argument("--pair-audience", default="neutral",
                    help="pairwise judge audience(s), comma list of neutral | mission (the brand's panel)")
    ap.add_argument("--pair-test", type=int, default=0,
                    help="also judge N same-account viral-vs-deadweight pairs (teammate-comparable); 0 = off")
    ap.add_argument("--pair-tiers", default="fast", help="claude tiers for --pair-test: fast,judge")
    ap.add_argument("--provider", default=None, help="override HARNESS_PROVIDER (e.g. mock)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json-out", default=None, help="per-post scores (default out/backtest_<feed>.json)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    posts = B.load(args.feed)
    total = len(posts)
    posts, mode = B.normalize(posts, args.target)
    segs = [s.strip() for s in args.segments.split(",") if s.strip()]
    if not any(p.segment for p in posts):
        segs = []
    picked = B.sample(posts, segs or None, args.accounts, args.per_account, args.seed, skip=args.skip_accounts)[: args.limit]
    grouped = mode == "account"
    synth = sum(p.synthetic for p in picked)
    print(f"[backtest] {args.feed}: {total} posts loaded, target={mode}, evaluating {len(picked)} "
          f"from {len({p.group for p in picked})} group(s)" + (f", segments={segs}" if segs else "")
          + (f"; {synth} SYNTHETIC" if synth else ""))

    llm = get_llm(args.provider)
    ectx = EvalContext(llm=llm, mission=config.mission(), lane="post", seed=args.seed, triage=False)
    methods = {m.strip() for m in args.methods.split(",") if m.strip()}
    deciders = available_backends() if args.deciders == "auto" else [d.strip() for d in args.deciders.split(",")]

    scores: dict[str, dict[str, float]] = {}
    extra: list[str] = []
    if "panel" in methods:
        scores["panel (qualify.py, absolute 0-1)"] = B.run_panel(picked, ectx)
    for d in deciders:
        if "rubric" in methods:
            tot, per = B.run_rubric(picked, ectx, d)
            scores[f"rubric[{d}] (engagement criteria)"] = tot
            for crit, m in per.items():
                scores[f"  rubric[{d}].{crit}"] = m
        for aud in ([a.strip() for a in args.pair_audience.split(",") if a.strip()] if "pairwise" in methods else []):
            wp, t = B.run_pairwise(picked, dataclasses.replace(ectx, pair_audience=aud), d)
            pname = f"pairwise[{d},{aud}]"
            scores[f"{pname} (BT win prob)"] = wp
            tgt = {p.id: p.target for p in picked}
            decided = [r for r in t.results if tgt[r.i] != tgt[r.j]]
            direct = sum((r.s - 0.5) * (tgt[r.i] - tgt[r.j]) > 0 for r in decided) / max(1, len(decided))
            ties = sum(r.s == 0.5 for r in decided) / max(1, len(decided))
            cons = sum(r.consistent for r in t.results) / max(1, len(t.results))
            extra.append(f"{pname}: {len(t.results)} pairs ({t.calls} decisions); both orders agreed on "
                         f"{cons:.0%}; on judged pairs the judge picked the actual winner {direct:.0%}, tie {ties:.0%}")
        if "triage" in methods:
            scores[f"triage[{d}] P(engage)"] = B.run_triage(picked, ectx, d)
        aud0 = args.pair_audience.split(",")[0].strip()
        r, pw = scores.get(f"rubric[{d}] (engagement criteria)"), scores.get(f"pairwise[{d},{aud0}] (BT win prob)")
        pan = scores.get("panel (qualify.py, absolute 0-1)")
        if r and pw:
            scores[f"combo rubric+pairwise[{d}]"] = B.combine(r, pw)
        if pan and r:
            scores[f"combo panel+rubric[{d}]"] = B.combine(pan, r)
        if pan and pw:
            scores[f"combo panel+pairwise[{d}]"] = B.combine(pan, pw)
        if pan and r and pw:
            scores[f"combo panel+rubric+pairwise[{d}]"] = B.combine(pan, r, pw)

    rng = random.Random(args.seed)
    scores["baseline: length"] = {p.id: float(B.body_len(p.text)) for p in picked}
    if any(p.media for p in picked):
        scores["baseline: has_media (not shown to judges)"] = {
            p.id: float(p.media.lower() not in {"", "none"}) for p in picked}
    scores["baseline: random"] = {p.id: rng.random() for p in picked}

    if args.pair_test:
        # pairs come from accounts NOT in the ranked sample (held out from anything tuned on it)
        seen = {p.author for p in picked}
        pool = [p for p in posts if p.author not in seen]
        vp = B.viral_pairs(pool, args.pair_test, args.seed, segs or None)
        for d in deciders:
            for tier in ([t.strip() for t in args.pair_tiers.split(",")] if d == "claude" else ["fast"]):
                for aud in [a.strip() for a in args.pair_audience.split(",") if a.strip()]:
                    r = B.judge_pairs(vp, dataclasses.replace(ectx, pair_audience=aud, pair_tier=tier), d)
                    extra.append(f"same-account viral-vs-deadweight pairs [{d},{tier},{aud}]: {r['acc']:.2f} "
                                 f"(90% CI {r['lo']:.2f}-{r['hi']:.2f}; strict {r['strict']}/{r['n']}, ties {r['ties']}, "
                                 f"orders agreed {r['consistent']:.0%}) from {len({h.author for h, _ in vp})} accounts")

    rows = [(name, B.evaluate_scores(picked, s, grouped)) for name, s in scores.items()]
    target_desc = {"account": "within-account engagement percentile", "impressions": "engagement / impressions",
                   "followers": "engagement / followers", "raw": "raw engagement"}[mode]
    print(f"\nTarget: {target_desc}." + (" pair_acc and viral_AUC count within-account pairs only." if grouped else "")
          + " P@25% = share of the method's top quartile that is in the actual top quartile.\n")
    print(B.fmt_table(rows))
    for line in extra:
        print(line)
    st = cache.STATS
    labels = sorted({k.rsplit(":", 1)[0] for k in st})
    print("\ncalls (new / cached): " + ", ".join(f"{lab} {st[lab + ':call']}/{st[lab + ':hit']}" for lab in labels))

    out = Path(args.json_out) if args.json_out else config.OUT_DIR / f"backtest_{Path(args.feed).stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "feed": args.feed, "target": mode, "n": len(picked),
        "metrics": {n: r for n, r in rows},
        "posts": [{"id": p.id, "author": p.author, "text": p.text, "eng": p.eng, "target": p.target,
                   "category": p.category, "segment": p.segment,
                   "scores": {n: s.get(p.id) for n, s in scores.items()}} for p in picked],
    }, indent=1, default=str), encoding="utf-8")
    print(f"per-post scores -> {out}")


if __name__ == "__main__":
    main()
