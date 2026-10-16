"""Fetch one real data point from every keyless source and record the result.

`python -m harness.context check` writes data/context/live_checks.json: per
source, OK or the error, and one concrete value, so a reader can see which
sources worked on which day without rerunning anything.
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta, timezone

from harness.context.backfill import CONTEXT_DIR

OUT = CONTEXT_DIR / "live_checks.json"


def _checks():
    from harness.context.collectors import espn, gdelt, google_trends, hackernews, wikipedia, x_trends
    from harness.context.xreader import FxTwitterReader

    now = datetime.now(timezone.utc)
    d = lambda y, m, dd: datetime(y, m, dd, tzinfo=timezone.utc)  # noqa: E731

    def fx():
        p = FxTwitterReader(max_age_s=None).status("2100570682593546310")
        return f"@{p.author_handle} {p.created_at:%Y-%m-%dT%H:%M}Z likes={p.likes} views={p.views} followers={p.author_followers} (as of {p.fetched_at})"

    def wiki_daily():
        s = wikipedia.pageviews("Caitlin_Clark", d(2026, 9, 18), d(2026, 9, 22))
        ts, v = s.points[2]
        return f"Caitlin_Clark {ts:%Y-%m-%d}: {int(v)} views"

    def wiki_hourly():
        hour = (now - timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
        c = wikipedia.hourly_from_dump(["Caitlin_Clark"], hour)
        return f"Caitlin_Clark {hour:%Y-%m-%dT%H}Z: {c['Caitlin_Clark']} views (dump file)"

    def wikidata():
        m = wikipedia.articles_for_handles(["deno_land", "CaitlinClark22"])
        return f"@deno_land -> {m['deno_land']['article']}; @CaitlinClark22 -> {m['caitlinclark22']['article']}"

    def hn():
        st = hackernews.stories("supabase", d(2026, 9, 15), d(2026, 10, 15))
        top = max(st, key=lambda s: s.points)
        return f"{len(st)} 'supabase' stories 09-15..10-15; top: {top.title!r} {top.points} pts {top.created_at:%Y-%m-%d}"

    def gtrends_rss():
        t = google_trends.trending_now("US")[0]
        return f"US trending now: {t.term!r} {t.approx_traffic} at {t.published:%Y-%m-%dT%H:%M}Z"

    def gtrends_iot():
        s = google_trends.interest_over_time("Caitlin Clark", d(2026, 9, 1), d(2026, 10, 14))
        ts, v = max(s.points, key=lambda p: p[1])
        return f"'Caitlin Clark' US daily 09-01..10-14: {len(s.points)} points, peak {int(v)} on {ts:%Y-%m-%d}"

    def x_trends_live():
        lists = x_trends.live("united-states")
        return f"{len(lists)} hourly lists; newest {lists[-1].at:%Y-%m-%dT%H:%M}Z #1 {lists[-1].trends[0]!r}"

    def x_trends_wayback():
        caps = x_trends.wayback_captures("united-states", d(2026, 9, 25), d(2026, 9, 26))
        lists = x_trends.wayback_lists("united-states", caps[0])
        return f"capture {caps[0]}: {len(lists)} lists; {lists[0].at:%Y-%m-%dT%H:%M}Z #1 {lists[0].trends[0]!r}"

    def gdelt_vol():
        s = gdelt.news_volume('"Caitlin Clark"', d(2026, 9, 20), d(2026, 9, 22))
        total = sum(v for _, v in s.points)
        return f"'Caitlin Clark' 09-20..09-22: {len(s.points)} {s.resolution} buckets, {int(total)} articles"

    def espn_game():
        g = espn.with_wallclock(next(g for g in espn.scoreboard(d(2026, 9, 20)) if g.completed))
        return f"{g.name} {g.away_score}-{g.home_score}: first play {g.started:%H:%M}Z, last play {g.ended:%H:%M}Z"

    return {
        "fxtwitter_status": fx,
        "wikipedia_daily": wiki_daily,
        "wikipedia_hourly_dump": wiki_hourly,
        "wikidata_handle_lookup": wikidata,
        "hackernews_algolia": hn,
        "google_trends_rss": gtrends_rss,
        "google_trends_interest": gtrends_iot,
        "x_trends_trends24_live": x_trends_live,
        "x_trends_wayback": x_trends_wayback,
        "gdelt_timeline": gdelt_vol,
        "espn_wallclock": espn_game,
    }


def run(only: list[str] | None = None) -> dict:
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for name, fn in _checks().items():
        if only and name not in only:
            continue
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            results[name] = {"ok": True, "checked_at": at, "value": fn()}
        except Exception as e:  # noqa: BLE001 - record every failure mode, keep checking
            results[name] = {"ok": False, "checked_at": at, "error": f"{type(e).__name__}: {e}"[:300],
                             "where": traceback.format_exc(limit=1).strip().splitlines()[-1][:200]}
        status = "OK  " if results[name]["ok"] else "FAIL"
        print(f"[check] {status} {name}: {results[name].get('value') or results[name].get('error')}")
    OUT.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return results
