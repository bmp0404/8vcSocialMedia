"""Read-only access to the account's posts, through the context worker's fxtwitter reader.

`harness.context.xreader` lives on sam/context-dynamics (PR #18) and is imported only when sync
needs the network, the same way `harness/sources/x_fx.py` does it, so everything else here works
without it.

`FxTwitterReader.profile_statuses` reads X's Posts tab, which leaves replies out, and a reply action
can only be found among the account's replies. fxtwitter takes `with_replies=1` on the same endpoint
(checked on @CClarkReport on 2026-10-16: its replies to other accounts appear only with the flag).
Until the reader accepts that flag, `timeline` builds the request itself through the reader's
`_get`, so the shared one-request-per-second limit, the 404 retry and the code check still apply.
"""

import urllib.parse
from dataclasses import dataclass
from datetime import datetime


class ReaderMissing(RuntimeError):
    """The fxtwitter reader from PR #18 isn't importable in this checkout."""


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
    posts: list[OwnPost]  # newest first, without reposts or other accounts' posts
    fetched_at: str
    exhausted: bool  # True when fxtwitter had no older page, so the read reaches the account's first post
    requests: int


def fx_reader() -> "FxOutcomeReader":
    try:
        from harness.context import xreader
    except ImportError as e:
        raise ReaderMissing(
            "harness outcomes reads X through harness/context/xreader.py from sam/context-dynamics (PR #18), "
            "which isn't in this checkout. Merge PR #18 first, or run from a branch that has it."
        ) from e
    return FxOutcomeReader(xreader)


def _counts(post) -> Counts:
    return Counts(post.fetched_at, post.likes, post.reposts, post.replies, post.quotes, post.views,
                  post.author_followers)


def _kind(post, raw: dict, handle: str) -> str:
    if post.replying_to or post.replying_to_status:
        return "self_reply" if (post.replying_to or "").lower() == handle.lower() else "reply"
    # X marks a reply's leading @mentions with display_text_range. fxtwitter sometimes drops
    # replying_to on a reply (@CClarkReport 2106410826999500938, 2026-10-16) but keeps the range.
    start = ((raw.get("raw_text") or {}).get("display_text_range") or [0])[0]
    return "reply" if start and post.text[:start].lstrip().startswith("@") else "original"


class FxOutcomeReader:
    """`timeline` and `status` over api.fxtwitter.com. Counts how many requests it made."""

    def __init__(self, xreader, reader=None):
        self._x = xreader
        self.reader = reader or xreader.FxTwitterReader()
        self.requests = 0

    def timeline(self, handle: str, limit: int) -> Timeline:
        """The account's newest `limit` posts and replies, one fxtwitter page (about 20) per request."""
        posts: list[OwnPost] = []
        seen: set[str] = set()
        cursor, fetched_at, exhausted, requests = None, "", True, 0
        while len(posts) < limit:
            path = f"/2/profile/{urllib.parse.quote(handle)}/statuses?with_replies=1"
            if cursor:
                path += "&cursor=" + urllib.parse.quote(cursor)
            data, fetched_at = self.reader._get(path, 0)
            requests += 1
            for raw in data.get("results") or []:
                if raw.get("type", "status") != "status":
                    continue
                p = self._x._parse(raw, fetched_at)
                # with_replies also returns the posts the account replied to, by their own authors, and
                # a post can come back on two pages (@greptile, 2026-10-16).
                if p.author_handle.lower() != handle.lower() or p.is_repost or p.id in seen:
                    continue
                seen.add(p.id)
                posts.append(OwnPost(p.id, p.created_at, _kind(p, raw, handle), p.text, p.replying_to_status,
                                     p.quoted_id, _counts(p)))
            cursor = (data.get("cursor") or {}).get("bottom")
            exhausted = not cursor or not data.get("results")
            if exhausted:
                break
        self.requests += requests
        posts.sort(key=lambda p: p.created_at, reverse=True)
        return Timeline(posts[:limit], fetched_at, exhausted and len(posts) <= limit, requests)

    def status(self, status_id: str) -> Counts:
        self.requests += 1
        return _counts(self.reader.status(status_id))
