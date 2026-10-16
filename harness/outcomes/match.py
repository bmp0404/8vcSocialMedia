"""Match approved actions to the posts the bot made from them.

The bot may edit a draft before posting it: fill a [VERIFY: ...] gap, change punctuation or case,
add an emoji, append a link. So texts are compared after normalizing them, with a similarity
threshold instead of equality. A reply whose post replies to the action's target status, or a quote
whose post quotes it, is anchored to that action by X itself and needs less text agreement.

None of the thresholds has been calibrated against a real bot edit; 8VC's bot hasn't posted one yet.
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from harness.outcomes.handoff import ApprovedAction
from harness.outcomes.reader import OwnPost

TEXT_MIN = 0.8  # posts and mentions, and replies or quotes whose anchor doesn't hold. Uncalibrated.
ANCHORED_MIN = 0.5  # a reply to the target status, or a quote of it. Uncalibrated.
# On short texts one changed character moves the ratio a lot: "this team 🫶" scores 0.91 against
# "this team 💛". Below this many normalized characters an unanchored match must be exact, apart
# from [VERIFY] gaps, which the bot has to fill.
SHORT_TEXT = 20
# A post can't come from an approval made after it. The slack covers clock skew between this
# machine (approved_at) and X (the post's Snowflake id).
CLOCK_SLACK = timedelta(minutes=5)
UNMATCHABLE_TYPES = {"follow"}  # a follow leaves no post on the timeline

_GAP = "\x00"
_VERIFY = re.compile(r"\[VERIFY[^\]]*\]", re.IGNORECASE)
_URL = re.compile(r"https?://\S+|\bt\.co/\S+")
_LEADING_MENTIONS = re.compile(r"^(?:@\w+\s*)+")
_ASCII = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        "…": "..."})


@dataclass(frozen=True)
class Match:
    action_id: str
    status_id: str
    score: float  # text similarity after normalizing, 0 to 1
    anchored: bool


def normalize(text: str, strip_mentions: bool = False, keep_gaps: bool = False) -> str:
    """Lowercase text without links, curly quotes or extra spaces.

    A reply's leading @mentions are X's reply prefix, so `strip_mentions` drops them for replies. A
    mention post keeps them, because the handle is what tells two mention drafts apart. [VERIFY]
    gaps are dropped, or kept as one placeholder character with `keep_gaps`.
    """
    t = unicodedata.normalize("NFKC", text or "").translate(_ASCII)
    t = _URL.sub(" ", _VERIFY.sub(f" {_GAP} " if keep_gaps else " ", t))
    if strip_mentions:
        t = _LEADING_MENTIONS.sub("", t.strip())
    return " ".join(t.lower().split())


def similarity(a: str, b: str, strip_mentions: bool = False) -> float:
    a, b = normalize(a, strip_mentions), normalize(b, strip_mentions)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def exact_but_gaps(draft: str, posted: str, strip_mentions: bool = False) -> bool:
    """True when `posted` equals `draft` after normalizing, with each [VERIFY] gap filled by anything short."""
    parts = [re.escape(p.strip()) for p in normalize(draft, strip_mentions, keep_gaps=True).split(_GAP)]
    return re.fullmatch(r"\s*.{1,40}?\s*".join(parts), normalize(posted, strip_mentions)) is not None


def anchored(action: ApprovedAction, post: OwnPost) -> bool:
    if not action.target_status:
        return False
    if action.type == "reply":
        return post.replying_to_status == action.target_status
    if action.type == "quote":
        return post.quoted_id == action.target_status
    return False


def _drafts(action: ApprovedAction) -> list[str]:
    """The approved text and, if a reviewer edited it, the mind's draft."""
    return [action.text] + ([action.original_text] if action.original_text else [])


def score(action: ApprovedAction, post: OwnPost) -> float:
    return max(similarity(t, post.text, action.type == "reply") for t in _drafts(action))


def accepts(action: ApprovedAction, post: OwnPost, is_anchored: bool, s: float) -> bool:
    if action.anchor_only:  # a hot target: the operator's own words are expected, so only the reply link counts
        return is_anchored
    if is_anchored:
        return s >= ANCHORED_MIN
    strip = action.type == "reply"
    if min(len(normalize(action.text, strip)), len(normalize(post.text, strip))) < SHORT_TEXT:
        return any(exact_but_gaps(t, post.text, strip) for t in _drafts(action))
    return s >= TEXT_MIN


def match(actions: list[ApprovedAction], posts: list[OwnPost], window: timedelta) -> list[Match]:
    """One-to-one matches between actions and posts made within `window` after their approval.

    Strongest first: anchored before unanchored, then by similarity. On a tie the earlier approval
    takes the earlier post.
    """
    candidates = []
    for a in actions:
        if a.type in UNMATCHABLE_TYPES:
            continue
        for p in posts:
            if not a.approved_at - CLOCK_SLACK <= p.created_at <= a.approved_at + window:
                continue
            is_anchored = anchored(a, p)
            s = score(a, p)
            if accepts(a, p, is_anchored, s):
                candidates.append((not is_anchored, -s, a.approved_at, a.id, p.created_at, p.id, is_anchored, s))
    candidates.sort()
    used_actions, used_posts, out = set(), set(), []
    for *_, action_id, _created, post_id, is_anchored, s in candidates:
        if action_id in used_actions or post_id in used_posts:
            continue
        used_actions.add(action_id)
        used_posts.add(post_id)
        out.append(Match(action_id, post_id, round(s, 3), is_anchored))
    return out
