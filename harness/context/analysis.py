"""First analysis of what separates Super Viral from Deadweight in the enriched dataset.

`python -m harness.context analyze` reads data/context/devtools_enriched.csv
(and timeline_posts.csv if present) and writes data/context/analysis.json.
Numbers only; the interpretation, with its caveats, is in DESIGN.md.

Comparisons are made two ways because account identity dominates the labels:
- pooled: Super Viral rows against Deadweight rows, as the CSV labels them;
- within account: for each account with both labels among eligible rows, the
  difference between its Super Viral and Deadweight means, averaged over
  accounts, with a 95% interval from resampling accounts.
Rows are restricted to `label_eligible` (mature, not a thread continuation).
"""

from __future__ import annotations

import csv
import json
import math
import random
import statistics
from collections import Counter, defaultdict

from harness.context.backfill import CONTEXT_DIR, TIMELINES, read_jsonl

ENRICHED = CONTEXT_DIR / "devtools_enriched.csv"
TIMELINE_POSTS = CONTEXT_DIR / "timeline_posts.csv"
OUT = CONTEXT_DIR / "analysis.json"

BINARY = [
    "pre_has_media", "pre_hook_announce", "pre_hook_feature_tip", "pre_hook_funding_deal", "pre_hook_event",
    "pre_hook_social_proof", "pre_hook_humor", "pre_hook_hiring", "pre_mentions_big_name", "pre_has_question",
    "pre_has_number", "pre_is_thread", "pre_has_list", "pre_first_person_singular", "pre_first_person_plural",
    "pre_all_lowercase", "pre_short_no_link", "pre_round_minute", "pre_is_weekend_local", "pre_is_quote_post",
    "pre_starts_with_mention",
]
NUMERIC = [
    "pre_n_chars", "pre_n_words", "pre_n_lines", "pre_n_links_text", "pre_n_mentions", "pre_n_hashtags",
    "pre_n_emoji", "pre_n_exclaim", "pre_caps_ratio", "pre_hour_local", "pre_author_posts_prev2h",
    "pre_hn_stories_prev24h", "pre_wiki_prev_day_ratio",
]


def _f(v):
    if v in ("", None):
        return None
    if v in ("True", "False"):
        return 1.0 if v == "True" else 0.0
    try:
        return float(v)
    except ValueError:
        return None


def auc(pos: list[float], neg: list[float]) -> float | None:
    """Probability a random positive outranks a random negative (ties count half)."""
    if not pos or not neg:
        return None
    ranked = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank_sum, i = 0.0, 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        avg = (i + j + 1) / 2  # 1-based average rank of the tie block
        rank_sum += avg * sum(1 for k in range(i, j) if ranked[k][1])
        i = j
    n1, n0 = len(pos), len(neg)
    return (rank_sum - n1 * (n1 + 1) / 2) / (n1 * n0)


def spearman(x: list[float], y: list[float]) -> float | None:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j < len(order) and v[order[j]] == v[order[i]]:
                j += 1
            for k in range(i, j):
                r[order[k]] = (i + j - 1) / 2
            i = j
        return r

    if len(x) < 10:
        return None
    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 3) if den else None


def within_account(rows: list[dict], feature: str, pos: str, neg: str, seed: int = 11, boots: int = 2000) -> dict:
    by = defaultdict(lambda: {pos: [], neg: []})
    for r in rows:
        v = _f(r.get(feature))
        if v is not None and r["Category"] in (pos, neg):
            by[r["Account"]][r["Category"]].append(v)
    diffs = [statistics.mean(d[pos]) - statistics.mean(d[neg]) for d in by.values() if d[pos] and d[neg]]
    if not diffs:
        return {"n_accounts": 0}
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(diffs, k=len(diffs))) for _ in range(boots))
    return {
        "n_accounts": len(diffs),
        "mean_diff": round(statistics.mean(diffs), 4),
        "ci95": [round(means[int(0.025 * boots)], 4), round(means[int(0.975 * boots) - 1], 4)],
        "share_accounts_pos_higher": round(sum(d > 0 for d in diffs) / len(diffs), 3),
    }


def pooled(rows: list[dict], feature: str, pos: str, neg: str) -> dict:
    a = [v for r in rows if r["Category"] == pos and (v := _f(r.get(feature))) is not None]
    b = [v for r in rows if r["Category"] == neg and (v := _f(r.get(feature))) is not None]
    if not a or not b:
        return {}
    return {"pos_mean": round(statistics.mean(a), 4), "neg_mean": round(statistics.mean(b), 4),
            "n_pos": len(a), "n_neg": len(b), "auc": round(auc(a, b), 3)}


