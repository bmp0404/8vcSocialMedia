"""Google Trends, keyless: the trending-now RSS feed and interest over time.

Interest over time uses the same unofficial explore -> widgetdata/multiline
flow as pytrends. It worked on 2026-10-15 at one request every 3-4 seconds.
Values are 0-100 relative to the maximum inside the requested window, so two
requests are not comparable. Windows of a few days return hourly points
(sparse and noisy: many hours come back 0 with hasData false); longer windows
return daily points; a 45-day hourly request returned HTTP 400.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from harness.context.collectors.base import Series
from harness.context.http import USER_AGENT, FetchError, PoliteClient
from harness.context.settings import DEFAULT

BASE = "https://trends.google.com"
HT = "{https://trends.google.com/trending/rss}"
_client = PoliteClient(min_interval_s=DEFAULT.fetch.trends_interval_s)
_cookie: str | None = None


@dataclass
class TrendingSearch:
    term: str
    approx_traffic: str  # bucket such as "1000+"
    published: datetime
    news_titles: list[str]


def trending_now(geo: str = "US") -> list[TrendingSearch]:
    """The latest ~10 trending searches. No history: the feed ignores any time parameter."""
    resp = _client.get(f"{BASE}/trending/rss?geo={geo}", max_age_s=900)
    root = ET.fromstring(resp.body)
    out = []
    for item in root.iter("item"):
        out.append(
            TrendingSearch(
                term=item.findtext("title") or "",
                approx_traffic=item.findtext(f"{HT}approx_traffic") or "",
                published=parsedate_to_datetime(item.findtext("pubDate")).astimezone(timezone.utc),
                news_titles=[n.findtext(f"{HT}news_item_title") or "" for n in item.findall(f"{HT}news_item")],
            )
        )
    if not out:
        raise FetchError(f"Google Trends RSS for geo={geo} had no items")
    return out


def _session_cookie() -> str:
    global _cookie
    if _cookie is None:
        req = urllib.request.Request(f"{BASE}/trends/?geo=US", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                cookies = resp.headers.get_all("Set-Cookie") or []
        except OSError as e:  # URLError and HTTPError are OSErrors
            raise FetchError(f"Google Trends cookie request failed: {e}") from e
        nid = next((c.split(";", 1)[0] for c in cookies if c.startswith("NID=")), None)
        if not nid:
            raise FetchError("Google Trends set no NID cookie; the explore API will refuse requests")
        _cookie = nid
    return _cookie


def _strip(body: str) -> dict:
    return json.loads(re.sub(r"^\)\]\}',?\s*", "", body))


def interest_over_time(term: str, start: datetime, end: datetime, geo: str = "US") -> Series:
    """Relative search interest (0-100) for `term` between `start` and `end` (UTC).

    Spans up to 7 days are requested hourly, longer spans daily.
    """
    hourly = end - start <= timedelta(days=7)
    window = f"{start:%Y-%m-%dT%H} {end:%Y-%m-%dT%H}" if hourly else f"{start:%Y-%m-%d} {end:%Y-%m-%d}"
    return _timeseries(term, window, geo, "1h" if hourly else "1d", settled_after=end + timedelta(days=2))


def interest_recent(term: str, window: str = "now 1-d", geo: str = "US") -> Series:
    """Interest over the last `window` at Google's finest grain.

    On 2026-10-16 "now 1-d" returned 8-minute buckets and "now 7-d" hourly
    ones. The resolution field is read from the response, not assumed.
    """
    return _timeseries(term, window, geo, None, settled_after=None)


def _timeseries(term: str, window: str, geo: str, resolution: str | None, settled_after: datetime | None) -> Series:
    # A relative window ("now 1-d") names a different span every day, so it never settles.
    req = {"comparisonItem": [{"keyword": term, "geo": geo, "time": window}], "category": 0, "property": ""}
    headers = {"Cookie": _session_cookie()}
    explore = _client.get(
        f"{BASE}/trends/api/explore?" + urllib.parse.urlencode({"hl": "en-US", "tz": "0", "req": json.dumps(req)}),
        headers=headers,
        max_age_s=900,
        settled_after=settled_after,
    )
    widgets = _strip(explore.body).get("widgets") or []
    ts = next((w for w in widgets if w.get("id") == "TIMESERIES"), None)
    if ts is None:
        raise FetchError(f"Google Trends explore returned no TIMESERIES widget for {term!r}")
    data = _client.get(
        f"{BASE}/trends/api/widgetdata/multiline?"
        + urllib.parse.urlencode({"hl": "en-US", "tz": "0", "req": json.dumps(ts["request"]), "token": ts["token"]}),
        headers=headers,
        max_age_s=900,
        settled_after=settled_after,
    )
    timeline = _strip(data.body)["default"]["timelineData"]
    points = [
        (datetime.fromtimestamp(int(p["time"]), tz=timezone.utc), float(p["value"][0]))
        for p in timeline
        if p.get("hasData", [True])[0]
    ]
    if resolution is None:
        steps = sorted({int(b["time"]) - int(a["time"]) for a, b in zip(timeline, timeline[1:])})
        resolution = f"{steps[0] // 60}min" if steps else "unknown"
    return Series(
        term=term,
        source="google_trends",
        resolution=resolution,
        points=points,
        fetched_at=data.fetched_at,
        note="0-100 relative to the window maximum; not comparable across requests",
    )
