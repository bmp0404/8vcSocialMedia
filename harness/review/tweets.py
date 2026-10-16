"""Post data for the page: `GET /api/tweet/<status_id>` and `GET /api/account/<handle>`.

A post comes from X's syndication endpoint, the one `react-tweet` reads, so the page renders it with
react-tweet's own components. Responses are cached under ~/.cache/media-harness/tweets/ and every request
waits its turn in the shared per-host throttle (`harness/context/http.py`), one per second, as the
fxtwitter reads do. When syndication has nothing, the route also returns what the harness stored about
the post (a hot file, a label item, or a round's draft and trace), and the page builds a `Tweet` from it
(`web/src/tweet.ts`). The brand account's name and avatar come from fxtwitter, so a draft renders as a
post from that account.
"""

import json
import math
import os
import re
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from harness import config, memory
from harness.context.http import FetchError, PoliteClient
from harness.outcomes.handoff import status_id
from harness.review import hot
from harness.review.routes import HTTPError, Request, route

CACHE_DIR = Path(os.getenv("HARNESS_TWEET_CACHE") or Path.home() / ".cache" / "media-harness" / "tweets")
SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result"
FXTWITTER = "https://api.fxtwitter.com"
# The feature flags react-tweet sends (packages/react-tweet/src/api/fetch-tweet.ts), so X answers in the
# shape its components expect.
FEATURES = ";".join([
    "tfw_timeline_list:", "tfw_follower_count_sunset:true", "tfw_tweet_edit_backend:on", "tfw_refsrc_session:on",
    "tfw_fosnr_soft_interventions_enabled:on", "tfw_show_birdwatch_pivots_enabled:on",
    "tfw_show_business_verified_badge:on", "tfw_duplicate_scribes_to_settings:on",
    "tfw_use_profile_image_shape_enabled:on", "tfw_show_blue_verified_badge:on", "tfw_legacy_timeline_sunset:true",
    "tfw_show_gov_verified_badge:on", "tfw_show_business_affiliate_badge:on", "tfw_tweet_edit_frontend:on",
])
# Cache lifetimes, chosen without measurement. A post's counts move while it is young, and a reply card shows
# its reply count, so a found post is refetched after 10 minutes. A missing post is retried after an hour.
FOUND_TTL_S, MISSING_TTL_S, ACCOUNT_TTL_S = 600, 3600, 86400
STATUS_ID = re.compile(r"\d{1,25}")
HANDLE = re.compile(r"\w{1,15}")
_RADIX_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

_client = PoliteClient(min_interval_s=1.0, cache_dir=None, timeout_s=8.0, max_retries=1)


# ---------------------------------------------------------------- the syndication token


def js_radix(value: float, radix: int = 36) -> str:
    """`Number.prototype.toString(radix)` as V8 computes it for a non-negative double (DoubleToRadixCString
    in src/numbers/conversions.cc): fraction digits up to the double's precision, rounded half to even."""
    integer = math.floor(value)
    fraction = value - integer
    delta = max(0.5 * (math.nextafter(value, math.inf) - value), math.nextafter(0.0, 1.0))
    digits: list[int] = []
    if fraction >= delta:
        while True:
            fraction *= radix
            delta *= radix
            digit = int(fraction)
            digits.append(digit)
            fraction -= digit
            if (fraction > 0.5 or (fraction == 0.5 and digit & 1)) and fraction + delta > 1:
                while True:  # round up, carrying through trailing digits that are already radix - 1
                    if not digits:
                        integer += 1
                        break
                    last = digits.pop()
                    if last + 1 < radix:
                        digits.append(last + 1)
                        break
                break
            if fraction < delta:
                break
    head = []
    while integer / radix >= 2**53:  # digits a double can't represent are written as 0
        integer /= radix
        head.append("0")
    while True:
        remainder = math.fmod(integer, radix)
        head.append(_RADIX_DIGITS[int(remainder)])
        integer = (integer - remainder) / radix
        if integer <= 0:
            break
    text = "".join(reversed(head))
    return text + ("." + "".join(_RADIX_DIGITS[d] for d in digits) if digits else "")


def token(status_id: str) -> str:
    """react-tweet's getToken: ((id / 1e15) * PI).toString(36) without its zeros and point."""
    return re.sub(r"0+|\.", "", js_radix((int(status_id) / 1e15) * math.pi))


# ---------------------------------------------------------------- what the harness stored about a post


def _fields(sid: str, author, text, created_at=None, counts: dict | None = None, followers=None, reply_to=None) -> dict:
    counts = counts or {}
    return {"status_id": sid, "author": author or "", "text": text or "", "created_at": created_at,
            "likes": counts.get("likes"), "replies": counts.get("replies"), "reposts": counts.get("reposts"),
            "quotes": counts.get("quotes"), "author_followers": followers, "in_reply_to": reply_to}


def _from_hot(sid: str) -> dict | None:
    path = hot.hot_dir() / f"{sid}.json"
    if not path.is_file():
        return None
    try:
        t = hot.load(path).get("target") or {}
    except hot.HotFileError:
        return None  # the queue names the broken file; here it just isn't a copy of the post
    return _fields(sid, t.get("author"), t.get("text"), t.get("created_at"), t, t.get("author_followers"))


def _from_labels(sid: str) -> dict | None:
    try:
        from harness.label.store import default_store
    except ImportError:  # the label store comes with sam/corpus-clean
        return None
    try:
        item = default_store().items([f"x:{sid}"])[0]
    except (KeyError, FileNotFoundError):
        return None
    ctx = item.get("context") or {}
    return _fields(sid, item.get("author"), item.get("text"), item.get("created_at"), item.get("metrics"),
                   item.get("author_followers"), ctx.get("parent_author"))


