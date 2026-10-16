"""Hacker News stories via the Algolia search API (keyless).

For dev tools, Hacker News is where launch attention outside X shows up: a
front-page story about the product the same day is context the X API cannot see.
"""

from __future__ import annotations

import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from harness.context.collectors.base import Series, client

ALGOLIA = "https://hn.algolia.com/api/v1"


@dataclass
class Story:
    id: str
    title: str
    url: str | None
    points: int
    comments: int
    created_at: datetime
    front_page: bool = False


def stories(query: str, start: datetime, end: datetime, front_page_only: bool = False, max_hits: int = 1000) -> list[Story]:
    """Stories matching `query` created in [start, end), newest first."""
    tags = "(story,front_page)" if front_page_only else "story"
    params = {
        "query": query,
        "tags": tags,
        "numericFilters": f"created_at_i>={int(start.timestamp())},created_at_i<{int(end.timestamp())}",
        "hitsPerPage": min(max_hits, 1000),
    }
    url = f"{ALGOLIA}/search_by_date?" + urllib.parse.urlencode(params)
    past = end < datetime.now(timezone.utc) - timedelta(days=2)
    data = client().get_json(url, max_age_s=None if past else 3600)
    out = []
    for h in data.get("hits") or []:
        out.append(
            Story(
                id=str(h.get("objectID")),
                title=h.get("title") or "",
                url=h.get("url"),
                points=int(h.get("points") or 0),
                comments=int(h.get("num_comments") or 0),
                created_at=datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc),
                front_page="front_page" in (h.get("_tags") or []),
            )
        )
    return out


def story_volume(query: str, start: datetime, end: datetime) -> Series:
    """Daily sum of points on stories matching `query`. Points are as of fetch time, not the day itself."""
    days: dict[datetime, float] = defaultdict(float)
    d = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while d < end:
        days[d] = 0.0
        d += timedelta(days=1)
    for s in stories(query, start, end):
        days[s.created_at.replace(hour=0, minute=0, second=0, microsecond=0)] += s.points
    return Series(
        term=query,
        source="hackernews",
        resolution="1d",
        points=sorted(days.items()),
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        note="sum of story points per UTC day; points are current, not as of that day",
    )


def front_page(day: datetime) -> list[Story]:
    """Stories that reached the front page, created on `day` (UTC)."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return stories("", start, start + timedelta(days=1), front_page_only=True)
