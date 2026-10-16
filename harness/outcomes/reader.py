"""Read-only access to the account's posts, through the context worker's fxtwitter reader.

`harness.context.xreader` lives on sam/context-dynamics (PR #18) and is imported only when sync
needs the network, the same way `harness/sources/x_fx.py` does it, so everything else here works
without it. The timeline is read with `with_replies=True`, because X's Posts tab leaves replies out
and a reply action can only be found among the account's replies.
"""

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone


class ReaderMissing(RuntimeError):
    """The fxtwitter reader from PR #18 isn't importable in this checkout, or is too old."""


@dataclass(frozen=True)
class Counts:
    """A post's public counts at `fetched_at` (ISO 8601 UTC)."""

    fetched_at: str
    likes: int
    reposts: int
    replies: int
    quotes: int
    views: int | None = None
    author_followers: int | None = None

    @property
    def engagement(self) -> int:
        """likes + reposts + replies + quotes, the sum the dataset and the context worker call engagement."""
        return self.likes + self.reposts + self.replies + self.quotes


@dataclass(frozen=True)
class OwnPost:
    """One of the brand account's own posts as its timeline showed it."""

    id: str
    created_at: datetime
    kind: str  # "original" (posts and quotes), "reply" to another account, or "self_reply" (a thread)
    text: str
    replying_to_status: str | None
    quoted_id: str | None
    counts: Counts | None = None  # None when rebuilt from the ledger, which keeps counts per checkpoint


@dataclass(frozen=True)
class Timeline:
    posts: list[OwnPost]  # the account's own posts, newest first, without reposts
    fetched_at: str
    # The oldest creation time the feed provably reached, or None when it returned no usable post.
    # A post older than this may be missing from the read. A feed that ends early doesn't prove it
    # reached the account's first post: one empty page from fxtwitter looks the same.
    reach: datetime | None


def fx_reader() -> "FxOutcomeReader":
    try:
        from harness.context import xreader
    except ImportError as e:
        raise ReaderMissing(
            "harness outcomes reads X through harness/context/xreader.py from sam/context-dynamics (PR #18), "
            "which isn't in this checkout. Merge PR #18 first, or run from a branch that has it."
        ) from e
    if "with_replies" not in inspect.signature(xreader.FxTwitterReader.profile_statuses).parameters:
        raise ReaderMissing("harness/context/xreader.py predates profile_statuses(with_replies=...), which "
                            "bf396e5 on sam/context-dynamics (PR #18) added; update that checkout")
    return FxOutcomeReader(xreader.FxTwitterReader())


def _counts(post) -> Counts:
    return Counts(post.fetched_at, post.likes, post.reposts, post.replies, post.quotes, post.views,
                  post.author_followers)


def _kind(post, handle: str) -> str:
    if not (post.replying_to or post.replying_to_status):
        return "original"
    return "self_reply" if (post.replying_to or "").lower() == handle.lower() else "reply"


def own_timeline(feed: list, handle: str, read_at: str) -> Timeline:
    """The account's own posts from a with-replies feed of XReader posts, in feed order.

    `read_at` dates the read when the feed is empty; otherwise the latest fetch time in it does.
    """
    own = [p for p in feed if not p.timeline_context and not p.is_repost]
    # The feed shows the post a reply answers right above the reply, whatever its age. When that
    # parent is the account's own old post it isn't flagged as context, so no post that something in
    # the feed replies to counts toward `reach`. Dropping a recent post that merely has a reply only
    # makes `reach` later, which can delay a not_posted verdict but never invent one.
    parents = {p.replying_to_status for p in feed if p.replying_to_status}
    reached = [p.created_at for p in own if p.id not in parents]
    posts, seen = [], set()
    for p in own:
        if p.id in seen:  # a post can come back on two pages (@greptile, 2026-10-16)
            continue
        seen.add(p.id)
        posts.append(OwnPost(p.id, p.created_at, _kind(p, handle), p.text, p.replying_to_status, p.quoted_id,
                             _counts(p)))
    posts.sort(key=lambda p: p.created_at, reverse=True)
    fetched_at = max((p.fetched_at for p in feed), default=read_at)
    return Timeline(posts, fetched_at, min(reached, default=None))


class FxOutcomeReader:
    """`timeline` and `status` over an XReader, counting the calls it makes."""

    def __init__(self, reader):
        self.reader = reader
        self.timeline_reads = 0
        self.status_reads = 0

    def timeline(self, handle: str, limit: int) -> Timeline:
        """The newest `limit` entries of the account's posts-and-replies feed, about 20 per request."""
        self.timeline_reads += 1
        read_at = datetime.now(timezone.utc).isoformat()
        return own_timeline(self.reader.profile_statuses(handle, limit=limit, with_replies=True), handle, read_at)

    def status(self, status_id: str) -> Counts:
        self.status_reads += 1
        return _counts(self.reader.status(status_id))
