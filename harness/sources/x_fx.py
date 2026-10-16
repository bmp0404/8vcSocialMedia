"""Keyless account timelines through the context worker's fxtwitter reader.

`harness.context.xreader.FxTwitterReader` (branch sam/context-dynamics) owns the HTTP client,
the one-request-per-second limit and the cache; this module only adapts its posts to
`x_common.XPost`. fxtwitter is a third-party proxy with unclear terms that can break without
notice, which is why it sits behind `XBackend` and comes after the X API in `get_backend`.
It has no search endpoint, so only `user_posts` works.
"""

from datetime import datetime

from harness.sources.common import SourceError, iso_utc
from harness.sources.x_common import XAccessError, XBackend, XPost


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

    def __init__(self, reader=None):
        if reader is None:
            try:
                from harness.context.xreader import FxTwitterReader
            except ImportError as e:
                raise XAccessError(
                    "fxtwitter timelines need harness.context.xreader from sam/context-dynamics, which isn't merged here"
                ) from e
            reader = FxTwitterReader()
        self.reader = reader

    def search(self, query: str, since: datetime, limit: int) -> list[XPost]:
        raise XAccessError("fxtwitter has no search endpoint")

    def user_posts(self, handle: str, since: datetime, limit: int) -> list[XPost]:
        try:
            raw = self.reader.profile_statuses(handle, limit=limit)
        except RuntimeError as e:  # the reader's FetchError subclasses RuntimeError; observe() expects SourceError
            raise SourceError(f"fxtwitter timeline for @{handle}: {e}") from e
        return [from_fx(p) for p in raw if not p.is_repost and p.created_at >= since]
