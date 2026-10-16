"""Build the enriched dataset, the restored timeline dataset, and per-account tables.

Outputs (all in data/context/):
- devtools_enriched.csv: David's 1,665 rows with every derived column.
- timeline_posts.csv: every original post on the 91 accounts' timelines in the
  window, including the 11-99 engagement band the CSV left out.
- accounts.csv: one row per account (followers, medians, counts) for joins.
- boost_flags.csv: the boost labels and filter columns keyed by `Post URL`.

Column groups, so a model cannot mix them by accident:
- David's eleven columns first, unchanged and in order, so any loader of the
  original file (such as `harness.dataset.load` on aarav/self-improving) reads
  the enriched file as-is.
- `post_id`, `created_at_utc`: identity.
- `pre_*`: known when the post went out.
- `outcome_*`: measured after posting, mostly at fetch time. `outcome_spillover_*`
  is outside attention after the post, which the post may have caused.
- `label_*`: targets and the filters that decide which rows a label is fair for.
- `boost_*`: suspicion labels (see boost.py).
- Unprefixed because teammates already filter on them: `eng_ratio_author_median`
  (a label), `boost_suspect` and `is_thread_continuation`.
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta

from harness.context import boost, snowflake
from harness.context.backfill import CONTEXT_DIR, TIMELINES, X_METRICS, load_dataset, read_jsonl
from harness.context.features import audience_tz, text_features, time_features
from harness.context.settings import DEFAULT, ContextConfig

ENRICHED = CONTEXT_DIR / "devtools_enriched.csv"
TIMELINE_POSTS = CONTEXT_DIR / "timeline_posts.csv"
ACCOUNTS = CONTEXT_DIR / "accounts.csv"
BOOST_FLAGS = CONTEXT_DIR / "boost_flags.csv"
REPLY_SAMPLES = CONTEXT_DIR / "reply_samples.jsonl"
REPOSTERS = CONTEXT_DIR / "reposters.jsonl"
CONTEXT_SIGNALS = CONTEXT_DIR / "context_signals.json"
WINDOW_START = "2026-09-15T00:00:00+00:00"  # first day in David's CSV
ORIGINAL_COLUMNS = [
    "Category", "Account", "Post Content", "Media", "Likes", "Replies",
    "Retweets", "Engagement", "Post URL", "Date", "Segment",
]
BOOST_COLUMNS = [
    "Post URL", "post_id", "Account", "Category", "boost_suspect", "boost_paid_or_external_reach",
    "boost_coordinated_authentic", "boost_likely_inauthentic", "boost_class", "boost_inauthentic_votes",
    "boost_reasons", "boost_weak_signals", "is_thread_continuation", "label_mature", "label_eligible",
]


def category(engagement: int) -> str:
    """David's thresholds, plus the 11-99 band his file left out."""
    if engagement <= 10:
        return "Deadweight"
    if engagement < 100:
        return "Low"
    if engagement < 750:
        return "Medium Viral"
    return "Super Viral"


def _median(values):
    return statistics.median(values) if values else None


def _media(types: list[str]) -> str:
    kinds = set(types or [])
    if not kinds:
        return "none"
    return next(iter(kinds)) if len(kinds) == 1 else "photo/video"


def _eng(x: dict) -> int:
    return x["likes"] + x["reposts"] + x["replies"] + x["quotes"]


def _is_self_reply(x: dict | None, handle: str) -> bool:
    return bool(x and x.get("replying_to")) and x["replying_to"].lower() == handle.lstrip("@").lower()


class History:
    """One account's own original posts (no reposts, no thread continuations), oldest first."""

    def __init__(self, posts: list[dict]):
        self.posts = sorted(posts, key=lambda p: p["created"])
        self.times = [p["created"] for p in self.posts]

    def before(self, when: datetime) -> list[dict]:
        return self.posts[: bisect_left(self.times, when)]

    def in_window(self, start: datetime, end: datetime) -> list[dict]:
        return [p for p in self.posts if start <= p["created"] < end]