def _from_rounds(sid: str) -> dict | None:
    """A round's draft that answers the post, with the post's counts from that round's trace when it has one."""
    with memory.connect() as conn:
        rows = conn.execute("SELECT run_id, data FROM actions WHERE data LIKE ? ORDER BY run_id DESC",
                            (f"%/status/{sid}%",)).fetchall()
    row = next((r for r in rows if status_id(json.loads(r["data"]).get("target_url")) == sid), None)
    if row is None:
        return None
    a = json.loads(row["data"])
    found = _fields(sid, a.get("target_handle"), a.get("target_text"))
    trace = config.OUT_DIR / f"trace-run{row['run_id']}.json"
    if trace.is_file():
        data = json.loads(trace.read_text(encoding="utf-8"))
        for t in [t for n in data.get("narratives") or [] for t in n.get("reply_targets") or []]:
            if status_id(t.get("url")) == sid:
                return _fields(sid, t.get("author") or found["author"], t.get("text") or found["text"],
                               t.get("created_at"), t, t.get("author_followers"))
        for s in data.get("signals") or []:
            if status_id(s.get("url")) == sid:
                return _fields(sid, s.get("author") or found["author"], s.get("text") or found["text"],
                               s.get("created_at"), s.get("metrics"))
    return found


def stored(sid: str) -> dict | None:
    for find in (_from_hot, _from_labels, _from_rounds):
        if (found := find(sid)) is not None:
            return found
    return None


# ---------------------------------------------------------------- the cache and the fetch


def _read_cache(path: Path) -> tuple[dict, float] | None:
    """(the cached record, its age in seconds), or None when absent or unreadable; a torn file is a miss."""
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        return rec, time.time() - datetime.fromisoformat(rec["fetched_at"]).timestamp()
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _write_cache(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, path)  # a reader in another thread never sees half a file


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def syndication(sid: str, get=None) -> dict:
    """{"result": "ok"|"not_found"|"tombstone", "data": Tweet|None} from cache or X. Raises FetchError when X
    can't be reached and nothing is cached. `get` replaces the HTTP GET in tests."""
    path = CACHE_DIR / f"{sid}.json"
    hit = _read_cache(path)
    cached = hit[0] if hit else None
    if hit and hit[1] <= (FOUND_TTL_S if cached["result"] == "ok" else MISSING_TTL_S):
        return cached
    url = f"{SYNDICATION}?id={sid}&lang=en&features={FEATURES}&token={token(sid)}"
    try:
        body = (get or (lambda u: _client.get(u).body))(url)
        data = json.loads(body)
    except FetchError as e:
        if isinstance(e.__cause__, urllib.error.HTTPError) and e.__cause__.code == 404:
            data = {}
        elif cached is not None:
            return cached  # stale beats nothing while X is unreachable
        else:
            raise
    except json.JSONDecodeError as e:
        if cached is not None:
            return cached
        raise FetchError(f"syndication answered {sid} with something other than JSON: {e}") from e
    if not isinstance(data, dict) or not data:
        rec = {"result": "not_found", "data": None}
    elif data.get("__typename") == "TweetTombstone":
        rec = {"result": "tombstone", "data": None}
    else:
        rec = {"result": "ok", "data": data}
    _write_cache(path, {"fetched_at": _now(), **rec})
    return rec


@route("GET", r"/api/tweet/(\d+)")
def get_tweet(req: Request):
    """{"data": Tweet|None, "stored": fields|None, "missing": None|"not_found"|"tombstone"|"unreachable"}.
    `stored` comes with every answer that has no data, so the page can still show the post."""
    sid = req.match[1]
    if not STATUS_ID.fullmatch(sid):
        raise HTTPError(400, f"{sid} isn't a status id")
    try:
        rec = syndication(sid)
    except FetchError:
        rec = {"result": "unreachable", "data": None}
    if rec["result"] == "ok":
        return {"data": rec["data"], "stored": None, "missing": None}
    found = stored(sid)
    if found is None and rec["result"] == "unreachable":
        raise HTTPError(502, f"X didn't answer for post {sid}, and the harness has no copy of it")
    return {"data": None, "stored": found, "missing": rec["result"]}


# ---------------------------------------------------------------- the brand account


def account(handle: str, get=None) -> dict:
    """{"handle", "name", "avatar_url", "verified"} from fxtwitter, or just the handle when it can't be read."""
    path = CACHE_DIR / f"account-{handle.lower()}.json"
    hit = _read_cache(path)
    if hit and hit[1] <= ACCOUNT_TTL_S:
        return hit[0]["data"]
    try:
        body = (get or (lambda u: _client.get(u).body))(f"{FXTWITTER}/{handle}")
        user = json.loads(body).get("user") or {}
    except (FetchError, json.JSONDecodeError, AttributeError):
        # The page draws an initial for the avatar; a stale profile is better when there is one.
        return hit[0]["data"] if hit else {"handle": handle, "name": None, "avatar_url": None, "verified": False}
    data = {"handle": user.get("screen_name") or handle, "name": user.get("name"),
            "avatar_url": user.get("avatar_url"), "verified": bool((user.get("verification") or {}).get("verified"))}
    _write_cache(path, {"fetched_at": _now(), "data": data})
    return data


@route("GET", r"/api/account/(\w+)")
def get_account(req: Request):
    handle = req.match[1]
    if not HANDLE.fullmatch(handle):
        raise HTTPError(400, f"{handle} isn't an X handle")
    return account(handle)
