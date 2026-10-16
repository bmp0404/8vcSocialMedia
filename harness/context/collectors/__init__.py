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
    # GDELT is opt-in: on this IP its DOC API answers most calls with HTTP 429 after 9-17 s,
    # which cost Observe's live run about 3 minutes, and it misses most dev-tool news anyway.
    sources: tuple[str, ...] = ("wikipedia", "hackernews", "google_trends"),
    errors: dict[str, str] | None = None,
) -> dict[str, Series]:
    """Attention to `term` from each source that answered, as {source: Series}.

    Pass a dict as `errors` to receive {source: reason} for every source that failed.
    """
    from harness.context.collectors import gdelt, google_trends, hackernews, wikipedia

    calls = {
        "wikipedia": lambda: wikipedia.pageviews(wikipedia_article or term, start, end),
        "hackernews": lambda: hackernews.story_volume(term, start, end),
        "google_trends": lambda: google_trends.interest_over_time(term, start, end),
        "gdelt": lambda: gdelt.news_volume(f'"{term}"', start, end),
    }
    got = {}
    for name in sources:
        try:
            got[name] = calls[name]()
        except (FetchError, KeyError, ValueError) as e:
            if errors is not None:
                errors[name] = f"{type(e).__name__}: {e}"
    return got


__all__ = ["Series", "attention"]