def _gap_continuations(rows: list[dict], gap_s: float) -> set[str]:
    """Post URLs within `gap_s` of the same account's previous CSV post: the fallback when X data is missing."""
    by = defaultdict(list)
    for r in rows:
        by[r["Account"]].append((snowflake.to_datetime(snowflake.status_id_from_url(r["Post URL"])), r["Post URL"]))
    out = set()
    for lst in by.values():
        lst.sort()
        out |= {url for (t0, _), (t1, url) in zip(lst, lst[1:]) if (t1 - t0).total_seconds() < gap_s}
    return out


def _histories(rows, xm, timelines, gap_urls) -> tuple[dict[str, History], dict[str, list[datetime]]]:
    """Per account: originals for baselines, and every own post time (continuations included) for cadence."""
    originals: dict[str, dict[str, dict]] = defaultdict(dict)
    all_times: dict[str, set] = defaultdict(set)
    for t in timelines:
        if "error" in t or t.get("is_repost"):
            continue
        acct = "@" + t["timeline_of"].lower()
        created = datetime.fromisoformat(t["created_at"])
        all_times[acct].add(created)
        if not _is_self_reply(t, t["timeline_of"]):
            originals[acct][t["id"]] = {"id": t["id"], "created": created, "eng": _eng(t), "views": t.get("views")}
    for r in rows:
        pid = str(snowflake.status_id_from_url(r["Post URL"]))
        acct = r["Account"].lower()
        created = snowflake.to_datetime(pid)
        all_times[acct].add(created)
        x = xm.get(pid)
        has_x = bool(x) and "error" not in x
        if pid in originals[acct] or (has_x and _is_self_reply(x, acct)) or (not has_x and r["Post URL"] in gap_urls):
            continue
        eng, views = (_eng(x), x.get("views")) if has_x else (int(r["Engagement"]), None)
        originals[acct][pid] = {"id": pid, "created": created, "eng": eng, "views": views}
    return (
        {a: History(list(p.values())) for a, p in originals.items()},
        {a: sorted(t) for a, t in all_times.items()},
    )


def _names_brand(text: str | None, terms: list[str]) -> bool:
    """Whole-word match, so "Zed" does not match "organized" and "Bun" does not match "Ubuntu"."""
    low = (text or "").lower()
    return any(re.search(rf"(?<![\w@]){re.escape(t.lower())}(?!\w)", low) for t in terms if t and len(t) >= 3)


def _reply_block(sample: list[dict], created: datetime, terms: list[str], cfg: ContextConfig) -> dict:
    if not sample:
        return {}
    replies = [r for r in sample if "id" in r]
    cov = next((r.get("coverage") for r in sample if r.get("coverage") is not None), None)
    out = {"outcome_reply_coverage": round(cov, 3) if cov is not None else None, "outcome_reply_sample_n": len(replies)}
    if not replies:
        return out
    sig = boost.reply_signals(
        [{"text": r.get("text"), "author_followers": r.get("author_followers"),
          "created_unix": datetime.fromisoformat(r["created_at"]).timestamp()} for r in replies],
        cfg.boost,
    )
    covered = cov is not None and cov >= cfg.enrich.min_reply_coverage
    delays = sorted((datetime.fromisoformat(r["created_at"]) - created).total_seconds() / 60 for r in replies)
    insiders = [r for r in replies if _names_brand(r.get("author_description"), terms)]
    out.update(
        outcome_reply_low_follower_share=sig.get("reply_low_follower_share"),
        outcome_reply_dup_share=sig.get("reply_dup_share"),
        # Timing statistics only on near-complete samples: the likes-ranked page favours early replies.
        outcome_reply_burst_share=sig.get("reply_burst_share") if covered else None,
        outcome_reply_median_delay_min=round(statistics.median(delays), 1) if covered else None,
        outcome_t_to_reply_1_min=round(delays[0], 1) if covered else None,
        outcome_t_to_reply_5_min=round(delays[4], 1) if covered and len(delays) >= 5 else None,
        outcome_t_to_reply_10_min=round(delays[9], 1) if covered and len(delays) >= 10 else None,
        outcome_reply_big_accounts=sum(1 for r in replies if (r.get("author_followers") or 0) >= cfg.enrich.big_account),
        outcome_reply_max_follower=max((r.get("author_followers") or 0) for r in replies),
        outcome_reply_insider_share=round(len(insiders) / len(replies), 3),
    )
    return out


