import json
import urllib.parse
import urllib.request

from harness.models import Signal
from harness.sources.base import Source

QUERIES = ["claude code", "coding agents", "cursor agent", "codex", "multi-agent", "AI agents parallel"]


class HackerNewsSource(Source):
    """Recent HN stories about AI coding agents, via the free Algolia HN search API."""

    name = "hn"

    def fetch(self, mission: str) -> list[Signal]:
        seen, signals = set(), []
        for q in QUERIES:
            url = "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(
                {"query": q, "tags": "story", "hitsPerPage": 15, "numericFilters": "points>5"}
            )
            with urllib.request.urlopen(url, timeout=15) as resp:
                hits = json.load(resp)["hits"]
            for h in hits:
                if h["objectID"] in seen:
                    continue
                seen.add(h["objectID"])
                signals.append(
                    Signal(
                        source=self.name,
                        text=h.get("title") or "",
                        url=h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                        author=h.get("author"),
                        created_at=h.get("created_at"),
                        metrics={"points": h.get("points"), "comments": h.get("num_comments")},
                    )
                )
        return signals
