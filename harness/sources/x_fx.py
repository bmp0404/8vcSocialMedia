"""Keyless account timelines through the context worker's fxtwitter reader.

`harness.context.xreader.FxTwitterReader` (branch sam/context-dynamics) owns the HTTP client,
the one-request-per-second limit and the cache; this module only adapts its posts to
`x_common.XPost`. fxtwitter is a third-party proxy with unclear terms that can break without
notice, which is why it sits behind `XBackend` and comes after the X API in `get_backend`.
It has no search endpoint, so only `user_posts` works.
"""

import time
from dataclasses import replace
from datetime import datetime

from harness.sources.common import SourceError, iso_utc
from harness.sources.x_common import XAccessError, XBackend, XPost

# fxtwitter 404'd on 3 of 13 real timelines on 2026-10-15 and served them on the next try.
RETRY_DELAY_S = 2.0


def _reader():
    try:
        from harness.context.xreader import FxTwitterReader
    except ImportError as e:
        raise XAccessError(
            "fxtwitter timelines need harness.context.xreader from sam/context-dynamics, which isn't merged here"
        ) from e
    return FxTwitterReader()


def from_fx(post) -> XPost:
    """Map a harness.context.xreader.XPost. fxtwitter's counts come from X, so they're verified."""
    return XPost(
        id=post.id,
        text=post.text,
        author=post.author_handle,
        created_at=iso_utc(post.created_at),
        likes=post.likes,
        reposts=post.reposts,
        replies=post.replies,
        quotes=post.quotes,
        views=post.views,
        author_followers=post.author_followers,
        is_reply=post.replying_to is not None,
        metrics_verified=True,
    )


class FxXBackend(XBackend):
    name = "fx"

    def __init__(self, reader=None, sleep=time.sleep):
        self.reader = reader or _reader()
        self.sleep = sleep

    def search(self, query: str, since: datetime, limit: int) -> list[XPost]:
        raise XAccessError("fxtwitter has no search endpoint")

    def user_posts(self, handle: str, since: datetime, limit: int) -> list[XPost]:
        # The reader's FetchError subclasses RuntimeError; observe() expects SourceError.
        try:
            raw = self.reader.profile_statuses(handle, limit=limit)
        except RuntimeError as first:
            if "404" not in str(first):
                raise SourceError(f"fxtwitter timeline for @{handle}: {first}") from first
            self.sleep(RETRY_DELAY_S)
            try:
                raw = self.reader.profile_statuses(handle, limit=limit)
            except RuntimeError as second:
                raise SourceError(f"fxtwitter timeline for @{handle}: 404 twice; {self._profile_state(handle)}") from second
        return [from_fx(p) for p in raw if not p.is_repost and p.created_at >= since]

    def _profile_state(self, handle: str) -> str:
        """Tell a missing or suspended account apart from fxtwitter flaking on its timeline."""
        # The reader has no profile method yet; its client is public and shares the rate limit.
        client = getattr(self.reader, "client", None)
        if client is None:
            return "could not check the profile"
        try:
            data = client.get_json(f"https://api.fxtwitter.com/2/profile/{handle}", max_age_s=0)
        except RuntimeError as e:
            return f"profile lookup failed too, so the account may be gone or renamed: {e}"
        if data.get("code") == 200:
            return "the profile exists, so fxtwitter is failing on this timeline"
        return f"the account looks gone: {data.get('message') or data.get('code')}"


def verify_counts(posts: list[XPost], reader=None) -> list[XPost]:
    """Replace counts an LLM reported (Grok) with fxtwitter's reading of the same post.

    Posts already verified pass through. If fxtwitter isn't available, the posts come back
    unchanged and still flagged unverified; a post fxtwitter can't load is dropped, since a URL
    nobody can open is no target.
    """
    if all(p.metrics_verified for p in posts):
        return posts
    try:
        reader = reader or _reader()
    except XAccessError:
        return posts
    out = []
    for p in posts:
        if p.metrics_verified:
            out.append(p)
            continue
        try:
            fresh = from_fx(reader.status(p.id))
        except RuntimeError as e:
            print(f"[observe] dropping {p.url}: fxtwitter could not load it ({e})")
            continue
        out.append(replace(fresh, text=fresh.text or p.text))
    return out