def _repost_block(users: list[dict], created: datetime, terms: list[str], cfg: ContextConfig) -> dict:
    users = [u for u in users if u.get("handle")]
    if not users:
        return {}
    total = users[0].get("reposts") or 0
    followers = sorted((u.get("followers") or 0 for u in users), reverse=True)
    staff = [u for u in users if _names_brand(u.get("description"), terms)]
    complete = total > 0 and len(users) >= 0.9 * total
    new = [
        u for u in users
        if u.get("joined") and (created - datetime.fromisoformat(u["joined"])).days < cfg.enrich.new_account_days
    ]
    return {
        "outcome_reposters_sampled": len(users),
        "outcome_reposters_complete": complete,
        "outcome_max_reposter_followers": followers[0],
        "outcome_top10_reposter_followers_sum": sum(followers[:10]),
        "outcome_reposter_staff_share": round(len(staff) / len(users), 3),
        # New-account share needs the whole tail; the largest-first order hides small new accounts.
        "outcome_reposter_new_account_share": round(len(new) / len(users), 3) if complete else None,
    }


def _context_block(ctx: dict, acct: str, created: datetime) -> dict:
    """Outside attention strictly before the post (pre_*) and after it (outcome_spillover_*)."""
    a = ctx.get("accounts", {}).get(acct)
    out: dict = {}
    ts = created.timestamp()
    prev = (created - timedelta(days=1)).strftime("%Y-%m-%d")
    lo = (created - timedelta(days=15)).strftime("%Y-%m-%d")
    if a and "hn_stories" in a:
        hn = a["hn_stories"]
        before = [s for s in hn if 0 < ts - s["created_unix"] <= 86400]
        after = [s for s in hn if 0 <= s["created_unix"] - ts <= 86400]
        out["pre_hn_stories_prev24h"] = len(before)
        # Points are read at fetch time, so a story posted a minute earlier carries points it earned later.
        out["outcome_hn_max_points_prev24h"] = max((s["points"] for s in before), default=0)
        out["outcome_spillover_hn_stories_next24h"] = len(after)
        out["outcome_spillover_hn_max_points_next24h"] = max((s["points"] for s in after), default=0)
    wiki = (a or {}).get("wiki_daily") or {}
    if wiki:
        base = _median([v for d, v in wiki.items() if lo <= d < prev])
        day = created.strftime("%Y-%m-%d")
        out["pre_wiki_article"] = a.get("wiki_article")
        out["pre_wiki_views_prev_day"] = wiki.get(prev)
        out["pre_wiki_prev_day_ratio"] = round(wiki[prev] / base, 3) if prev in wiki and base else None
        out["outcome_spillover_wiki_same_day_ratio"] = round(wiki[day] / base, 3) if day in wiki and base else None
    news = (a or {}).get("gdelt_daily") or {}
    if news:
        base = _median([v for d, v in news.items() if lo <= d < prev])
        out["pre_news_articles_prev_day"] = news.get(prev)
        out["pre_news_prev_day_ratio"] = round(news[prev] / base, 3) if prev in news and base else None
    days = ctx.get("days", {})
    two = [days.get(d.strftime("%Y-%m-%d"), {}) for d in (created - timedelta(days=1), created)]
    if days:
        big = [t for d in two for t in d.get("hn_500plus_unix") or []]
        # "500+ points" is judged at fetch time: outcome, not pre.
        out["outcome_hn_500plus_stories_prev24h"] = sum(1 for t in big if 0 < ts - t <= 86400)
    trends = [
        name
        for d in two
        for key in ("x_trend_lists_united-states", "x_trend_lists_worldwide")
        for at, names in (d.get(key) or {}).items()
        if 0 < ts - datetime.fromisoformat(at).timestamp() <= 86400
        for name in names
    ]
    out["pre_x_trend_lists_available"] = bool(trends)
    out["_trends"] = trends
    return out


