"""Match approved actions to the posts the bot made from them.

The bot may edit a draft before posting it: fill a [VERIFY: ...] gap, change punctuation or case,
add an emoji, append a link. So texts are compared after normalizing them, with a similarity
threshold instead of equality. A reply whose post replies to the action's target status, or a quote
whose post quotes it, is anchored to that action by X itself and needs less text agreement.

Neither threshold has been calibrated against a real bot edit; 8VC's bot hasn't posted one yet.
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
# "this team 💛". Below this many normalized characters an unanchored match must be exact.
SHORT_TEXT = 20
# A post can't come from an approval made after it. The slack covers clock skew between this
# machine (approved_at) and X (the post's Snowflake id).
CLOCK_SLACK = timedelta(minutes=5)
UNMATCHABLE_TYPES = {"follow"}  # a follow leaves no post on the timeline

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


def normalize(text: str) -> str:
    """Lowercase text without links, [VERIFY] gaps, reply @-prefixes, curly quotes or extra spaces."""
    t = unicodedata.normalize("NFKC", text or "").translate(_ASCII)
    t = _URL.sub(" ", _VERIFY.sub(" ", t))
    t = _LEADING_MENTIONS.sub("", t.strip())
    return " ".join(t.lower().split())


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def anchored(action: ApprovedAction, post: OwnPost) -> bool:
    if not action.target_status:
        return False
    if action.type == "reply":
        return post.replying_to_status == action.target_status
    if action.type == "quote":
        return post.quoted_id == action.target_status
    return False


def score(action: ApprovedAction, post: OwnPost) -> float:
    """The better similarity of the post to the approved text or, if a reviewer edited it, the draft."""
    texts = [action.text] + ([action.original_text] if action.original_text else [])
    return max(similarity(t, post.text) for t in texts)


def accepts(action: ApprovedAction, post: OwnPost, is_anchored: bool, s: float) -> bool:
    if is_anchored:
        return s >= ANCHORED_MIN
    if min(len(normalize(action.text)), len(normalize(post.text))) < SHORT_TEXT:
        return s == 1.0
    return s >= TEXT_MIN


def match(actions: list[ApprovedAction], posts: list[OwnPost]) -> list[Match]:
    """One-to-one matches, strongest first: anchored before unanchored, then by similarity."""
    candidates = []
    for a in actions:
        if a.type in UNMATCHABLE_TYPES:
            continue
        for p in posts:
            if p.created_at < a.approved_at - CLOCK_SLACK:
                continue
            is_anchored = anchored(a, p)
            s = score(a, p)
            if accepts(a, p, is_anchored, s):
                candidates.append((is_anchored, s, a.id, p.id))
    candidates.sort(reverse=True)
    used_actions, used_posts, out = set(), set(), []
    for is_anchored, s, action_id, post_id in candidates:
        if action_id in used_actions or post_id in used_posts:
            continue
        used_actions.add(action_id)
        used_posts.add(post_id)
        out.append(Match(action_id, post_id, round(s, 3), is_anchored))
    return out
