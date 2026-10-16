import json
import os
import urllib.request

from harness.models import Signal
from harness.sources.base import Source


class RedditSource(Source):
    """Hot posts from fan subreddits via Reddit's JSON listing.

    TODO: Reddit now returns 403 for unauthenticated requests; switch to OAuth
    (script app at reddit.com/prefs/apps -> oauth.reddit.com) before enabling.
    """

    name = "reddit"

    def fetch(self, mission: str) -> list[Signal]:
        subreddits = os.getenv("HARNESS_SUBREDDITS", "").strip()  # e.g. "caitlinclark+wnba+indianafever"
        if not subreddits:
            raise RuntimeError("set HARNESS_SUBREDDITS (plus-separated) to use the reddit source")
        req = urllib.request.Request(
            f"https://www.reddit.com/r/{subreddits}/hot.json?limit=40",
            headers={"User-Agent": "media-harness/0.1"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        signals = []
        for child in data["data"]["children"]:
            p = child["data"]
            signals.append(
                Signal(
                    source=self.name,
                    text=f"{p['title']}\n{p.get('selftext', '')[:500]}".strip(),
                    id=p.get("id"),
                    url="https://www.reddit.com" + p["permalink"],
                    author=p.get("author"),
                    created_at=str(p.get("created_utc")),
                    metrics={"score": p.get("score"), "comments": p.get("num_comments")},
                )
            )
        return signals