def featurize(post: dict, acct: str, hist: History, own_times: list[datetime], cfg: ContextConfig, ctx: dict) -> dict:
    """Columns shared by CSV rows and timeline posts.

    `post`: id, created, text, media (CSV-style string), engagement, likes,
    replies, reposts, quotes, and `x` (the fetch-time XPost dict or None).
    """
    ep = cfg.enrich
    c = post["created"]
    tz = audience_tz(ep.domain)
    row = {"post_id": post["id"], "created_at_utc": c.isoformat(timespec="seconds")}
    row.update({f"pre_{k}": v for k, v in time_features(c, tz).items() if k != "created_at_utc"})
    row["pre_audience_tz"] = tz
    row.update({f"pre_{k}": v for k, v in text_features(post["text"], post["media"], ep.domain).items()})

    prev = [t for t in own_times if t < c]
    row["pre_seconds_since_prev_own_post"] = round((c - prev[-1]).total_seconds(), 1) if prev else None
    row[f"pre_author_posts_prev{ep.prev_window_h:g}h"] = sum(
        1 for t in prev if (c - t).total_seconds() <= ep.prev_window_h * 3600
    )

    prior = hist.before(c - timedelta(hours=ep.prior_gap_h))
    prior_eng = [p["eng"] for p in prior]
    prior_views = [p["views"] for p in prior if p.get("views")]
    enough = len(prior_eng) >= ep.prior_min_posts
    row["pre_author_prior_n"] = len(prior_eng)
    row["pre_author_prior_median_eng"] = _median(prior_eng) if enough else None
    row["pre_author_prior_median_views"] = _median(prior_views) if len(prior_views) >= ep.prior_min_posts else None
    row["pre_author_prior_viral_share"] = round(sum(e >= 100 for e in prior_eng) / len(prior_eng), 3) if enough else None

    x = post.get("x")
    has_x = bool(x) and "error" not in x
    if has_x:
        self_reply = _is_self_reply(x, acct)
        row["pre_is_self_reply"] = self_reply
        row["pre_is_reply_to_other"] = bool(x.get("replying_to")) and not self_reply
        row["pre_is_quote_post"] = bool(x.get("quoted_id"))
        # Fetch-time values. A post that went viral grew the follower count, so these are outcomes;
        # use accounts.csv for an account-level join.
        row["outcome_author_followers_at_fetch"] = x.get("author_followers")
        row["outcome_author_verified"] = x.get("author_verified")

    eng = post["engagement"]
    w = ep.weights
    row["outcome_quotes"] = post["quotes"]
    row["outcome_x_weighted_engagement"] = round(
        w[0] * post["likes"] + w[1] * post["reposts"] + w[2] * post["replies"] + w[3] * post["quotes"], 2
    )
    row["outcome_log_engagement"] = round(math.log1p(eng), 4)
    if has_x:
        x_eng = _eng(x)
        f, v = x.get("author_followers"), x.get("views")
        row.update(
            outcome_x_fetched_at=x["fetched_at"],
            outcome_age_at_fetch_hours=round((datetime.fromisoformat(x["fetched_at"]) - c).total_seconds() / 3600, 2),
            outcome_x_likes=x["likes"], outcome_x_reposts=x["reposts"], outcome_x_replies=x["replies"],
            outcome_x_quotes=x["quotes"], outcome_x_bookmarks=x.get("bookmarks"), outcome_x_views=v,
            outcome_eng_delta_since_csv=x_eng - eng,
            outcome_eng_per_1k_followers=round(eng / f * 1000, 3) if f else None,
            outcome_views_per_follower=round(v / f, 4) if f and v else None,
            outcome_eng_per_view=round(x_eng / v, 5) if v else None,
            outcome_bookmarks_per_like=round(x["bookmarks"] / x["likes"], 4)
            if x.get("bookmarks") is not None and x["likes"] else None,
        )
    elif x:
        row["outcome_x_error"] = x["error"][:120]

    if enough:
        row["label_lift_prior"] = round(math.log1p(eng) - math.log1p(row["pre_author_prior_median_eng"]), 4)
        row["label_above_author_prior"] = eng > row["pre_author_prior_median_eng"]
    row["label_category_x"] = category(_eng(x)) if has_x else None

    if ctx:
        cb = _context_block(ctx, acct, c)
        trends = cb.pop("_trends")
        row.update(cb)
        low = post["text"].lower()
        row["pre_mentions_x_trend_prev24h"] = any(t.lower() in low for t in trends if len(t) >= 4) if trends else None
    return row