def timeline_prior_auc(rows: list[dict], gap_h: float = 72, min_posts: int = 5) -> dict:
    """Leakage check for the author floor: a prior built only from timeline posts (never CSV rows,
    which were selected on the outcome), ending `gap_h` before each post so no earlier post's
    count can include engagement that arrived after the target was posted."""
    from datetime import datetime, timedelta

    hist = defaultdict(list)
    for t in read_jsonl(TIMELINES):
        if "error" in t or t.get("is_repost"):
            continue
        if t.get("replying_to") and t["replying_to"].lower() == t["timeline_of"].lower():
            continue
        eng = t["likes"] + t["reposts"] + t["replies"] + t["quotes"]
        hist["@" + t["timeline_of"].lower()].append((datetime.fromisoformat(t["created_at"]), eng))

    def prior(r):
        c = datetime.fromisoformat(r["created_at_utc"]) - timedelta(hours=gap_h)
        h = [e for t, e in hist.get(r["Account"].lower(), []) if t < c]
        return statistics.median(h) if len(h) >= min_posts else None

    elig = [r for r in rows if r.get("label_eligible") == "True" and r["Account"].lower() in hist]
    out = {"gap_hours": gap_h, "accounts_with_timeline": len({r["Account"] for r in elig})}
    for pos, neg in (("Super Viral", "Deadweight"), ("Medium Viral", "Deadweight"), ("Super Viral", "Medium Viral")):
        a = [p for r in elig if r["Category"] == pos and (p := prior(r)) is not None]
        b = [p for r in elig if r["Category"] == neg and (p := prior(r)) is not None]
        out[f"{pos} vs {neg}"] = {"auc": round(auc(a, b), 3) if a and b else None, "n": [len(a), len(b)]}
    return out


def reply_curves(rows: list[dict], min_coverage: float = 0.8, min_replies: int = 20) -> dict:
    """Shapes of reply-arrival curves, and per-account decay exponents, from near-complete reply samples.

    Replies are not the whole of engagement (they reach few non-followers), so
    this checks the curve model on real arrival times rather than measuring virality.
    """
    from datetime import datetime

    from harness.context.curves import InflectionParams, classify_shape, curve_from_event_times, features, to_grid
    from harness.context.decay import crane_sornette_class, fit_alpha, post_peak_counts

    samples = defaultdict(list)
    for r in read_jsonl(CONTEXT_DIR / "reply_samples.jsonl"):
        if "id" in r:
            samples[r["reply_to_post"]].append(r)
    p = InflectionParams()
    shapes = defaultdict(Counter)
    per_account = defaultdict(list)
    onsets = defaultdict(list)
    n = 0
    for r in rows:
        reps = samples.get(r["post_id"])
        if r.get("label_eligible") != "True" or not reps:
            continue
        cov = reps[0].get("coverage")
        if cov is None or cov < min_coverage or len(reps) < min_replies:
            continue
        created = datetime.fromisoformat(r["created_at_utc"])
        times = [datetime.fromisoformat(x["created_at"]) for x in reps]
        end = max(datetime.fromisoformat(x["fetched_at"]) for x in reps)
        curve = curve_from_event_times(created, times, end=end)
        f = features(curve, p)
        shapes[r["Category"]][classify_shape(f, p)] += 1
        per_account[r["Account"]].append(post_peak_counts(to_grid(curve, p.dt)))
        onsets[r["Account"]].append((f.rise_bins or 0) <= 1)
        n += 1
    decay = {}
    for acct, posts in per_account.items():
        alpha = fit_alpha(posts)
        if alpha is None:
            continue
        sudden = sum(onsets[acct]) >= len(onsets[acct]) / 2
        decay[acct] = {"alpha": round(alpha, 3), "posts": len(posts), "sudden_onset_majority": sudden,
                       "class": crane_sornette_class(alpha, sudden)}
    return {"posts": n, "min_coverage": min_coverage, "shapes_by_category": shapes, "decay_by_account": decay}


