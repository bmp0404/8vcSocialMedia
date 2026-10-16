"""Real replies under a target post, which the continuation writer copies the register of.

Observe's reply targets carry them in `thread` ({author, text, likes, followers}, up to 15, most
liked first; `replies` is the reply count). Only when `thread` is empty and the count says
replies exist does this read the conversation through api.fxtwitter.com: read-only, one request
per 1.1 s, no login.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

API = "https://api.fxtwitter.com"
UA = {"User-Agent": "media-harness/0.1 (read-only)"}
STATUS_ID = re.compile(r"/status/(\d+)")
LEADING_MENTIONS = re.compile(r"^(?:@\w+\s+)+")
TCO = re.compile(r"\s*https?://t\.co/\S+")
_lock = threading.Lock()
_last = [0.0]


class ThreadUnavailable(RuntimeError):
    """The conversation couldn't be read. The lane falls back to the styled writer."""


def status_id(url: str) -> str:
    m = STATUS_ID.search(url or "")
    if not m:
        raise ThreadUnavailable(f"no status id in {url!r}")
    return m.group(1)


def clean(text: str) -> str:
    """Strip the leading @mentions X prepends to replies and trailing t.co links; one line."""
    return TCO.sub("", LEADING_MENTIONS.sub("", text or "")).strip().replace("\n", " / ")


def usable(text: str) -> bool:
    return len(re.findall(r"[A-Za-z']{2,}", text)) >= 3


def fetch_replies(url: str, timeout: float = 20) -> list[dict]:
    """Direct replies to `url`, most-liked first, in Observe's thread shape: [{"author", "text", "likes", "followers"}]."""
    sid = status_id(url)
    with _lock:
        wait = 1.1 - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
    req = urllib.request.Request(f"{API}/2/conversation/{sid}", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise ThreadUnavailable(f"fxtwitter conversation {sid}: {e}") from e
    out = []
    for r in data.get("replies") or []:
        if (r.get("replying_to") or {}).get("status") != sid:
            continue
        text = clean(r.get("text", ""))
        if usable(text):
            author = r.get("author") or {}
            out.append({"text": text, "likes": r.get("likes", 0), "followers": author.get("followers"),
                        "author": author.get("screen_name")})
    return sorted(out, key=lambda r: -r["likes"])


def replies_for(target: dict, fetch=fetch_replies) -> list[dict]:
    """Replies Observe attached in `thread`, else a live read when the post has replies.
    Raises ThreadUnavailable when the read fails."""
    given = target.get("thread") or []
    if given:
        return [{"text": clean(r["text"]), "likes": r.get("likes", 0), "followers": r.get("followers"),
                 "author": r.get("author")} for r in given if usable(clean(r.get("text", "")))]
    count = target.get("replies")
    if isinstance(count, int) and count == 0:
        return []
    return fetch(target["url"])
