"""One read-only interface over the ways we can read X.

Three backends implement `XBackend`: the X API v2 with a bearer token (`x_api.py`), xAI's
Grok with its X search tool (`x_grok.py`), and fxtwitter account timelines (`x_fx.py`, no key).
`get_backend()` picks the first one whose credential is present, so the X sources start
working the moment a key lands in `.env`. Nothing here writes to X.

There is deliberately no browser backend: X's Terms of Service (effective 2026-10-09) forbid
scraping "in any form, for any purpose" without written consent. See observation/DESIGN.md.
"""

import os
from dataclasses import dataclass
from datetime import datetime

from harness.models import Signal
from harness.sources.common import SourceError


class XAccessError(SourceError):
    """No X backend is usable: missing credential, missing dependency, or the backend refused."""


@dataclass
class XPost:
    """One X post as every backend reports it. Counts are None when the backend didn't say."""

    id: str
    text: str
    author: str  # handle without "@"
    created_at: str  # ISO 8601 UTC with Z, see common.iso_utc
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None
    quotes: int | None = None
    views: int | None = None
    author_followers: int | None = None
    is_reply: bool = False
    # False when the numbers came from an LLM's reading (Grok) rather than X itself.
    metrics_verified: bool = True

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author}/status/{self.id}"

    def metrics(self) -> dict:
        names = ("likes", "reposts", "replies", "quotes", "views", "author_followers")
        out = {k: getattr(self, k) for k in names if getattr(self, k) is not None}
        if not self.metrics_verified:
            out["metrics_unverified"] = 1
        return out


class XBackend:
    """Read-only X access. Implementations raise XAccessError or SourceError, never return [] on failure."""

    name = "base"

    def search(self, query: str, since: datetime, limit: int) -> list[XPost]:
        """Recent original posts matching `query`, newest first. Exclude reposts."""
        raise NotImplementedError

    def user_posts(self, handle: str, since: datetime, limit: int) -> list[XPost]:
        """`handle`'s own recent posts (no reposts), newest first."""
        raise NotImplementedError


def post_to_signal(post: XPost, source: str, kind: str) -> Signal:
    return Signal(
        source=source,
        text=post.text,
        url=post.url,
        author=post.author,
        created_at=post.created_at,
        metrics=post.metrics(),
        kind=kind,
    )


BACKEND_ENV = "HARNESS_X_BACKEND"

# Credential that switches each backend on, and what it can do. fxtwitter needs no key but has
# no search endpoint, so it only serves account timelines.
BACKENDS = {
    "api": ("X_BEARER_TOKEN", {"search", "user_posts"}),
    "grok": ("XAI_API_KEY", {"search", "user_posts"}),
    "fx": (None, {"user_posts"}),
}
# auto order per capability. Backends whose counts come from X itself go before Grok, whose
# counts are the model's reading of the page.
AUTO_ORDER = {
    "search": ["api", "grok"],
    "user_posts": ["api", "fx", "grok"],
}


def _build(name: str) -> XBackend:
    if name == "api":
        from harness.sources.x_api import XApiBackend

        return XApiBackend(os.environ["X_BEARER_TOKEN"])
    if name == "grok":
        from harness.sources.x_grok import GrokXBackend

        return GrokXBackend(os.environ["XAI_API_KEY"])
    from harness.sources.x_fx import FxXBackend

    return FxXBackend()


def get_backend(capability: str = "search", choice: str | None = None) -> XBackend:
    """Return the X backend named by HARNESS_X_BACKEND, or the first usable one for `capability`.

    `capability` is "search" or "user_posts". A backend is usable when its credential is set
    (fxtwitter needs none). Raises XAccessError naming the env vars that would switch one on.
    """
    if capability not in AUTO_ORDER:
        raise ValueError(f"capability must be one of {sorted(AUTO_ORDER)}, got {capability!r}")
    choice = (choice or os.getenv(BACKEND_ENV) or "auto").lower()
    if choice != "auto" and choice not in BACKENDS:
        raise XAccessError(f"{BACKEND_ENV}={choice!r}; expected auto or one of {sorted(BACKENDS)}")
    names = AUTO_ORDER[capability] if choice == "auto" else [choice]
    for name in names:
        env, caps = BACKENDS[name]
        if capability not in caps:
            raise XAccessError(f"X backend {name!r} can't {capability.replace('_', ' ')}; set {BACKEND_ENV}=auto")
        if env is None or os.getenv(env):
            return _build(name)
    wanted = " or ".join(BACKENDS[n][0] for n in names if BACKENDS[n][0])
    raise XAccessError(f"no X read access for {capability.replace('_', ' ')}: set {wanted} in .env (see observation/DESIGN.md)")