def _window_label(row: dict, eng: int, med: float | None, source: str) -> None:
    if med is None:
        return
    row["label_author_median_source"] = source
    row["label_author_window_median_eng"] = med
    row["eng_ratio_author_median"] = round(eng / max(med, 1), 4)
    row["label_lift_window"] = round(math.log1p(eng) - math.log1p(med), 4)
    row["label_above_author_median"] = eng > med


def build(cfg: ContextConfig = DEFAULT) -> tuple[list[dict], list[dict], list[dict]]:
    ep = cfg.enrich
    rows = load_dataset()
    xm = {r["id"]: r for r in read_jsonl(X_METRICS)}
    timelines = read_jsonl(TIMELINES)
    samples, reposters = defaultdict(list), defaultdict(list)
    for r in read_jsonl(REPLY_SAMPLES):
        samples[r["reply_to_post"]].append(r)
    for r in read_jsonl(REPOSTERS):
        reposters[r["post_id"]].append(r)
    ctx = json.loads(CONTEXT_SIGNALS.read_text(encoding="utf-8")) if CONTEXT_SIGNALS.exists() else {}
    names = {a: v.get("name") for a, v in ctx.get("accounts", {}).items()}
    window = (datetime.fromisoformat(WINDOW_START), datetime.fromisoformat(ep.scrape_time_iso))
    gap_urls = _gap_continuations(rows, ep.thread_gap_s)
    hists, own_times = _histories(rows, xm, timelines, gap_urls)

    out_rows = []
    for r in rows:
        pid = str(snowflake.status_id_from_url(r["Post URL"]))
        acct, key = r["Account"], r["Account"].lower()
        likes, replies, rts, eng = (int(r[k]) for k in ("Likes", "Replies", "Retweets", "Engagement"))
        x = xm.get(pid)
        post = {"id": pid, "created": snowflake.to_datetime(pid), "text": r["Post Content"], "media": r["Media"],
                "engagement": eng, "likes": likes, "replies": replies, "reposts": rts,
                "quotes": eng - likes - replies - rts, "x": x}
        hist = hists.get(key, History([]))
        row = {k: r[k] for k in ORIGINAL_COLUMNS}
        row.update(featurize(post, acct, hist, own_times.get(key, []), cfg, ctx))
        has_x = bool(x) and "error" not in x
        cont = row["pre_is_self_reply"] if has_x else r["Post URL"] in gap_urls
        row["is_thread_continuation"] = cont
        row["label_continuation_source"] = "x_replying_to" if has_x else "gap_under_180s"
        row["label_age_at_scrape_hours"] = round((window[1] - post["created"]).total_seconds() / 3600, 2)
        row["label_mature"] = row["label_age_at_scrape_hours"] >= ep.mature_h
        row["label_eligible"] = row["label_mature"] and not cont

        win = [p["eng"] for p in hist.in_window(*window)]
        if len(win) >= ep.timeline_min_posts:
            _window_label(row, eng, _median(win), "timeline")
        else:
            csv_eng = [int(o["Engagement"]) for o in rows if o["Account"] == acct and o["Post URL"] not in gap_urls]
            _window_label(row, eng, _median(csv_eng), "csv")

        terms = [acct.lstrip("@"), names.get(acct) or ""]
        row.update(_reply_block(samples.get(pid, []), post["created"], terms, cfg))
        row.update(_repost_block(reposters.get(pid, []), post["created"], terms, cfg))
        out_rows.append(row)

    _add_boost(out_rows, hists, cfg)
    timeline_rows = _timeline_rows(timelines, hists, own_times, rows, cfg, ctx, window)
    accounts = _accounts(out_rows, hists, window, ctx)
    return out_rows, timeline_rows, accounts


