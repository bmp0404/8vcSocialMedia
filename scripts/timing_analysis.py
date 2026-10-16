"""When to post: engagement lift by local (PT) hour block and day of week, normalized within account.

    python scripts/timing_analysis.py                       # Mid-size + Smaller/closest
    python scripts/timing_analysis.py --segments Other      # replication on the other accounts

Posts: mature, eligible (not thread continuations), not boost-flagged, from data/context/devtools_enriched.csv;
repeated texts (same account, same first 60 chars) are kept once; accounts with >= 3 posts. Outcome per
post: log(engagement / author median) (label_lift_window) or log(engagement per view), minus the
account's mean, so a bucket's value is "vs. that account's usual". For each bucket: raw mean, a shrunk
mean (n / (n + K) toward 0) and a 90% account-bootstrap CI. Overall test: a within-account permutation
test of the bucket labels (between-bucket sum of squares). Also run on within-account percentile ranks
(robust to a few huge outliers) and leaving out each account. Stdlib only, no LLM calls.
"""

import argparse
import collections
import csv
import math
import random
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.evaluator.metrics import percentile_within  # noqa: E402

HOURS = [(0, 6, "00-05"), (6, 9, "06-08"), (9, 12, "09-11"), (12, 15, "12-14"), (15, 18, "15-17"), (18, 24, "18-23")]
DOW = {"Mon": "Mon", "Tue": "Tue-Thu", "Wed": "Tue-Thu", "Thu": "Tue-Thu", "Fri": "Fri", "Sat": "Sat-Sun", "Sun": "Sat-Sun"}
K = 20  # shrinkage prior strength (posts)


def hour_bucket(h: int) -> str:
    return next(name for lo, hi, name in HOURS if lo <= h < hi)


def load(path: Path, segments: set[str]) -> list[dict]:
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    t = lambda v: str(v).strip().lower() == "true"  # noqa: E731
    keep, seen = [], set()
    for r in rows:
        if r["Segment"] not in segments or not (t(r["label_mature"]) and t(r["label_eligible"])):
            continue
        if t(r["boost_suspect"]) or t(r["boost_likely_inauthentic"]):
            continue
        k = (r["Account"], " ".join(r["Post Content"].split())[:60])
        if k not in seen:
            seen.add(k)
            keep.append(r)
    return keep


def centered(rows: list[dict], outcome: str, rank: bool, drop: str | None = None) -> dict[str, list[tuple]]:
    """{account: [(hour bucket, dow bucket, centered outcome)]}"""
    by = collections.defaultdict(list)
    for r in rows:
        if r["Account"] == drop:
            continue
        if outcome == "lift":
            y = float(r["label_lift_window"])
        elif r["outcome_eng_per_view"]:
            y = math.log(float(r["outcome_eng_per_view"]))
        else:
            continue
        by[r["Account"]].append((hour_bucket(int(float(r["pre_hour_local"]))), DOW[r["pre_dow_local"]], y))
    out = {}
    for a, v in by.items():
        if len(v) < 3:
            continue
        ys = [y for *_, y in v]
        ys = [p - 0.5 for p in percentile_within(ys, [a] * len(ys))] if rank else [y - st.mean(ys) for y in ys]
        out[a] = [(h, d, y) for (h, d, _), y in zip(v, ys)]
    return out


def ss_between(acc: dict, idx: int) -> tuple[float, dict]:
    g = collections.defaultdict(list)
    for v in acc.values():
        for p in v:
            g[p[idx]].append(p[2])
    mu = st.mean(y for vs in g.values() for y in vs)
    return sum(len(vs) * (st.mean(vs) - mu) ** 2 for vs in g.values()), g


def perm_p(acc: dict, idx: int, n: int, rng: random.Random) -> float:
    s0, _ = ss_between(acc, idx)
    hits = 0
    for _ in range(n):
        perm = {}
        for a, v in acc.items():
            labs = [p[idx] for p in v]
            rng.shuffle(labs)
            perm[a] = [(lab, lab, p[2]) for p, lab in zip(v, labs)]
        hits += ss_between(perm, idx)[0] >= s0
    return (hits + 1) / (n + 1)


def boot_ci(acc: dict, idx: int, rng: random.Random, n: int = 1000) -> dict[str, tuple[float, float]]:
    accts = list(acc)
    vals = collections.defaultdict(list)
    for _ in range(n):
        g = collections.defaultdict(list)
        for _ in accts:
            for p in acc[rng.choice(accts)]:
                g[p[idx]].append(p[2])
        for k, v in g.items():
            vals[k].append(st.mean(v))
    return {k: (sorted(v)[int(0.05 * len(v))], sorted(v)[int(0.95 * len(v)) - 1]) for k, v in vals.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--enriched", default=str(ROOT / "data" / "context" / "devtools_enriched.csv"))
    ap.add_argument("--segments", default="Mid-size,Smaller/closest")
    ap.add_argument("--perms", type=int, default=2000)
    args = ap.parse_args()
    rows = load(Path(args.enriched), {s.strip() for s in args.segments.split(",")})
    rng = random.Random(0)
    for outcome in ("lift", "epv"):
        acc = centered(rows, outcome, rank=False)
        accr = centered(rows, outcome, rank=True)
        n = sum(map(len, acc.values()))
        name = "log(engagement / author median)" if outcome == "lift" else "log(engagement per view)"
        print(f"\n== {name}, centered within account: {n} posts, {len(acc)} accounts ({args.segments})")
        for idx, label in ((0, "hour PT"), (1, "day")):
            _, g = ss_between(acc, idx)
            ci = boot_ci(acc, idx, rng)
            p_raw, p_rank = perm_p(acc, idx, args.perms, rng), perm_p(accr, idx, args.perms, rng)
            loao = [perm_p(centered(rows, outcome, False, drop=a), idx, 300, rng) for a in sorted(acc)]
            print(f"{label}: permutation p = {p_raw:.3f} (log values), {p_rank:.3f} (percentile ranks); "
                  f"leave-one-account-out p range {min(loao):.3f}-{max(loao):.3f}")
            for k in sorted(g):
                m = st.mean(g[k])
                shrunk = m * len(g[k]) / (len(g[k]) + K)
                lo, hi = ci[k]
                print(f"  {k:<8} n={len(g[k]):>3}  mean {m:+.2f} (x{math.exp(m):.2f})  shrunk {shrunk:+.2f} "
                      f"(x{math.exp(shrunk):.2f})  90% CI [{lo:+.2f},{hi:+.2f}]")


if __name__ == "__main__":
    main()