def run() -> dict:
    with ENRICHED.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    res: dict = {"n_rows": len(rows), "by_category": Counter(r["Category"] for r in rows)}
    elig = [r for r in rows if r.get("label_eligible") == "True"]
    res["eligible_by_category"] = Counter(r["Category"] for r in elig)
    res["continuations_by_category"] = Counter(r["Category"] for r in rows if r["is_thread_continuation"] == "True")
    res["immature_by_category"] = Counter(r["Category"] for r in rows if r["label_mature"] == "False")
    res["x_metrics_rows"] = sum(1 for r in rows if r.get("outcome_x_likes"))

    sv_accounts = Counter(r["Account"] for r in rows if r["Category"] == "Super Viral")
    res["accounts"] = {
        "total": len({r["Account"] for r in rows}),
        "with_super_viral": len(sv_accounts),
        "top10_share_of_super_viral": round(sum(c for _, c in sv_accounts.most_common(10)) / sum(sv_accounts.values()), 3),
        "segments_with_super_viral": Counter(r["Segment"] for r in rows if r["Category"] == "Super Viral"),
    }

    sd = [r for r in elig if r["Category"] in ("Super Viral", "Deadweight")]
    nd = [r for r in elig]
    floors = {}
    for feat in ("pre_author_prior_median_eng", "pre_author_prior_viral_share", "outcome_author_followers_at_fetch"):
        a = [_f(r[feat]) for r in sd if r["Category"] == "Super Viral" and _f(r.get(feat)) is not None]
        b = [_f(r[feat]) for r in sd if r["Category"] == "Deadweight" and _f(r.get(feat)) is not None]
        c = [_f(r[feat]) for r in nd if r["Category"] != "Deadweight" and _f(r.get(feat)) is not None]
        d = [_f(r[feat]) for r in nd if r["Category"] == "Deadweight" and _f(r.get(feat)) is not None]
        floors[feat] = {"auc_sv_vs_dw": round(auc(a, b), 3) if a and b else None, "n": [len(a), len(b)],
                        "auc_viral_vs_dw": round(auc(c, d), 3) if c and d else None, "n_viral": [len(c), len(d)]}
    res["author_floor_auc"] = floors
    res["author_floor_timeline_only"] = timeline_prior_auc(rows)

    res["features"] = {}
    for feat in BINARY + NUMERIC:
        res["features"][feat] = {
            "pooled": pooled(sd, feat, "Super Viral", "Deadweight"),
            "within_account": within_account(elig, feat, "Super Viral", "Deadweight"),
            "within_account_mv_vs_dw": within_account(elig, feat, "Medium Viral", "Deadweight"),
            "within_account_sv_vs_mv": within_account(elig, feat, "Super Viral", "Medium Viral"),
        }

    lift = [r for r in elig if _f(r.get("label_lift_prior")) is not None]
    res["lift_prior_spearman"] = {
        "n": len(lift),
        "by_feature": {
            feat: spearman(*zip(*[(_f(r[feat]), _f(r["label_lift_prior"])) for r in lift if _f(r.get(feat)) is not None]))
            if sum(1 for r in lift if _f(r.get(feat)) is not None) >= 10 else None
            for feat in BINARY + NUMERIC
        },
    }

    media = defaultdict(Counter)
    for r in elig:
        media[r["pre_media"]][r["Category"]] += 1
    res["media_by_category"] = media

    res["boost_by_category"] = defaultdict(Counter)
    for r in rows:
        res["boost_by_category"][r["boost_class"]][r["Category"]] += 1

    reach = defaultdict(list)
    for r in elig:
        if _f(r.get("outcome_views_per_follower")) is not None:
            reach[r["Category"]].append((_f(r["outcome_views_per_follower"]), _f(r["outcome_eng_per_view"])))
    res["reach_vs_resonance_median"] = {
        k: {"views_per_follower": round(statistics.median(a for a, _ in v), 4),
            "eng_per_view": round(statistics.median(b for _, b in v if b is not None), 5), "n": len(v)}
        for k, v in reach.items()
    }

    amp = [r for r in elig if _f(r.get("outcome_max_reposter_followers")) is not None and _f(r.get("outcome_author_followers_at_fetch"))]
    res["reposter_larger_than_author"] = {
        cat: {"n": len(v), "share": round(sum(_f(r["outcome_max_reposter_followers"]) > _f(r["outcome_author_followers_at_fetch"]) for r in v) / len(v), 3)}
        for cat in ("Super Viral", "Medium Viral", "Deadweight")
        if (v := [r for r in amp if r["Category"] == cat])
    }

    res["reply_curves"] = reply_curves(rows)

    if TIMELINE_POSTS.exists():
        with TIMELINE_POSTS.open(encoding="utf-8", newline="") as f:
            tl = list(csv.DictReader(f))
        orig = [r for r in tl if r.get("is_thread_continuation") != "True"]
        res["timeline"] = {
            "posts": len(tl),
            "originals": len(orig),
            "in_csv": sum(r["in_csv"] == "True" for r in orig),
            "category_x": Counter(r.get("label_category_x") for r in orig),
            "category_x_not_in_csv": Counter(r.get("label_category_x") for r in orig if r["in_csv"] != "True"),
        }

    OUT.write_text(json.dumps(res, indent=1, default=dict) + "\n", encoding="utf-8")
    return res
