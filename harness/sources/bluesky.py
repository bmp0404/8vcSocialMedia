"""Public Bluesky search through the AppView, keyless.

Bluesky is not where the brand posts, so these signals (kind "social") tell orient what fans are
saying and how much engagement it draws, with real counts and timestamps. They never become
reply targets. Posts whose authors opted out of logged-out viewing are skipped.
"""

from datetime import timedelta

from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, get_json, iso_utc, now_utc, parse_time
from harness.sources.watch import load_watch

# api.bsky.app answers search without auth; public.api.bsky.app returned 403 on 2026-10-15.
URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
OPT_OUT_LABEL = "!no-unauthenticated"


def post_signals(payload: dict) -> list[Signal]:
    signals = []
    for p in payload.get("posts", []):
        author = p.get("author") or {}
        if any(label.get("val") == OPT_OUT_LABEL for label in author.get("labels", [])):
            continue
        record = p.get("record") or {}
        created = parse_time(record.get("createdAt"))
        rkey = p.get("uri", "").rsplit("/", 1)[-1]
        handle = author.get("handle")
        # Link-only posts have empty text; the link card's title says what they're about.
        text = record.get("text") or ((record.get("embed") or {}).get("external") or {}).get("title", "")
        if not text:
            continue
        signals.append(
            Signal(
                source="bluesky",
                text=text,
                url=f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else None,
                author=handle,
                created_at=iso_utc(created) if created else None,
                metrics={
                    "likes": p.get("likeCount", 0),
                    "reposts": p.get("repostCount", 0),
                    "replies": p.get("replyCount", 0),
                    "quotes": p.get("quoteCount", 0),
                },
                kind="social",
            )
        )
    return signals


class BlueskySource(Source):
    name = "bluesky"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().bluesky
        if not watch.queries:
            raise SourceError("bluesky: no queries in [bluesky] of the profile's observe.toml")
        since = iso_utc(now_utc() - timedelta(hours=watch.lookback_hours))
        signals: dict[str, Signal] = {}
        for query in watch.queries:
            params = {"q": query, "sort": "top", "since": since, "limit": watch.per_query}
            for s in post_signals(get_json(URL, source=self.name, params=params)):
                signals.setdefault(s.url or s.text, s)
        return list(signals.values())
