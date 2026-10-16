"""Keyless context collectors. Importing this package makes no network call.

Each function raises `harness.context.http.FetchError` with the reason when its
source fails. `attention()` calls every per-term source and records failures
instead of raising, so one blocked API does not hide the others.
"""

from __future__ import annotations

from datetime import datetime

from harness.context.collectors.base import Series
from harness.context.http import FetchError


def attention(
    term: str,
    start: datetime,
    end: datetime,
    wikipedia_article: str | None = None,
    sources: tuple[str, ...] = ("wikipedia", "hackernews", "google_trends", "gdelt"),
) -> tuple[dict[str, Series], dict[str, str]]:
    """Attention to `term` from each source: ({source: Series}, {source: error message})."""
    from harness.context.collectors import gdelt, google_trends, hackernews, wikipedia

    calls = {
        "wikipedia": lambda: wikipedia.pageviews(wikipedia_article or term, start, end),
        "hackernews": lambda: hackernews.story_volume(term, start, end),
        "google_trends": lambda: google_trends.interest_over_time(term, start, end),
        "gdelt": lambda: gdelt.news_volume(f'"{term}"', start, end),
    }
    got, errors = {}, {}
    for name in sources:
        try:
            got[name] = calls[name]()
        except (FetchError, KeyError, ValueError) as e:
            errors[name] = f"{type(e).__name__}: {e}"
    return got, errors


__all__ = ["Series", "attention"]
