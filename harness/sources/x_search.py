"""X posts about the brand's subjects, and the brand account's own activity.

Both read through `x_common.get_backend`, so they work with whichever X backend has a
credential: X API v2, Grok's X search, or fxtwitter (timelines only, no key).
"""

from datetime import timedelta

from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, now_utc
from harness.sources.watch import load_watch
from harness.sources.x_common import XAccessError, get_backend, post_to_signal
from harness.sources.x_fx import verify_counts

BRAND_LOOKBACK = timedelta(days=7)


class XSearchSource(Source):
    """Recent posts matching the [x] queries in mission/observe.toml."""

    name = "x"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().x
        if not watch.queries:
            raise SourceError("x: no queries in [x] of mission/observe.toml")
        backend = get_backend("search")
        since = now_utc() - timedelta(hours=watch.lookback_hours)
        signals: dict[str, Signal] = {}
        for query in watch.queries:
            for post in verify_counts(backend.search(query, since, limit=watch.per_query)):
                signals.setdefault(post.id, post_to_signal(post, self.name, "post"))
        return list(signals.values())


class BrandActivitySource(Source):
    """The brand account's own recent posts with their counts, plus posts that mention it.

    Own posts tell orient what the account already said and how it landed; mentions are the
    cheapest replies a small account can make, because the author is already talking to it.
    """

    name = "x_brand"

    def fetch(self, mission: str) -> list[Signal]:
        handle = load_watch().brand_handle
        if not handle:
            raise SourceError("x_brand: [brand] x_handle is empty in mission/observe.toml; set the account's handle")
        since = now_utc() - BRAND_LOOKBACK
        signals = [post_to_signal(p, self.name, "own_post") for p in get_backend("user_posts").user_posts(handle, since, 20)]
        try:
            mentions = get_backend("search").search(f"@{handle}", since, 25)
        except XAccessError as e:
            print(f"[observe] {self.name}: mentions skipped: {e}")
            mentions = []
        signals += [post_to_signal(p, self.name, "mention") for p in mentions if p.author.lower() != handle.lower()]
        return signals
