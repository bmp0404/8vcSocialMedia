"""What an account's own timeline says about it: baselines, voice exemplars and the accounts it engages with.

Every function here is pure and takes posts the way `XReader.profile_statuses` returns them, so a fake reader,
the live fxtwitter reader and the corpus reader in `validate.py` all go through the same code.
"""

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from harness.context.xreader import XPost

# A post's counts are taken as settled once it is 48 hours old at the read, the corpus rule
# (`harness.corpus.load.MATURE_AFTER`). A younger post is still gaining, so it would understate the median
# and could never look strong enough to be an exemplar.
MATURE_AFTER = timedelta(hours=48)
# An exemplar beats the account's median engagement at least this many times. No measurement picked 2;
# it keeps out posts that did merely well. Five exemplars fit in the mission without crowding it.
EXEMPLAR_MIN_MULTIPLE = 2.0
EXEMPLAR_COUNT = 5
# The watcher polls each handle once per 120 s interval, and the devtools list's 24 handles keep one profile
# at about 0.2 timeline requests a second (harness/watcher/DESIGN.md). A drafted list stays inside that.
WATCH_MAX = 24
# An account the brand engaged with once is as likely noise as a relationship, so it needs two posts.
WATCH_MIN_EVENTS = 2
ENGAGEMENT_KINDS = ("reply", "quote", "mention", "repost")

HANDLE = re.compile(r"(?<![\w@])@(\w{1,15})")  # the gate's HANDLE pattern
LEADING_HANDLES = re.compile(r"^(?:\s*@\w{1,15})+\s*")
URL = re.compile(r"https?://\S+")
WORD = re.compile(r"[A-Za-z][A-Za-z'’]+")


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.lower().lstrip("@") == b.lower().lstrip("@")


def _read_at(p: XPost) -> datetime:
    return datetime.fromisoformat(p.fetched_at)


@dataclass
class Timeline:
    """One account's timeline split by what each post is. Reposts keep the original author and time."""

    handle: str
    originals: list[XPost] = field(default_factory=list)  # its own top-level posts, quotes included
    continuations: list[XPost] = field(default_factory=list)  # its replies to itself: threads
    replies: list[XPost] = field(default_factory=list)  # its replies to other accounts
    reposts: list[XPost] = field(default_factory=list)  # other accounts' posts it reposted

    @property
    def own(self) -> list[XPost]:
        return self.originals + self.continuations + self.replies

    @property
    def read_at(self) -> datetime | None:
        stamps = [_read_at(p) for p in self.own + self.reposts]
        return max(stamps) if stamps else None


def split(handle: str, statuses: list[XPost]) -> Timeline:
    """Sort a read of the account's tabs into a Timeline, newest first, each status once.

    A status read twice (it is on both tabs) keeps the later read's counts. Posts a with-replies tab shows only
    as context for a reply (`timeline_context`) are dropped: they are other accounts' words, and the reply itself
    says whom the account answered."""
    latest: dict[str, XPost] = {}
    for p in statuses:
        if p.timeline_context:
            continue
        if p.id not in latest or p.fetched_at > latest[p.id].fetched_at:
            latest[p.id] = p
    tl = Timeline(handle.lstrip("@"))
    for p in sorted(latest.values(), key=lambda p: int(p.id), reverse=True):
        if p.is_repost:
            if not _same(p.author_handle, tl.handle):
                tl.reposts.append(p)
        elif not _same(p.author_handle, tl.handle):
            continue  # another account's post that isn't marked as a repost: nothing the account wrote
        elif p.replying_to is None:
            tl.originals.append(p)
        elif _same(p.replying_to, tl.handle):
            tl.continuations.append(p)
        else:
            tl.replies.append(p)
    return tl


# ---------------------------------------------------------------- baselines


@dataclass
class Baselines:
    read_at: str
    originals: int
    continuations: int
    replies: int
    reposts: int
    # Posts a day over each kind's own window, from its oldest post read to the read: the two tabs reach back
    # different distances, so one shared window would understate whichever kind the read cut shorter.
    originals_per_day: float
    replies_per_day: float
    reposts_per_day: float
    oldest_original: str | None
    mature_originals: int  # the originals the medians and exemplars use
    median_engagement: float | None  # likes + reposts + replies + quotes, as XPost.engagement
    median_likes: float | None
    median_views: float | None
    formats: dict[str, float]  # share of all originals with each format; a post can have several
    media_share: float  # share of originals with any photo, video or gif


FORMATS = ("text_only", "photo", "video", "gif", "link", "quote")


def _per_day(posts: list[XPost], read_at: datetime) -> float:
    if not posts:
        return 0.0
    oldest = min(p.created_at for p in posts)
    return round(len(posts) / max((read_at - oldest) / timedelta(days=1), 1.0), 3)


def _formats(p: XPost) -> set[str]:
    out = {f for f in ("photo", "video", "gif") if f in p.media_types}
    if p.n_links:
        out.add("link")
    if p.quoted_id:
        out.add("quote")
    return out or {"text_only"}


