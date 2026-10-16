"""`python -m harness.onboard validate`: onboarding checked on corpus brands. See validate.py and DESIGN.md."""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from harness.corpus.load import load
from harness.llm import get_llm
from harness.onboard import validate
from harness.watcher import modelrec


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m harness.onboard")
    p.add_argument("command", choices=["validate"])
    p.add_argument("--out", type=Path, required=True,
                   help="directory for drafted profiles, verdicts and results; keep it outside the repository, "
                        "since verdict rows quote corpus posts")
    p.add_argument("--brands", default=",".join(validate.BRANDS), help="comma-separated; each needs a site in BRANDS")
    p.add_argument("--cutoff", default=validate.CUTOFF.date().isoformat())
    p.add_argument("--provider", help="override HARNESS_PROVIDER")
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()

    brands = {b: validate.BRANDS[b] for b in args.brands.split(",")}
    cutoff = datetime.fromisoformat(args.cutoff).replace(tzinfo=timezone.utc)
    corpus = load()
    planned = validate.planned_calls(corpus, brands, cutoff)
    if planned > validate.LLM_BUDGET:
        raise SystemExit(f"[validate] would make up to {planned} LLM calls, over the budget of {validate.LLM_BUDGET}")
    print(f"[validate] {len(brands)} brands, cutoff {cutoff.date()}, up to {planned} LLM calls")
    args.out.mkdir(parents=True, exist_ok=True)
    llm = get_llm(args.provider)
    modelrec.install()
    results = []
    with modelrec.recording() as calls:
        for brand, site in brands.items():
            res, rows = validate.run_brand(corpus, brand, site, llm, args.out, cutoff, workers=args.workers)
            results.append(res)
            (args.out / f"verdicts-{brand}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            print(json.dumps({"brand": brand, "recall": res.recall, "claims": {k: v for k, v in res.claims.items()
                                                                                 if k != "failures"},
                              "fit": res.fit}, indent=1))
    models = Counter((c.get("requested"), c.get("reported")) for c in calls)
    summary = {"cutoff": cutoff.isoformat(), "llm_calls": len(calls),
               "models": [{"requested": r, "reported": m, "calls": n} for (r, m), n in models.items()],
               "brands": [vars(r) for r in results]}
    (args.out / "results.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"[validate] {len(calls)} LLM calls; models {dict(models)}; results in {args.out / 'results.json'}")


if __name__ == "__main__":
    main()
