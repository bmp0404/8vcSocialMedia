"""Google News RSS search. Keyless; queries come from the profile's `observe.toml`.

Every RSS link is a `news.google.com/rss/articles/<id>` redirect, which qualify's critic vetoes
as an opaque link in a caption. Following it with GET or HEAD doesn't help: on 2026-10-15 it
302'd to itself and then served a page whose script finds the publisher URL. The RSS
`<source url=...>` is only the publisher's home page. So `Resolver` does what that page does:
it reads the article id, timestamp and signature from the page, and asks Google's
`batchexecute` endpoint for the publisher URL. Resolved links are cached under out/, and each run
resolves at most `gnews_resolve_max` new ones. A link that isn't resolved keeps the Google URL
and `url_resolved: False`, so Action can avoid it.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from harness import config
from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, get_text, iso_utc, parse_time, post_form
from harness.sources.knobs import current
from harness.sources.watch import load_watch

URL = "https://news.google.com/rss/search"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
ARTICLE_ID = re.compile(r"news\.google\.com/(?:rss/)?articles/([A-Za-z0-9_-]+)")
PAGE_ATTR = re.compile(r'data-n-a-(id|ts|sg)="([^"]+)"')
# A browser user agent: the article page serves the decode attributes to browsers.
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko)"}


def article_id(link: str | None) -> str | None:
    match = ARTICLE_ID.search(link or "")
    return match.group(1) if match else None


def decode_params(page_html: str) -> tuple[str, int, str] | None:
    """(article id, timestamp, signature) from an article page, or None if the page lacks them."""
    found = dict(PAGE_ATTR.findall(page_html))
    if not {"id", "ts", "sg"} <= found.keys() or not found["ts"].isdigit():
        return None
    return found["id"], int(found["ts"]), found["sg"]


def batch_form(aid: str, ts: int, sg: str) -> dict:
    """The form body the article page's own script posts to resolve its link."""
    inner = [
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        aid,
        ts,
        sg,
    ]
    return {"f.req": json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])}


def parse_batch(text: str) -> str | None:
    """The publisher URL from a batchexecute reply, or None."""
    body = text.split("\n", 2)[-1] if text.startswith(")]}'") else text
    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        return None
    for row in rows:
        if isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr" and row[1] == "Fbv4je" and row[2]:
            try:
                payload = json.loads(row[2])
            except json.JSONDecodeError:
                return None
            if payload[:1] == ["garturlres"] and isinstance(payload[1], str) and payload[1].startswith("http"):
                return payload[1]
    return None


def cache_path() -> Path:
    return Path(os.getenv("HARNESS_GNEWS_CACHE") or config.OUT_DIR / "gnews_urls.json")


class Resolver:
    """Google News link -> publisher URL, with a JSON cache and a per-run budget of new lookups.

    `fetch(url)` and `post(url, form)` return response text; tests pass fakes. After the first
    failed lookup it stops trying for the rest of the run, since Google rate-limits in bursts.
    """

    def __init__(
        self,
        path: Path,
        budget: int,
        fetch: Callable[[str], str] | None = None,
        post: Callable[[str, dict], str] | None = None,
    ):
        self.path, self.budget = path, budget
        self.fetch = fetch or (lambda url: get_text(url, source="google_news", headers=BROWSER_UA, timeout=10))
        self.post = post or (lambda url, form: post_form(url, form, source="google_news", headers=BROWSER_UA, timeout=10))
        self.cache: dict[str, str] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.failed: str = ""

    def resolve(self, link: str) -> str | None:
        aid = article_id(link)
        if aid is None:
            return None
        if aid in self.cache:
            return self.cache[aid]
        if self.budget <= 0 or self.failed:
            return None
        self.budget -= 1
        try:
            params = decode_params(self.fetch(link))
            url = parse_batch(self.post(BATCH_URL, batch_form(*params))) if params else None
        except SourceError as e:
            self.failed = str(e)
            return None
        if url is None:
            self.failed = f"no publisher URL for {aid[:24]}..."
            return None
        self.cache[aid] = url
        return url

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.cache, indent=1), encoding="utf-8")


def resolve_links(signals: list[Signal], resolver: Resolver) -> None:
    """Swap each signal's Google link for the publisher URL and record whether that worked."""
    for s in signals:
        url = resolver.resolve(s.url) if s.url else None
        if url:
            s.url = url
        s.metrics["url_resolved"] = url is not None


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
    """Google News search results, linked to each publisher's article."""

    name = "google_news"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().google_news
        if not watch.queries:
            raise SourceError("google_news: no queries in [google_news] of the profile's observe.toml")
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
        # Newest first, so a spent budget leaves the oldest links unresolved.
        signals.sort(key=lambda s: s.created_at or "", reverse=True)
        resolver = Resolver(cache_path(), current().gnews_resolve_max)
        resolve_links(signals, resolver)
        resolver.save()
        unresolved = sum(1 for s in signals if not s.metrics["url_resolved"])
        if unresolved:
            why = f": {resolver.failed}" if resolver.failed else " (resolve budget spent)"
            print(f"[observe] google_news: {unresolved} of {len(signals)} links left as Google redirects{why}")
        return signals
