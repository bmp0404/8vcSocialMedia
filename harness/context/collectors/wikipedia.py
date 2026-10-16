"""Wikipedia pageviews (Wikimedia REST, keyless) and X-handle-to-article lookup (Wikidata, keyless).

Pageviews are a cheap proxy for how much the public is looking something up.
Per-article data is daily: the API rejects `hourly` for per-article requests
(HTTP 400 "granularity should be equal to one of the allowed values: [daily,
monthly]", checked 2026-10-15). Hourly views exist only in the bulk dump files.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

from harness.context.collectors.base import Series, client
from harness.context.http import FetchError

PAGEVIEWS = "https://wikimedia.org/api/rest_v1/metrics/pageviews"
SPARQL = "https://query.wikidata.org/sparql"


def pageviews(article: str, start: datetime, end: datetime, granularity: str = "daily", project: str = "en.wikipedia") -> Series:
    """Daily user (non-bot) pageviews for `article` (title with underscores) from `start` to `end` inclusive."""
    if granularity not in ("daily", "monthly"):
        raise ValueError("Wikimedia per-article pageviews support only daily or monthly granularity")
    title = urllib.parse.quote(article.replace(" ", "_"), safe="")
    url = f"{PAGEVIEWS}/per-article/{project}/all-access/user/{title}/{granularity}/{start:%Y%m%d00}/{end:%Y%m%d00}"
    # Past days do not change once published; any cached copy is good unless the range reaches today.
    recent = (datetime.now(timezone.utc) - end).days < 2
    resp = client().get(url, max_age_s=6 * 3600 if recent else None)
    items = resp.json().get("items") or []
    if not items:
        raise FetchError(f"wikimedia returned no pageviews for {article!r} ({start:%Y-%m-%d}..{end:%Y-%m-%d})")
    points = [(datetime.strptime(i["timestamp"], "%Y%m%d%H").replace(tzinfo=timezone.utc), float(i["views"])) for i in items]
    return Series(term=article, source="wikipedia", resolution="1d", points=points, fetched_at=resp.fetched_at)


def top_articles(day: datetime, project: str = "en.wikipedia") -> list[tuple[str, int]]:
    """The day's most-viewed articles: what the general public was looking at."""
    url = f"{PAGEVIEWS}/top/{project}/all-access/{day:%Y/%m/%d}"
    data = client().get_json(url)
    arts = (data.get("items") or [{}])[0].get("articles") or []
    skip = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "File:")
    return [(a["article"], a["views"]) for a in arts if not a["article"].startswith(skip)]


def articles_for_handles(handles: list[str]) -> dict[str, dict]:
    """Map X handles to Wikidata items and English Wikipedia articles via the X-username property (P2002).

    Returns {lowercased handle: {"wikidata": Q-id, "article": title or None, "label": str}}.
    Matching is exact on the handle as given or lowercased, so pass X's own
    capitalisation. Handles with no Wikidata item are absent.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(handles), 40):
        chunk = [h.lstrip("@") for h in handles[i : i + 40]]
        # Exact-match VALUES uses the property index. A case-insensitive FILTER
        # scans every P2002 statement and got HTTP 429 from the query service.
        variants = sorted({v for h in chunk for v in (h, h.lower())})
        values = " ".join(json.dumps(v) for v in variants)
        query = f"""
SELECT ?item ?handle ?article ?label WHERE {{
  VALUES ?handle {{ {values} }}
  ?item wdt:P2002 ?handle .
  OPTIONAL {{ ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }}
  OPTIONAL {{ ?item rdfs:label ?label FILTER(LANG(?label) = "en") }}
}}"""
        url = SPARQL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
        data = client().get_json(url, headers={"Accept": "application/sparql-results+json"})
        for b in data["results"]["bindings"]:
            h = b["handle"]["value"].lower()
            article = b.get("article", {}).get("value")
            prev = out.get(h)
            if prev and prev["article"] and not article:
                continue
            out[h] = {
                "wikidata": b["item"]["value"].rsplit("/", 1)[-1],
                "article": urllib.parse.unquote(article.rsplit("/wiki/", 1)[-1]) if article else None,
                "label": b.get("label", {}).get("value"),
            }
    return out


def article_for_handle(handle: str) -> str | None:
    hit = articles_for_handles([handle]).get(handle.lstrip("@").lower())
    return hit["article"] if hit else None
