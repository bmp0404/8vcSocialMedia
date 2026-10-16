"""Read-only X readers. Nothing here writes to X.

`XReader` is the interface the tracker, the enrichment job and Observe call.
`FxTwitterReader` implements it against api.fxtwitter.com, a free third-party
service that scrapes X and needs no key. Its terms of service are unclear and
it can break or rate-limit without notice, so code should depend on `XReader`
and an official X API reader can replace this one.

Every count on an `XPost` is the value at `fetched_at`, not at post time.
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol

from harness.context import snowflake
from harness.context.http import FetchError, PoliteClient

FX_BASE = "https://api.fxtwitter.com"
_READER_DEFAULT = object()  # "use the reader's max_age_s"


@dataclass
class XPost:
    """One X post as seen at `fetched_at`."""

    id: str
    text: str
    author_handle: str
    created_at: datetime  # UTC, decoded from the Snowflake id
    fetched_at: str  # ISO-8601 UTC; every count below is as of this moment
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0
    bookmarks: int | None = None
    views: int | None = None
    author_followers: int | None = None
    author_verified: bool | None = None
    author_description: str | None = None
    replying_to: str | None = None  # handle this post replies to
    replying_to_status: str | None = None
    quoted_id: str | None = None
    is_repost: bool = False  # True when it appears on a timeline because `reposted_by` reposted it
    reposted_by: str | None = None
    media_types: list[str] = field(default_factory=list)
    n_links: int = 0  # non-media URLs in the text
    n_mentions: int = 0
    lang: str | None = None
    source: str = "fxtwitter"

    @property
    def created_unix(self) -> int:
        return int(self.created_at.timestamp())

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author_handle}/status/{self.id}"

    @property
    def engagement(self) -> int:
        """likes + reposts + replies + quotes, the same sum as the dataset's `Engagement` column."""
        return self.likes + self.reposts + self.replies + self.quotes

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


class XReader(Protocol):
    def status(self, status_id: str) -> XPost: ...

    def profile_statuses(self, handle: str, limit: int = 20) -> list[XPost]: ...

    def conversation(self, status_id: str) -> list[XPost]: ...


def _parse(raw: dict, fetched_at: str) -> XPost:
    author = raw.get("author") or {}
    facets = (raw.get("raw_text") or {}).get("facets") or []
    media = (raw.get("media") or {}).get("all") or []
    reply_to = raw.get("replying_to")
    if isinstance(reply_to, dict):
        reply_status = reply_to.get("status")
        reply_to = reply_to.get("screen_name")
    else:
        reply_status = raw.get("replying_to_status")
    reposted_by = raw.get("reposted_by")
    if isinstance(reposted_by, dict):
        reposted_by = reposted_by.get("screen_name")
    quote = raw.get("quote") or {}
    verification = author.get("verification") or {}
    post_id = str(raw["id"])
    return XPost(
        id=post_id,
        text=raw.get("text") or "",
        author_handle=author.get("screen_name") or "",
        created_at=snowflake.to_datetime(post_id),
        fetched_at=fetched_at,
        likes=int(raw.get("likes") or 0),
        reposts=int(raw.get("reposts", raw.get("retweets")) or 0),
        replies=int(raw.get("replies") or 0),
        quotes=int(raw.get("quotes") or 0),
        bookmarks=raw.get("bookmarks"),
        views=raw.get("views"),
        author_followers=author.get("followers"),
        author_verified=verification.get("verified") if verification else None,
        author_description=author.get("description"),
        replying_to=reply_to,
        replying_to_status=str(reply_status) if reply_status else None,
        quoted_id=str(quote["id"]) if quote.get("id") else None,
        is_repost=bool(reposted_by),
        reposted_by=reposted_by,
        media_types=[m.get("type", "") for m in media],
        n_links=sum(1 for f in facets if f.get("type") == "url"),
        n_mentions=sum(1 for f in facets if f.get("type") == "mention"),
        lang=raw.get("lang"),
    )


class FxTwitterReader:
    """XReader backed by api.fxtwitter.com. One request per second, cached responses."""

    def __init__(self, client: PoliteClient | None = None, max_age_s: float | None = 0):
        # max_age_s=0 forces a fresh fetch (live counts); None reuses any cached copy.
        self.client = client or PoliteClient(min_interval_s=1.0)
        self.max_age_s = max_age_s

    def _get(self, path: str, max_age_s) -> tuple[dict, str]:
        if max_age_s is _READER_DEFAULT:
            max_age_s = self.max_age_s
        try:
            resp = self.client.get(FX_BASE + path, max_age_s=max_age_s)
        except FetchError as e:
            # Observe saw /2/profile/<h>/statuses 404 on live handles and succeed on retry.
            if "HTTP 404" not in str(e) or not path.startswith("/2/profile/"):
                raise
            time.sleep(2)
            resp = self.client.get(FX_BASE + path, max_age_s=max_age_s)
        data = resp.json()
        if data.get("code") != 200:
            raise FetchError(f"fxtwitter {path} returned code {data.get('code')}: {data.get('message')}")
        return data, resp.fetched_at

    def status(self, status_id: str, max_age_s=_READER_DEFAULT) -> XPost:
        data, fetched_at = self._get(f"/status/{int(status_id)}", max_age_s)
        return _parse(data["tweet"], fetched_at)

    def profile_statuses(self, handle: str, limit: int = 20, max_age_s=_READER_DEFAULT) -> list[XPost]:
        """Newest first. Includes the account's reposts, marked `is_repost=True`."""
        handle = handle.lstrip("@")
        posts: list[XPost] = []
        cursor = None
        while len(posts) < limit:
            path = f"/2/profile/{urllib.parse.quote(handle)}/statuses"
            if cursor:
                path += "?cursor=" + urllib.parse.quote(cursor)
            data, fetched_at = self._get(path, max_age_s)
            batch = [_parse(r, fetched_at) for r in data.get("results") or [] if r.get("type", "status") == "status"]
            if not batch:
                break
            for p in batch:
                if p.reposted_by is None and p.author_handle.lower() != handle.lower():
                    p.is_repost, p.reposted_by = True, handle
            posts.extend(batch)
            cursor = (data.get("cursor") or {}).get("bottom")
            if not cursor:
                break
        return posts[:limit]

    def conversation(self, status_id: str, max_age_s=_READER_DEFAULT) -> list[XPost]:
        """The first page of replies (about 35), in X's relevance order, not time order.

        Each reply carries its author's follower count. fxtwitter ignores the
        bottom cursor for conversations (checked 2026-10-15: `cursor=` 404s and
        `c=` returns page one again), so deeper pages are not available here.
        """
        data, fetched_at = self._get(f"/2/conversation/{int(status_id)}", max_age_s)
        return [_parse(r, fetched_at) for r in data.get("replies") or []]