def _add_boost(out_rows: list[dict], hists: dict[str, History], cfg: ContextConfig) -> None:
    def view_stats(acct):
        posts = [p for p in hists.get(acct.lower(), History([])).posts if p.get("views")]
        return _median([p["views"] for p in posts]), _median([p["eng"] / p["views"] for p in posts]), len(posts)

    stats = {a: view_stats(a) for a in {r["Account"] for r in out_rows}}
    inputs = []
    for r in out_rows:
        mv, mepv, n = stats[r["Account"]]
        has_x = r.get("outcome_x_likes") is not None
        inputs.append({
            "likes": r.get("outcome_x_likes"), "reposts": r.get("outcome_x_reposts"),
            "replies": r.get("outcome_x_replies"), "bookmarks": r.get("outcome_x_bookmarks"),
            "views": r.get("outcome_x_views"),
            "engagement": sum(r[k] for k in ("outcome_x_likes", "outcome_x_reposts", "outcome_x_replies", "outcome_x_quotes"))
            if has_x else None,
            "author_median_views": mv, "author_median_eng_per_view": mepv, "author_posts_with_views": n,
            "eng_ratio_author": r.get("eng_ratio_author_median"),
            "reply_coverage": r.get("outcome_reply_coverage"),
            "reply_low_follower_share": r.get("outcome_reply_low_follower_share"),
            "reply_dup_share": r.get("outcome_reply_dup_share"),
            "reply_burst_share": r.get("outcome_reply_burst_share"),
            "reposter_staff_share": r.get("outcome_reposter_staff_share"),
            "reposter_sample_n": r.get("outcome_reposters_sampled"),
            "reply_insider_share": r.get("outcome_reply_insider_share"),
            "reply_sample_n": r.get("outcome_reply_sample_n"),
            "reposter_new_account_share": r.get("outcome_reposter_new_account_share"),
        })
    cut = boost.ratio_cutoffs([i for i in inputs if i["likes"] is not None], cfg.boost)
    for r, i in zip(out_rows, inputs):
        r.update(boost.classify(i, cut, cfg.boost))


