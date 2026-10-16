"""News volume from the GDELT DOC 2.0 API (keyless).

GDELT asks for one request every 5 seconds, but on 2026-10-15 it returned
HTTP 429 to 11 of 12 probes even after 170-second gaps, probably because other
agents share this IP. So this client waits 6 seconds between calls, backs off
for minutes on 429, and caches everything; treat GDELT as best-effort.

Bucket size follows the span: under 72 h gives 15-minute buckets, 72 h to a
week hourly, longer spans daily. Only the last 3 months are queryable.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

from harness.context.collectors.base import Series
from harness.context.http import FetchError, PoliteClient
from harness.context.settings import DEFAULT

DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
_client = PoliteClient(min_interval_s=DEFAULT.fetch.gdelt_interval_s, timeout_s=40.0, max_retries=3)


def _resolution(start: datetime, end: datetime) -> str:
    hours = (end - start).total_seconds() / 3600
    return "15min" if hours < 72 else "1h" if hours <= 168 else "1d"


def news_volume(query: str, start: datetime, end: datetime) -> Series:
    """Raw count of monitored news articles matching `query` per bucket."""
    params = {
        "query": query,
        "mode": "timelinevolraw",
        "format": "json",
        "startdatetime": f"{start:%Y%m%d%H%M%S}",
        "enddatetime": f"{end:%Y%m%d%H%M%S}",
    }
    past = end < datetime.now(timezone.utc)
    resp = _client.get(f"{DOC}?" + urllib.parse.urlencode(params), max_age_s=None if past else 1800)
    if not resp.body.lstrip().startswith("{"):
        raise FetchError(f"GDELT returned non-JSON for {query!r}: {resp.body[:160]!r}")
    timeline = resp.json().get("timeline") or []
    if not timeline:
        return Series(term=query, source="gdelt", resolution=_resolution(start, end), fetched_at=resp.fetched_at,
                      note="no matching articles in window")
    data = timeline[0].get("data")
    if data is None:
        raise FetchError(f"GDELT timeline for {query!r} has no 'data' list; keys were {list(timeline[0])}")
    points = []
    for d in data:
        ts = datetime.strptime(d["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        points.append((ts, float(d["value"])))
    return Series(term=query, source="gdelt", resolution=_resolution(start, end), points=points,
                  fetched_at=resp.fetched_at, note="raw article counts")


def articles(query: str, start: datetime, end: datetime, max_records: int = 75) -> list[dict]:
    """Matching articles: url, title, seendate, domain, language, sourcecountry."""
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": min(max_records, 250),
        "startdatetime": f"{start:%Y%m%d%H%M%S}",
        "enddatetime": f"{end:%Y%m%d%H%M%S}",
    }
    resp = _client.get(f"{DOC}?" + urllib.parse.urlencode(params))
    if not resp.body.lstrip().startswith("{"):
        raise FetchError(f"GDELT returned non-JSON for {query!r}: {resp.body[:160]!r}")
    return resp.json().get("articles") or []