def mature(posts: list[XPost]) -> list[XPost]:
    return [p for p in posts if _read_at(p) - p.created_at >= MATURE_AFTER]


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def baselines(tl: Timeline) -> Baselines:
    read_at = tl.read_at
    if read_at is None:
        raise ValueError(f"@{tl.handle}'s timeline read returned no posts, so there is nothing to measure")
    settled = mature(tl.originals)
    n = len(tl.originals)
    counts = Counter(f for p in tl.originals for f in _formats(p))
    return Baselines(
        read_at=read_at.isoformat(), originals=n, continuations=len(tl.continuations), replies=len(tl.replies),
        reposts=len(tl.reposts), originals_per_day=_per_day(tl.originals, read_at),
        replies_per_day=_per_day(tl.replies, read_at), reposts_per_day=_per_day(tl.reposts, read_at),
        oldest_original=min(p.created_at for p in tl.originals).isoformat() if tl.originals else None,
        mature_originals=len(settled), median_engagement=_median([p.engagement for p in settled]),
        median_likes=_median([p.likes for p in settled]),
        median_views=_median([p.views for p in settled if p.views is not None]),
        formats={f: round(counts[f] / n, 3) if n else 0.0 for f in FORMATS},
        media_share=round(sum(1 for p in tl.originals if p.media_types) / n, 3) if n else 0.0,
    )


# ---------------------------------------------------------------- exemplars


@dataclass
class Exemplar:
    id: str
    url: str
    text: str
    engagement: int
    multiple: float  # engagement over the account's median engagement


def words_of(text: str) -> list[str]:
    """The words of a post once links and @handles are gone."""
    return WORD.findall(HANDLE.sub(" ", URL.sub(" ", text or "")))


def exemplars(tl: Timeline, k: int = EXEMPLAR_COUNT, min_multiple: float = EXEMPLAR_MIN_MULTIPLE) -> list[Exemplar]:
    """The account's strongest settled originals relative to its own median, at least `min_multiple` times it.

    Only originals with three or more words count, because an exemplar is there to show the voice. The median
    is over every settled original; a median of 0 is treated as 1 so a post with any engagement can qualify."""
    settled = mature(tl.originals)
    if not settled:
        return []
    median = max(statistics.median(p.engagement for p in settled), 1)
    scored = [(p.engagement / median, p) for p in settled if len(words_of(p.text)) >= 3]
    keep = sorted(((m, p) for m, p in scored if m >= min_multiple), key=lambda mp: (-mp[0], int(mp[1].id)))
    return [Exemplar(p.id, p.url, p.text, p.engagement, round(m, 2)) for m, p in keep[:k]]


# ---------------------------------------------------------------- the accounts it engages with


@dataclass(frozen=True)
class Engagement:
    account: str  # as written in the post
    kind: str  # one of ENGAGEMENT_KINDS
    post_id: str  # the account's own post, or the post it reposted
    at: datetime


def engagements(tl: Timeline) -> list[Engagement]:
    """Every reply to, quote of, mention of and repost of another account, from the account's own posts.

    A reply's leading @handles are X's addressing, not a mention, so they are skipped; the reply itself counts as
    a reply to `replying_to`. Mentions of the account itself never count."""
    out = []
    reply_ids = {p.id for p in tl.replies}
    for p in tl.own:
        if p.id in reply_ids:
            out.append(Engagement(p.replying_to, "reply", p.id, p.created_at))
        if p.quoted_id and p.quoted_author and not _same(p.quoted_author, tl.handle):
            out.append(Engagement(p.quoted_author, "quote", p.id, p.created_at))
        body = LEADING_HANDLES.sub("", p.text) if p.replying_to else p.text
        for h in dict.fromkeys(HANDLE.findall(body)):
            if not _same(h, tl.handle):
                out.append(Engagement(h, "mention", p.id, p.created_at))
    out += [Engagement(p.author_handle, "repost", p.id, p.created_at) for p in tl.reposts]
    return out


@dataclass
class Candidate:
    handle: str
    events: int  # distinct posts of the account's that engage this one
    kinds: dict[str, int]  # posts per engagement kind
    last: datetime
    evidence: list[str]  # status ids of up to three of those posts, newest first

    def why(self) -> str:
        plural = {"reply": "replies", "quote": "quotes", "mention": "mentions", "repost": "reposts"}
        parts = [f"{n} {kind if n == 1 else plural[kind]}" for kind, n in self.kinds.items()]
        return f"{self.events} posts ({', '.join(parts)}), last {self.last.date().isoformat()}"


def rank_accounts(found: list[Engagement], exclude: tuple[str, ...] = ()) -> list[Candidate]:
    """Accounts by how many distinct posts engaged them, then by how many kinds of engagement, then by the most
    recent engagement, then by handle. Handles match ignoring case and display in their most common spelling."""
    skip = {h.lower().lstrip("@") for h in exclude}
    posts: dict[str, set[str]] = defaultdict(set)
    kinds: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    last: dict[str, datetime] = {}
    when: dict[tuple[str, str], datetime] = {}
    spelling: dict[str, Counter] = defaultdict(Counter)
    for e in found:
        key = e.account.lower().lstrip("@")
        if not key or key in skip:
            continue
        posts[key].add(e.post_id)
        kinds[key][e.kind].add(e.post_id)
        last[key] = max(last.get(key, e.at), e.at)
        when[(key, e.post_id)] = e.at
        spelling[key][e.account.lstrip("@")] += 1
    out = []
    for key, ids in posts.items():
        name = min(spelling[key].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        newest = sorted(ids, key=lambda i: (when[(key, i)], i), reverse=True)[:3]
        out.append(Candidate(name, len(ids), {k: len(kinds[key][k]) for k in ENGAGEMENT_KINDS if kinds[key][k]},
                             last[key], newest))
    return sorted(out, key=lambda c: (-c.events, -len(c.kinds), -c.last.timestamp(), c.handle.lower()))


def watchlist(ranked: list[Candidate], max_n: int = WATCH_MAX, min_events: int = WATCH_MIN_EVENTS) -> list[Candidate]:
    """The cut: candidates with at least `min_events` posts, at most `max_n` of them, in rank order."""
    return [c for c in ranked if c.events >= min_events][:max_n]
