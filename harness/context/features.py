"""Features of a post's text and timing that need no API at all.

Brand-specific words live in `data/context/lexicons.json`, keyed by domain, so
the same code scores a dev-tool launch and a fan-account reply.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from harness import config

LEXICONS = config.ROOT / "data" / "context" / "lexicons.json"

_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"(?<![\w@])@\w{1,15}")
_HASHTAG = re.compile(r"(?<![\w#])#\w+")
_NUMBER = re.compile(r"\b\d[\d,.]*\s*(?:%|x|k|m|b|ms|s|gb|mb|tb)?\b", re.I)
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff\U0001f900-\U0001f9ff]"
)
_THREAD = re.compile(r"🧵|\b1/\d*\b|\(1/\d+\)|\bthread\b", re.I)
_LIST = re.compile(r"^\s*(?:\d+[.)]|[-•→✅▸·])\s", re.M)


@lru_cache(maxsize=1)
def lexicons() -> dict:
    return json.loads(LEXICONS.read_text(encoding="utf-8"))


def _has_any(text_lower: str, words: list[str]) -> bool:
    for w in words:
        if w.isalnum() or " " in w or "'" in w:
            if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text_lower):
                return True
        elif w in text_lower:
            return True
    return False


def text_features(text: str, media: str = "none", domain: str = "devtools") -> dict:
    """Deterministic text features. `media` is the dataset's Media column (none/photo/video/gif)."""
    lex = lexicons()
    urls = _URL.findall(text)
    body = _URL.sub("", text).strip()
    lower = body.lower()
    lines = [ln for ln in body.splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    words = body.split()
    letters = [c for c in body if c.isalpha()]
    feats = {
        "n_chars": len(body),
        "n_words": len(words),
        "n_lines": len(lines),
        "first_line_chars": len(first),
        # A trailing t.co link is the attached media when the post has media.
        "n_links_text": max(len(urls) - (1 if media not in ("none", "") else 0), 0),
        "n_mentions": len(_MENTION.findall(body)),
        "n_hashtags": len(_HASHTAG.findall(body)),
        "n_emoji": len(_EMOJI.findall(body)),
        "n_exclaim": body.count("!"),
        "has_question": "?" in body,
        "has_number": bool(_NUMBER.search(body)),
        "caps_ratio": round(sum(c.isupper() for c in letters) / len(letters), 3) if letters else 0.0,
        "starts_with_mention": body.startswith("@"),
        "is_thread": bool(_THREAD.search(body)),
        "has_list": len(_LIST.findall(body)) >= 2,
        "first_person_singular": bool(re.search(r"(?<!\w)(i|i'm|i've|my|me)(?!\w)", lower)),
        "first_person_plural": bool(re.search(r"(?<!\w)(we|we're|we've|our|us)(?!\w)", lower)),
        "all_lowercase": bool(letters) and all(c.islower() for c in letters),
        "media": media or "none",
        "has_media": (media or "none") != "none",
    }
    for hook, kws in lex["hooks"].items():
        feats[f"hook_{hook}"] = _has_any(lower, kws)
    feats["mentions_big_name"] = _has_any(lower, lex["domains"].get(domain, {}).get("big_names", []))
    feats["short_no_link"] = feats["n_chars"] <= 80 and feats["n_links_text"] == 0
    return feats


def time_features(created_at: datetime, audience_tz: str) -> dict:
    """Clock and calendar features in the audience's time zone, plus UTC hour."""
    local = created_at.astimezone(ZoneInfo(audience_tz))
    hour = local.hour + local.minute / 60
    if 5 <= local.hour < 9:
        part = "early_morning"
    elif 9 <= local.hour < 12:
        part = "morning"
    elif 12 <= local.hour < 17:
        part = "afternoon"
    elif 17 <= local.hour < 22:
        part = "evening"
    else:
        part = "night"
    return {
        "created_at_utc": created_at.isoformat(timespec="seconds"),
        "hour_utc": created_at.hour,
        "hour_local": local.hour,
        "hour_local_frac": round(hour, 2),
        "dow_local": local.strftime("%a"),
        "is_weekend_local": local.weekday() >= 5,
        "part_of_day_local": part,
        "minute_of_week_local": local.weekday() * 1440 + local.hour * 60 + local.minute,
        # Schedulers post at round minutes; people post at any minute (4 in 60 by chance).
        "round_minute": created_at.minute % 15 == 0,
    }


def audience_tz(domain: str) -> str:
    return lexicons()["domains"][domain]["audience_tz"]
