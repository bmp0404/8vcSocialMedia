"""Collect context for the dataset: outside attention per account, reply samples and reposters.

- `context`: writes data/context/context_signals.json with, per account, its
  display name, Wikipedia article and daily views, Hacker News stories whose
  title names it, and GDELT daily news volume (best effort); and per day, the
  count of 500+ point HN stories and the X trends from any Wayback capture.
- `replies`: writes data/context/reply_samples.jsonl, replies merged from both
  sort orders with each post's coverage.
- `reposters`: writes data/context/reposters.jsonl, the largest reposters.

Every job appends or caches, so a rerun continues instead of starting over.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from harness.context import snowflake
from harness.context.backfill import CONTEXT_DIR, X_METRICS, load_dataset, read_jsonl
from harness.context.http import CACHE_DIR, FetchError, PoliteClient
from harness.context.settings import DEFAULT
from harness.context.xreader import FxTwitterReader

CONTEXT_SIGNALS = CONTEXT_DIR / "context_signals.json"
REPLY_SAMPLES = CONTEXT_DIR / "reply_samples.jsonl"
REPOSTERS = CONTEXT_DIR / "reposters.jsonl"
WINDOW = (datetime(2026, 8, 31, tzinfo=timezone.utc), datetime(2026, 10, 17, tzinfo=timezone.utc))


def account_names() -> dict[str, str]:
    """{'@handle': display name} from the cached fxtwitter status responses."""
    names = {}
    for f in (CACHE_DIR / "api.fxtwitter.com").glob("*.json"):
        try:
            body = json.loads(json.loads(f.read_text(encoding="utf-8"))["body"])
        except (json.JSONDecodeError, KeyError):
            continue
        author = (body.get("tweet") or {}).get("author") or {}
        if author.get("screen_name") and author.get("name"):
            names.setdefault("@" + author["screen_name"].lower(), author["name"])
    return names


def collect_context(gdelt_budget: int = 0) -> dict:
    from harness.context.collectors import gdelt, hackernews, wikipedia, x_trends

    rows = load_dataset()
    accounts = sorted({r["Account"] for r in rows})
    names = account_names()
    out = json.loads(CONTEXT_SIGNALS.read_text(encoding="utf-8")) if CONTEXT_SIGNALS.exists() else {}
    acc = out.setdefault("accounts", {})
    days = out.setdefault("days", {})
    errors = out.setdefault("errors", [])
    start, end = WINDOW

    wiki = {}
    try:
        wiki = wikipedia.articles_for_handles(accounts)
    except FetchError as e:
        errors.append(f"wikidata: {e}")

    gdelt_failures = 0
    for a in accounts:
        rec = acc.setdefault(a, {})
        rec["name"] = names.get(a.lower(), a.lstrip("@"))
        hit = wiki.get(a.lstrip("@").lower())
        rec["wiki_article"] = hit["article"] if hit else None
        if rec["wiki_article"] and "wiki_daily" not in rec:
            try:
                s = wikipedia.pageviews(rec["wiki_article"], start, end - timedelta(days=1))
                rec["wiki_daily"] = {ts.strftime("%Y-%m-%d"): v for ts, v in s.points}
            except FetchError as e:
                errors.append(f"wikipedia {a}: {e}")
        if "hn_stories" not in rec:
            try:
                st = hackernews.stories(rec["name"], start, end, title_only=True)
                rec["hn_stories"] = [
                    {"id": s.id, "title": s.title, "points": s.points, "created_unix": int(s.created_at.timestamp())}
                    for s in st
                ]
            except FetchError as e:
                errors.append(f"hackernews {a}: {e}")
        if "gdelt_daily" not in rec and gdelt_budget > 0 and gdelt_failures < 3:
            gdelt_budget -= 1
            try:
                s = gdelt.news_volume(f'"{rec["name"]}"', start, end)
                rec["gdelt_daily"] = {ts.strftime("%Y-%m-%d"): v for ts, v in s.points}
                gdelt_failures = 0
            except FetchError as e:
                gdelt_failures += 1
                errors.append(f"gdelt {a}: {str(e)[:160]}")
        _save(out)

    d = start
    while d < end:
        key = d.strftime("%Y-%m-%d")
        if "hn_500plus_unix" not in days.get(key, {}):
            try:
                big = hackernews.big_stories(d, 500)
                days.setdefault(key, {})["hn_500plus_unix"] = sorted(int(s.created_at.timestamp()) for s in big)
            except FetchError as e:
                errors.append(f"hackernews big {key}: {e}")
        d += timedelta(days=1)
    _save(out)

    for country in ("united-states", "worldwide"):
        try:
            caps = x_trends.wayback_captures(country, start, end)
        except FetchError as e:
            errors.append(f"wayback cdx {country}: {e}")
            continue
        for ts in caps:
            try:
                lists = x_trends.wayback_lists(country, ts)
            except FetchError as e:
                errors.append(f"wayback {country} {ts}: {e}")
                continue
            for tl in lists:
                day = days.setdefault(tl.at.strftime("%Y-%m-%d"), {})
                lst = day.setdefault(f"x_trend_lists_{country}", {})
                lst[tl.at.isoformat(timespec="minutes")] = tl.trends
        _save(out)
    return out


def _save(out: dict) -> None:
    CONTEXT_SIGNALS.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _targets(seed: int = 7, mv_sample: int = 150, dw_sample: int = 100) -> list[dict]:
    """All Super Viral posts, a sample of Medium Viral, and a sample of Deadweight posts that have replies."""
    xm = {r["id"]: r for r in read_jsonl(X_METRICS) if "error" not in r}
    rows = load_dataset()
    for r in rows:
        r["_id"] = str(snowflake.status_id_from_url(r["Post URL"]))
    own = lambda r: not (xm.get(r["_id"], {}).get("replying_to") or "")  # noqa: E731 - skip thread continuations
    sv = [r for r in rows if r["Category"] == "Super Viral"]
    mv = [r for r in rows if r["Category"] == "Medium Viral" and own(r)]
    dw = [r for r in rows if r["Category"] == "Deadweight" and own(r) and xm.get(r["_id"], {}).get("replies", 0) >= 1]
    rng = random.Random(seed)
    return sv + rng.sample(mv, min(mv_sample, len(mv))) + rng.sample(dw, min(dw_sample, len(dw)))


def collect_replies(interval_s: float = DEFAULT.fetch.bulk_interval_s) -> int:
    reader = FxTwitterReader(PoliteClient(min_interval_s=interval_s), max_age_s=None)
    done = {r["reply_to_post"] for r in read_jsonl(REPLY_SAMPLES) if "error" not in r}  # failures are retried
    n = 0
    with REPLY_SAMPLES.open("a", encoding="utf-8") as f:
        for r in _targets():
            pid = r["_id"]
            if pid in done:
                continue
            try:
                sample = reader.replies(pid)
            except FetchError as e:
                f.write(json.dumps({"reply_to_post": pid, "error": str(e)[:200]}) + "\n")
                continue
            meta = {"reply_to_post": pid, "coverage": sample.coverage, "reply_count": sample.reply_count}
            if not sample.replies:
                f.write(json.dumps({**meta, "empty": True}) + "\n")
            for rep in sample.replies:
                f.write(json.dumps({**meta, **rep.to_dict()}) + "\n")
            f.flush()
            n += 1
            if n % 25 == 0:
                print(f"[collect] replies for {n} posts")
    return n


def collect_reposters(max_pages: int = 3, interval_s: float = DEFAULT.fetch.bulk_interval_s) -> int:
    reader = FxTwitterReader(PoliteClient(min_interval_s=interval_s), max_age_s=None)
    xm = {r["id"]: r for r in read_jsonl(X_METRICS) if "error" not in r}
    done = {r["post_id"] for r in read_jsonl(REPOSTERS) if "error" not in r}
    n = 0
    with REPOSTERS.open("a", encoding="utf-8") as f:
        for r in _targets():
            pid = r["_id"]
            reposts = xm.get(pid, {}).get("reposts", 0)
            if pid in done or reposts == 0:
                continue
            pages = max_pages if r["Category"] == "Super Viral" else 1
            try:
                users = reader.reposters(pid, max_pages=pages)
            except FetchError as e:
                f.write(json.dumps({"post_id": pid, "error": str(e)[:200]}) + "\n")
                continue
            for u in users:
                f.write(json.dumps({"post_id": pid, "reposts": reposts, **u.to_dict()}) + "\n")
            if not users:
                f.write(json.dumps({"post_id": pid, "reposts": reposts, "empty": True}) + "\n")
            f.flush()
            n += 1
            if n % 25 == 0:
                print(f"[collect] reposters for {n} posts")
    return n