def _timeline_rows(timelines, hists, own_times, rows, cfg, ctx, window) -> list[dict]:
    """Every original timeline post in the window, featurized the same way, with recomputed labels."""
    in_csv = {str(snowflake.status_id_from_url(r["Post URL"])) for r in rows}
    seen, out = set(), []
    for t in timelines:
        if "error" in t or t.get("is_repost") or t["id"] in seen:
            continue
        created = datetime.fromisoformat(t["created_at"])
        if not window[0] <= created < window[1]:
            continue
        seen.add(t["id"])
        acct, key = "@" + t["timeline_of"], "@" + t["timeline_of"].lower()
        eng = _eng(t)
        post = {"id": t["id"], "created": created, "text": t.get("text") or "", "media": _media(t.get("media_types")),
                "engagement": eng, "likes": t["likes"], "replies": t["replies"], "reposts": t["reposts"],
                "quotes": t["quotes"], "x": t}
        hist = hists.get(key, History([]))
        row = {"Account": acct, "Post URL": f"https://x.com/{t['author_handle']}/status/{t['id']}", "in_csv": t["id"] in in_csv}
        row.update(featurize(post, acct, hist, own_times.get(key, []), cfg, ctx))
        row["is_thread_continuation"] = row["pre_is_self_reply"]
        row["label_mature"] = row["outcome_age_at_fetch_hours"] >= cfg.enrich.mature_h
        row["label_eligible"] = row["label_mature"] and not row["is_thread_continuation"]
        _window_label(row, eng, _median([p["eng"] for p in hist.in_window(*window)]), "timeline")
        out.append(row)
    return out


def _accounts(out_rows, hists, window, ctx) -> list[dict]:
    by = defaultdict(list)
    for r in out_rows:
        by[r["Account"]].append(r)
    accounts = []
    for acct, rs in sorted(by.items()):
        win = hists.get(acct.lower(), History([])).in_window(*window)
        fetched = [r for r in rs if r.get("outcome_x_fetched_at")]
        latest = max(fetched, key=lambda r: r["outcome_x_fetched_at"]) if fetched else None
        a_ctx = ctx.get("accounts", {}).get(acct, {})
        accounts.append({
            "account": acct,
            "author_followers": latest.get("outcome_author_followers_at_fetch") if latest else None,
            "author_verified": latest.get("outcome_author_verified") if latest else None,
            "followers_fetched_at": latest["outcome_x_fetched_at"] if latest else None,
            "segment": rs[0]["Segment"],
            "name": a_ctx.get("name"),
            "wikipedia_article": a_ctx.get("wiki_article"),
            "n_csv": len(rs),
            "n_csv_super_viral": sum(r["Category"] == "Super Viral" for r in rs),
            "n_csv_medium_viral": sum(r["Category"] == "Medium Viral" for r in rs),
            "n_csv_deadweight": sum(r["Category"] == "Deadweight" for r in rs),
            "n_csv_thread_continuations": sum(bool(r["is_thread_continuation"]) for r in rs),
            "timeline_originals_in_window": len(win),
            "timeline_median_eng": _median([p["eng"] for p in win]),
            "timeline_median_views": _median([p["views"] for p in win if p.get("views")]),
            "timeline_share_11_99": round(sum(11 <= p["eng"] <= 99 for p in win) / len(win), 3) if win else None,
            "timeline_posts_per_day": round(len(win) / max((window[1] - window[0]).days, 1), 2) if win else None,
        })
    return accounts


def _write(path, rows: list[dict], first: list[str] | None = None) -> None:
    cols = list(first or [])
    for r in rows:
        cols += [k for k in r if k not in cols]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: "" if r.get(k) is None else r.get(k) for k in cols})


def run(cfg: ContextConfig = DEFAULT) -> None:
    rows, timeline_rows, accounts = build(cfg)
    _write(ENRICHED, rows, ORIGINAL_COLUMNS)
    _write(BOOST_FLAGS, [{k: r.get(k) for k in BOOST_COLUMNS} for r in rows], BOOST_COLUMNS)
    _write(ACCOUNTS, accounts)
    if timeline_rows:
        _write(TIMELINE_POSTS, timeline_rows, ["Account", "Post URL", "in_csv"])
    print(f"[enrich] {len(rows)} rows x {len(set().union(*rows))} columns -> {ENRICHED}")
    print(f"[enrich] boost flags -> {BOOST_FLAGS}; {len(accounts)} accounts -> {ACCOUNTS}")
    print(f"[enrich] {len(timeline_rows)} timeline posts -> {TIMELINE_POSTS}")
