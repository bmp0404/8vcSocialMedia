"""Google News RSS search. Keyless; queries come from `mission/observe.toml`."""

import xml.etree.ElementTree as ET

from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, get_text, iso_utc, parse_time
from harness.sources.watch import load_watch

URL = "https://news.google.com/rss/search"


def rss_signals(xml_text: str, query: str) -> list[Signal]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise SourceError(f"google_news: RSS for {query!r} is not valid XML: {e}") from e
    channel = root.find("channel")
    if channel is None:
        raise SourceError(f"google_news: RSS for {query!r} has no <channel>")
    signals = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        publisher = item.find("source")
        name = publisher.text.strip() if publisher is not None and publisher.text else None
        # Google appends " - Publisher" to every title; drop it since `author` carries it.
        if name and title.endswith(f" - {name}"):
            title = title[: -len(name) - 3]
        published = parse_time(item.findtext("pubDate"))
        signals.append(
            Signal(
                source="google_news",
                text=title,
                url=(item.findtext("link") or "").strip() or None,
                author=name,
                created_at=iso_utc(published) if published else None,
                metrics={},
                kind="news",
            )
        )
    return signals


class GoogleNewsSource(Source):
    name = "google_news"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().google_news
        if not watch.queries:
            raise SourceError("google_news: no queries in [google_news] of mission/observe.toml")
        signals: list[Signal] = []
        seen: set[str] = set()
        for query in watch.queries:
            q = f"{query} when:{watch.window}" if watch.window else query
            xml_text = get_text(URL, source=self.name, params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
            # Google ranks results by relevance, so newest-first must be imposed before the cut.
            items = sorted(rss_signals(xml_text, query), key=lambda s: s.created_at or "", reverse=True)
            for s in items[: watch.per_query]:
                # The same story under several queries is one story; keep the first.
                key = s.text.lower()
                if key not in seen:
                    seen.add(key)
                    signals.append(s)
        return signals
