"""Which accounts the watcher polls for each mission profile, and which of their posts count.

A profile's watch list is the `[reply_targets]` section of its observe.toml, the same list Observe
ranks reply targets from: `accounts` always count, `broad_accounts` count only when the post's text
contains one of `keywords`. Only the account's own original posts count; replies and reposts are
skipped, so every watched post is the root of its own thread.
"""

from dataclasses import dataclass

from harness import config
from harness.sources.common import SourceError
from harness.sources.watch import load_watch


class WatchlistError(SourceError):
    """A profile's watch list is missing or empty."""


@dataclass(frozen=True)
class Watched:
    profile: str
    handle: str
    broad: bool  # counts only posts whose text holds one of the profile's keywords


@dataclass(frozen=True)
class Watchlist:
    profile: str
    handles: tuple[Watched, ...]
    keywords: tuple[str, ...]

    def counts(self, handle: str, post) -> bool:
        """True when `post` (an xreader XPost read from `handle`'s timeline) is a watched root post."""
        if post.is_repost or post.timeline_context or post.replying_to or post.replying_to_status:
            return False
        if post.author_handle.lower() != handle.lower():
            return False
        broad = next((w.broad for w in self.handles if w.handle.lower() == handle.lower()), None)
        if broad is None:
            raise WatchlistError(f"@{handle} isn't on the {self.profile} watch list")
        return not broad or any(k.lower() in post.text.lower() for k in self.keywords)


def load(profile: str) -> Watchlist:
    """The watch list in mission/profiles/<profile>/observe.toml. Raises WatchlistError when it has no handles."""
    path = config.profile_file("observe.toml", profile)
    cfg = load_watch(path).reply_targets
    seen, handles = set(), []
    for handle, broad in [(h, False) for h in cfg.accounts] + [(h, True) for h in cfg.broad_accounts]:
        h = handle.strip().lstrip("@")
        if h.lower() in seen:
            raise WatchlistError(f"{path}: @{h} is listed twice in [reply_targets]")
        seen.add(h.lower())
        handles.append(Watched(profile, h, broad))
    if not handles:
        raise WatchlistError(f"{path}: [reply_targets] accounts and broad_accounts are both empty")
    if any(w.broad for w in handles) and not cfg.keywords:
        raise WatchlistError(f"{path}: broad_accounts need [reply_targets] keywords, and keywords is empty")
    return Watchlist(profile, tuple(handles), tuple(cfg.keywords))
