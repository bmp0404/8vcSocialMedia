"""Build data/context/devtools_enriched.csv and data/context/accounts.csv.

The enriched CSV keeps David's eleven columns first, unchanged and in order,
so any loader of the original file (such as `harness.dataset.load` on
aarav/self-improving) reads it as-is; every added column comes after them.

Inputs, all optional except the dataset; a missing input leaves its columns
blank rather than failing:
- data/context/x_metrics.jsonl        (backfill.py) X counts at fetch time
- data/context/author_timelines.jsonl (backfill.py) each account's recent posts
- data/context/reply_samples.jsonl    (collect.py)  first page of replies
- data/context/context_signals.json   (collect.py)  HN, Wikipedia, GDELT context
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from harness.context import boost, snowflake
from harness.context.backfill import CONTEXT_DIR, TIMELINES, X_METRICS, load_dataset, read_jsonl
from harness.context.features import audience_tz, text_features, time_features
from harness.context.settings import DEFAULT, ContextConfig

ENRICHED = CONTEXT_DIR / "devtools_enriched.csv"
ACCOUNTS = CONTEXT_DIR / "accounts.csv"
REPLY_SAMPLES = CONTEXT_DIR / "reply_samples.jsonl"
CONTEXT_SIGNALS = CONTEXT_DIR / "context_signals.json"
ORIGINAL_COLUMNS = [
    "Category", "Account", "Post Content", "Media", "Likes", "Replies",
    "Retweets", "Engagement", "Post URL", "Date", "Segment",
]


def _median(values):
    return statistics.median(values) if values else None


def _num(v):
    return None if v in (None, "") else v


def _account_stats(rows, timelines, window_start, window_end):
    by_acct = defaultdict(list)
    for r in rows:
        by_acct[r["Account"]].append(r)
    tl_by = defaultdict(list)
    for t in timelines:
        if "error" not in t:
            tl_by["@" + t["timeline_of"].lower()].append(t)
    out = {}
    for acct, posts in by_acct.items():
        tl = [
            t for t in tl_by.get(acct.lower(), [])
            if not t["is_repost"] and window_start <= datetime.fromisoformat(t["created_at"]) < window_end
        ]
        seen, uniq = set(), []
        for t in tl:
            if t["id"] not in seen:
                seen.add(t["id"])
                uniq.append(t)
        tl_eng = [t["likes"] + t["reposts"] + t["replies"] + t["quotes"] for t in uniq]
        days = max((window_end - window_start).days, 1)
        out[acct] = {
            "median_eng_dataset": _median([int(p["Engagement"]) for p in posts]),
            "median_eng_timeline": _median(tl_eng),
            "timeline_n": len(uniq),
            "timeline_posts_per_day": round(len(uniq) / days, 2) if uniq else None,
            "timeline_times": sorted(datetime.fromisoformat(t["created_at"]) for t in uniq),
            "n_dataset": len(posts),
            "n_super": sum(p["Category"] == "Super Viral" for p in posts),
            "n_medium": sum(p["Category"] == "Medium Viral" for p in posts),
            "n_dead": sum(p["Category"] == "Deadweight" for p in posts),
        }
    return out


def _reply_features(sample: list[dict], created: datetime, account_terms: list[str], cfg: ContextConfig) -> dict:
    replies = [r for r in sample if "error" not in r]
    feats = boost.reply_signals(
        [{"text": r.get("text"), "author_followers": r.get("author_followers"),
          "created_unix": datetime.fromisoformat(r["created_at"]).timestamp()} for r in replies],
        cfg.boost,
    )
    if replies:
        delays = sorted((datetime.fromisoformat(r["created_at"]) - created).total_seconds() / 60 for r in replies)
        big = [r for r in replies if (r.get("author_followers") or 0) >= cfg.enrich.big_account]
        terms = [t.lower() for t in account_terms if t]
        insiders = [
            r for r in replies
            if terms and any(t in (r.get("author_description") or "").lower() for t in terms)
        ]
        feats.update(
            reply_first_delay_min=round(delays[0], 1),
            reply_median_delay_min=round(statistics.median(delays), 1),
            reply_big_accounts=len(big),
            reply_max_follower=max((r.get("author_followers") or 0) for r in replies),
            reply_insider_share=round(len(insiders) / len(replies), 3),
        )
    return feats


def _context_features(ctx: dict, acct: str, created: datetime) -> dict:
    """Pre-post context only, so the feature cannot be caused by the post itself."""
    out = {}
    a = ctx.get("accounts", {}).get(acct, {})
    day = created.strftime("%Y-%m-%d")
    hn = a.get("hn_stories") or []
    pre = [s for s in hn if 0 < (created.timestamp() - s["created_unix"]) <= 86400]
    near = [s for s in hn if abs(created.timestamp() - s["created_unix"]) <= 86400]
    out["hn_stories_pre24h"] = len(pre) if "hn_stories" in a else None
    out["hn_max_points_pre24h"] = max((s["points"] for s in pre), default=0) if "hn_stories" in a else None
    out["hn_stories_pm24h"] = len(near) if "hn_stories" in a else None
    wiki = a.get("wiki_daily") or {}
    if wiki:
        prev = (created - timedelta(days=1)).strftime("%Y-%m-%d")
        base = [v for d, v in wiki.items() if d < prev and d >= (created - timedelta(days=15)).strftime("%Y-%m-%d")]
        med = _median(base)
        out["wiki_article"] = a.get("wiki_article")
        out["wiki_views_prev_day"] = wiki.get(prev)
        out["wiki_prev_day_ratio"] = round(wiki[prev] / med, 3) if prev in wiki and med else None
    news = a.get("gdelt_15min") or {}
    if news:
        ts = created.timestamp()
        pre_vol = sum(v for t, v in news.items() if 0 < ts - float(t) <= 86400)
        base_days = [v for t, v in news.items() if 86400 < ts - float(t) <= 15 * 86400]
        per_day = sum(base_days) / 14 if base_days else 0
        out["news_articles_pre24h"] = pre_vol
        out["news_pre24h_ratio"] = round(pre_vol / per_day, 3) if per_day else None
    days = ctx.get("days", {}).get(day, {})
    out["hn_big_stories_same_day"] = days.get("hn_stories_500plus")
    out["x_trends_same_day"] = days.get("x_trends_n")
    trends = [t.lower() for t in days.get("x_trends") or []]
    out["_trends_for_overlap"] = trends
    return out


def build(scrape_time: datetime | None = None, cfg: ContextConfig = DEFAULT) -> tuple[list[dict], list[dict]]:
    rows = load_dataset()
    xm = {r["id"]: r for r in read_jsonl(X_METRICS)}
    timelines = read_jsonl(TIMELINES)
    samples = defaultdict(list)
    for r in read_jsonl(REPLY_SAMPLES):
        samples[r["reply_to_post"]].append(r)
    ctx = json.loads(CONTEXT_SIGNALS.read_text(encoding="utf-8")) if CONTEXT_SIGNALS.exists() else {}
    ep = cfg.enrich
    tz = audience_tz(ep.domain)

    created = {r["Post URL"]: snowflake.to_datetime(snowflake.status_id_from_url(r["Post URL"])) for r in rows}
    # David's scrape time is not recorded; the newest post in the file is a lower bound for it.
    scrape = scrape_time or max(created.values())
    window_start = min(created.values()).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = _account_stats(rows, timelines, window_start, scrape + timedelta(seconds=1))

    by_acct_sorted = defaultdict(list)
    for r in rows:
        by_acct_sorted[r["Account"]].append(created[r["Post URL"]])
    for v in by_acct_sorted.values():
        v.sort()
    day_count = defaultdict(int)
    sv_day = defaultdict(set)
    for r in rows:
        d = created[r["Post URL"]].strftime("%Y-%m-%d")
        day_count[d] += 1
        if r["Category"] == "Super Viral":
            sv_day[d].add(r["Account"])

    out_rows = []
    for r in rows:
        url = r["Post URL"]
        c = created[url]
        pid = str(snowflake.status_id_from_url(url))
        acct = r["Account"]
        st = stats[acct]
        likes, replies, rts, eng = (int(r[k]) for k in ("Likes", "Replies", "Retweets", "Engagement"))
        row = {k: r[k] for k in ORIGINAL_COLUMNS}
        row["post_id"] = pid
        row.update(time_features(c, tz))
        row["audience_tz"] = tz

        times = by_acct_sorted[acct]
        i = times.index(c)
        prev_gap = (c - times[i - 1]).total_seconds() if i > 0 else None
        tl_prev = [t for t in st["timeline_times"] if t < c]
        row["seconds_since_prev_post"] = round(prev_gap, 1) if prev_gap is not None else None
        row["seconds_since_prev_post_timeline"] = round((c - tl_prev[-1]).total_seconds(), 1) if tl_prev else None
        row["is_thread_continuation"] = prev_gap is not None and prev_gap < ep.thread_gap_s
        day = c.strftime("%Y-%m-%d")
        row["author_posts_same_day"] = sum(1 for t in times if t.strftime("%Y-%m-%d") == day)
        row["author_post_rank_in_day"] = sum(1 for t in times if t.strftime("%Y-%m-%d") == day and t <= c)
        row["dataset_posts_same_day"] = day_count[day]
        row["other_accounts_super_viral_same_day"] = len(sv_day[day] - {acct})
        row["scrape_time_est"] = scrape.isoformat(timespec="seconds")
        row["age_at_scrape_hours"] = round((scrape - c).total_seconds() / 3600, 2)
        row["label_mature"] = row["age_at_scrape_hours"] >= ep.mature_h

        row.update(text_features(r["Post Content"], r["Media"], ep.domain))
        row["quotes_implied"] = eng - likes - replies - rts  # verified = X's quote count on sampled posts
        row["log_engagement"] = round(math.log1p(eng), 4)

        x = xm.get(pid)
        if x and "error" not in x:
            x_eng = x["likes"] + x["reposts"] + x["replies"] + x["quotes"]
            row.update(
                x_fetched_at=x["fetched_at"],
                age_at_fetch_hours=round((datetime.fromisoformat(x["fetched_at"]) - c).total_seconds() / 3600, 2),
                x_likes=x["likes"], x_reposts=x["reposts"], x_replies=x["replies"], x_quotes=x["quotes"],
                x_bookmarks=x.get("bookmarks"), x_views=x.get("views"),
                author_followers=x.get("author_followers"), author_verified=x.get("author_verified"),
                is_self_reply=bool(x.get("replying_to")) and x["replying_to"].lower() == acct.lstrip("@").lower(),
                is_reply_to_other=bool(x.get("replying_to")) and x["replying_to"].lower() != acct.lstrip("@").lower(),
                is_quote_post=bool(x.get("quoted_id")),
                x_n_links=x.get("n_links"), x_lang=x.get("lang"),
                eng_delta_since_csv=x_eng - eng,
            )
            f = x.get("author_followers")
            v = x.get("views")
            row["eng_per_1k_followers"] = round(eng / f * 1000, 3) if f else None
            row["views_per_follower"] = round(v / f, 4) if f and v else None
            row["eng_per_view"] = round(x_eng / v, 5) if v else None
            row["bookmarks_per_like"] = round(x["bookmarks"] / x["likes"], 4) if x.get("bookmarks") is not None and x["likes"] else None
        elif x:
            row["x_fetched_at"] = x.get("fetched_at")
            row["x_error"] = x["error"][:120]

        med_tl, med_ds = st["median_eng_timeline"], st["median_eng_dataset"]
        use_tl = st["timeline_n"] >= ep.timeline_min_posts and med_tl is not None
        med = med_tl if use_tl else med_ds
        row["author_median_eng_dataset"] = med_ds
        row["author_median_eng_timeline"] = med_tl
        row["author_timeline_n"] = st["timeline_n"]
        row["author_median_source"] = "timeline" if use_tl else "dataset"
        row["eng_ratio_author_median"] = round(eng / max(med, 1), 4)
        row["eng_ratio_author_timeline"] = round(eng / max(med_tl, 1), 4) if med_tl is not None else None
        row["above_author_median"] = (eng > med_tl) if use_tl else None

        terms = [acct.lstrip("@")] + ([ctx.get("accounts", {}).get(acct, {}).get("name")] if ctx else [])
        row.update(_reply_features(samples.get(pid, []), c, terms, cfg))
        if ctx:
            cf = _context_features(ctx, acct, c)
            trends = cf.pop("_trends_for_overlap")
            low = r["Post Content"].lower()
            row.update(cf)
            row["mentions_x_trend_same_day"] = any(t in low for t in trends if len(t) >= 4) if trends else None
        out_rows.append(row)

    cut = boost.ratio_cutoffs([
        {"likes": rr.get("x_likes"), "reposts": rr.get("x_reposts"), "replies": rr.get("x_replies"),
         "bookmarks": rr.get("x_bookmarks"), "views": rr.get("x_views")}
        for rr in out_rows if rr.get("x_likes") is not None
    ], cfg.boost)
    for rr in out_rows:
        rr.update(boost.score(
            {"likes": rr.get("x_likes"), "reposts": rr.get("x_reposts"), "replies": rr.get("x_replies"),
             "bookmarks": rr.get("x_bookmarks"), "views": rr.get("x_views"),
             "eng_ratio_author_timeline": rr.get("eng_ratio_author_timeline"),
             "reply_low_follower_share": rr.get("reply_low_follower_share"),
             "reply_dup_share": rr.get("reply_dup_share"), "reply_burst_share": rr.get("reply_burst_share")},
            cut,
            p=cfg.boost,
        ))

    accounts = []
    for acct, st in sorted(stats.items()):
        xs = [xm.get(rr["post_id"]) for rr in out_rows if rr["Account"] == acct]
        xs = [x for x in xs if x and "error" not in x]
        latest = max(xs, key=lambda x: x["fetched_at"]) if xs else None
        a_ctx = ctx.get("accounts", {}).get(acct, {}) if ctx else {}
        accounts.append({
            "account": acct,
            "author_followers": latest.get("author_followers") if latest else None,
            "author_verified": latest.get("author_verified") if latest else None,
            "followers_fetched_at": latest["fetched_at"] if latest else None,
            "segment": next(rr["Segment"] for rr in out_rows if rr["Account"] == acct),
            "n_dataset": st["n_dataset"], "n_super_viral": st["n_super"],
            "n_medium_viral": st["n_medium"], "n_deadweight": st["n_dead"],
            "median_eng_dataset": st["median_eng_dataset"],
            "median_eng_timeline": st["median_eng_timeline"],
            "timeline_originals_in_window": st["timeline_n"],
            "timeline_posts_per_day": st["timeline_posts_per_day"],
            "name": a_ctx.get("name"),
            "wikipedia_article": a_ctx.get("wiki_article"),
        })
    return out_rows, accounts


def write(rows: list[dict], accounts: list[dict]) -> None:
    cols = list(ORIGINAL_COLUMNS)
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with ENRICHED.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _num(r.get(k)) for k in cols})
    with ACCOUNTS.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(accounts[0]))
        w.writeheader()
        w.writerows(accounts)


def run() -> None:
    rows, accounts = build()
    write(rows, accounts)
    print(f"[enrich] {len(rows)} rows x {len(rows[0])} columns -> {ENRICHED}")
    print(f"[enrich] {len(accounts)} accounts -> {ACCOUNTS}")
