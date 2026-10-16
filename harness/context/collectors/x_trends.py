"""What was trending on X, per country, from trends24.in (keyless HTML).

The live page holds the last ~24 hourly lists of 50 trends, with no tweet
volumes. History comes only from Wayback Machine captures of that page, which
are sparse: 17 good captures between 2026-08-01 and 2026-10-15 and none for
2026-09-16..24 (checked 2026-10-15). So this is a going-forward source; for
history it covers only the days that happen to have a capture.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from harness.context.collectors.base import client
from harness.context.http import FetchError

_BLOCK = re.compile(
    r"<div class=list-container><h3 class=title data-timestamp=([\d.]+)>.*?</h3><ol class=trend-card__list>(.*?)</ol>",
    re.S,
)
_ITEM = re.compile(r'<a href="[^"]*" class=trend-link>(.*?)</a>', re.S)


@dataclass
class TrendList:
    at: datetime  # UTC time the list was published
    country: str
    trends: list[str]  # rank order, 1 first


def parse_trends24(html: str, country: str) -> list[TrendList]:
    out = []
    for ts, body in _BLOCK.findall(html):
        names = [re.sub(r"<[^>]+>", "", n).strip() for n in _ITEM.findall(body)]
        out.append(TrendList(datetime.fromtimestamp(float(ts), tz=timezone.utc), country, names))
    return sorted(out, key=lambda t: t.at)


def live(country: str = "united-states") -> list[TrendList]:
    """The last ~24 hourly trend lists for `country` (trends24 slug, e.g. united-states)."""
    resp = client().get(f"https://trends24.in/{country}/", max_age_s=1800)
    lists = parse_trends24(resp.body, country)
    if not lists:
        raise FetchError(f"trends24 page for {country} parsed to no trend lists; the HTML layout may have changed")
    return lists


def wayback_captures(country: str, start: datetime, end: datetime) -> list[str]:
    """Wayback timestamps (YYYYMMDDhhmmss) of successful trends24 captures in [start, end]."""
    url = (
        "http://web.archive.org/cdx/search/cdx?"
        f"url=trends24.in/{country}/&from={start:%Y%m%d}&to={end:%Y%m%d}&output=json&fl=timestamp,statuscode&limit=2000"
    )
    rows = client().get_json(url, max_age_s=3600, settled_after=end + timedelta(days=1))
    return [r[0] for r in rows[1:] if len(r) > 1 and r[1] == "200"]


def wayback_lists(country: str, timestamp: str) -> list[TrendList]:
    """The ~24 lists stored in one Wayback capture (the 24 hours before it)."""
    url = f"http://web.archive.org/web/{timestamp}id_/https://trends24.in/{country}/"
    body = client().get(url).body
    lists = parse_trends24(body, country)
    if not lists:
        raise FetchError(f"Wayback capture {timestamp} of trends24/{country} parsed to no lists")
    return lists


def by_day(lists: list[TrendList]) -> dict[str, list[str]]:
    """Distinct trends per UTC day, in first-seen order."""
    days: dict[str, list[str]] = {}
    for tl in lists:
        d = tl.at.strftime("%Y-%m-%d")
        seen = days.setdefault(d, [])
        seen.extend(t for t in tl.trends if t not in seen)
    return days


def to_json(lists: list[TrendList]) -> str:
    return json.dumps([{"at": t.at.isoformat(), "country": t.country, "trends": t.trends} for t in lists])
