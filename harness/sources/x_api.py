"""X API v2 client + source plugin (stdlib only).

Auth: app-only bearer token from env X_BEARER_TOKEN. Every endpoint used here
(recent search, user lookup, user timeline, post lookup) accepts app-only auth,
so the OAuth 1.0a keys (X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN /
X_ACCESS_TOKEN_SECRET) are not read.

Env:
  X_BEARER_TOKEN   required
  X_SEARCH_QUERY   full search query (overrides everything below)
  X_SEARCH_KEYWORDS comma-separated keywords (overrides the targets file's keywords)
  X_QUERY_MODE     what the default query is built from: both | keywords | accounts (default both).
                   keywords = the targets file's `keywords`; accounts = `from:` its priority handles
  X_QUERY_MAX_LEN  max query length; terms past it are dropped (default 512, X's basic-tier limit)
  X_MAX_RESULTS    cap on signals returned by the source (default 25)

The targets file is HARNESS_TARGETS (default `<mission stem>_targets.json`; see config.targets()).

Docs: https://docs.x.com/x-api  (recent search max_results 10-100; user
timeline 5-100; post lookup up to 100 ids; pay-per-use credits).
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from harness import config
from harness.models import Signal
from harness.sources.base import Source

BASE_URL = "https://api.x.com/2"
TIMEOUT = 20

TWEET_FIELDS = "public_metrics,created_at,conversation_id,author_id"
USER_FIELDS = "username,public_metrics"


class XAPIError(RuntimeError):
    """An X API call failed. Carries endpoint, HTTP status and rate-limit reset."""

    def __init__(self, endpoint: str, status: int | None, message: str, reset: str | None = None):
        self.endpoint = endpoint
        self.status = status
        self.reset = reset
        super().__init__(message)


# --------------------------------------------------------------------------- core


def _token() -> str:
    tok = os.getenv("X_BEARER_TOKEN", "").strip()
    if not tok:
        raise XAPIError("-", None, "X_BEARER_TOKEN is not set (add it to .env)")
    return tok


def _fmt_reset(reset: str | None) -> str:
    if not reset:
        return ""
    try:
        ts = int(reset)
        when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
        return f"; rate limit resets at {when} (in {max(0, ts - int(time.time()))}s)"
    except ValueError:
        return f"; x-rate-limit-reset={reset}"


def _get(path: str, params: dict | None = None) -> dict:
    """GET {BASE_URL}{path} with bearer auth. Raises XAPIError with a clear message."""
    endpoint = f"GET /2{path}"
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{BASE_URL}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {_token()}", "User-Agent": "media-harness/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        reset = e.headers.get("x-rate-limit-reset") if e.headers else None
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except (ValueError, OSError):
            body = {}
        detail = body.get("detail") or body.get("title") or body.get("reason") or ""
        if body.get("errors") and not detail:
            detail = "; ".join(str(x.get("message") or x) for x in body["errors"])[:300]
        msg = f"X API {endpoint} -> HTTP {e.code}"
        if e.code == 401:
            msg += " Unauthorized: bearer token invalid/expired or malformed header"
        elif e.code == 402:
            msg += " Payment Required: no X API credits left on this app (pay-per-use)"
        elif e.code == 403:
            msg += " Forbidden: this app/tier is not allowed to call this endpoint"
            if "search" in path:
                msg += (
                    " (RECENT SEARCH IS NOT AVAILABLE ON THIS ACCESS TIER; free/legacy "
                    "tokens usually cannot search. Buy credits or upgrade, or use another source)"
                )
        elif e.code == 429:
            msg += " Too Many Requests: rate limit or usage cap hit"
        if detail:
            msg += f" | {detail}"
        msg += _fmt_reset(reset)
        raise XAPIError(endpoint, e.code, msg, reset) from None
    except urllib.error.URLError as e:
        raise XAPIError(endpoint, None, f"X API {endpoint} network error: {e.reason}") from None


def _users_by_id(payload: dict) -> dict[str, dict]:
    return {u["id"]: u for u in payload.get("includes", {}).get("users", [])}


def _friendly(pm: dict) -> dict:
    """public_metrics plus the short aliases the rest of the harness uses."""
    out = dict(pm)
    out.update(
        likes=pm.get("like_count", 0),
        reposts=pm.get("retweet_count", 0),
        replies=pm.get("reply_count", 0),
        quotes=pm.get("quote_count", 0),
        views=pm.get("impression_count", 0),
    )
    return out


# ----------------------------------------------------------------------- search


def build_query(keywords: list[str] | None = None, handles: list[str] | None = None) -> str:
    """X_SEARCH_QUERY if set; else `(kw1 OR "kw 2" ... OR from:h1 OR from:h2) -is:retweet lang:en`.

    Keywords come from X_SEARCH_KEYWORDS or the targets file; handles are the targets file's priority
    accounts. X_QUERY_MODE picks keywords, accounts or both; terms that would push the query past
    X_QUERY_MAX_LEN are dropped (keywords and handles interleaved, so both survive truncation).
    """
    q = os.getenv("X_SEARCH_QUERY", "").strip()
    if q:
        return q
    mode = os.getenv("X_QUERY_MODE", "both").strip().lower()
    targets = config.targets()
    if keywords is None:
        env_kw = [k.strip() for k in os.getenv("X_SEARCH_KEYWORDS", "").split(",") if k.strip()]
        keywords = env_kw or list(targets["keywords"])
    if handles is None:
        handles = [a["handle"] for a in targets["accounts"] if a.get("priority") and a.get("handle")]
    kw_terms = [f'"{k}"' if " " in k else k for k in keywords] if mode in {"both", "keywords"} else []
    from_terms = [f"from:{h.lstrip('@')}" for h in handles] if mode in {"both", "accounts"} else []
    if not kw_terms and not from_terms:
        raise XAPIError("-", None, "no X search query: set X_SEARCH_QUERY / X_SEARCH_KEYWORDS or add "
                                   "keywords / priority accounts to the targets file (HARNESS_TARGETS)")
    interleaved = [t for pair in zip(kw_terms, from_terms) for t in pair]
    n = min(len(kw_terms), len(from_terms))
    interleaved += kw_terms[n:] + from_terms[n:]
    try:
        max_len = int(os.getenv("X_QUERY_MAX_LEN", "512"))
    except ValueError:
        max_len = 512
    suffix = " -is:retweet lang:en"
    terms: list[str] = []
    for t in interleaved:
        if len(f"({' OR '.join(terms + [t])}){suffix}") > max_len and terms:
            continue
        terms.append(t)
    return f"({' OR '.join(terms)}){suffix}"


def _max_results() -> int:
    try:
        return max(1, int(os.getenv("X_MAX_RESULTS", "25")))
    except ValueError:
        return 25


def reply_score(metrics: dict, created_at: str | None, now: float | None = None) -> float:
    """Higher = more reply-worthy: engagement, author reach, and freshness."""
    eng = (
        metrics.get("like_count", 0)
        + 2 * metrics.get("retweet_count", 0)
        + 3 * metrics.get("reply_count", 0)
        + 2 * metrics.get("quote_count", 0)
    )
    reach = math.log1p(metrics.get("author_followers", 0))
    age_h = 24.0
    if created_at:
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            age_h = max(0.0, ((now or time.time()) - ts) / 3600)
        except ValueError:
            pass
    freshness = 0.35 + 0.65 * math.exp(-age_h / 12)  # half-weight after ~8h
    return round((math.log1p(eng) + 0.4 * reach) * freshness, 3)


def search_recent(query: str | None = None, max_results: int | None = None) -> list[Signal]:
    """Recent search (last 7 days) mapped to Signals, ranked most reply-worthy first."""
    cap = max_results or _max_results()
    payload = _get(
        "/tweets/search/recent",
        {
            "query": query or build_query(),
            # over-fetch so ranking has a pool to choose from (API range 10-100)
            "max_results": min(100, max(10, cap * 2)),
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": USER_FIELDS,
        },
    )
    users = _users_by_id(payload)
    signals = []
    for t in payload.get("data", []):
        u = users.get(t.get("author_id"), {})
        username = u.get("username")
        metrics = _friendly(t.get("public_metrics", {}))
        metrics["author_followers"] = u.get("public_metrics", {}).get("followers_count", 0)
        metrics["reply_score"] = reply_score(metrics, t.get("created_at"))
        signals.append(
            Signal(
                source="x",
                text=t.get("text", ""),
                id=t["id"],
                conversation_id=t.get("conversation_id"),
                url=f"https://x.com/{username or 'i/web'}/status/{t['id']}",
                author=f"@{username}" if username else None,
                created_at=t.get("created_at"),
                metrics=metrics,
            )
        )
    signals.sort(key=lambda s: s.metrics["reply_score"], reverse=True)
    return signals[:cap]


# ------------------------------------------------------------- users / timeline


def lookup_user(handle: str) -> dict:
    """{id, name, username, public_metrics} for a handle (with or without @)."""
    handle = handle.lstrip("@").strip()
    payload = _get(f"/users/by/username/{urllib.parse.quote(handle)}", {"user.fields": USER_FIELDS})
    if "data" not in payload:
        errs = payload.get("errors") or [{"detail": "not found"}]
        raise XAPIError(
            "GET /2/users/by/username", 200, f"X user @{handle} not found: {errs[0].get('detail')}"
        )
    return payload["data"]


def fetch_own_posts(
    handle: str, since_id: str | None = None, max_results: int = 100, include_replies: bool = True
) -> list[dict]:
    """Recent posts by `handle` with public_metrics, newest first.

    Returns [{id, text, created_at, conversation_id, url, public_metrics, metrics}],
    where `metrics` adds likes/reposts/replies/quotes/views aliases. Retweets are
    excluded. Pass since_id to only get posts newer than the last learn run.
    """
    user = lookup_user(handle)
    exclude = "retweets" if include_replies else "retweets,replies"
    payload = _get(
        f"/users/{user['id']}/tweets",
        {
            "max_results": min(100, max(5, max_results)),
            "since_id": since_id,
            "exclude": exclude,
            "tweet.fields": TWEET_FIELDS,
        },
    )
    username = user["username"]
    return [
        {
            "id": t["id"],
            "text": t.get("text", ""),
            "created_at": t.get("created_at"),
            "conversation_id": t.get("conversation_id"),
            "url": f"https://x.com/{username}/status/{t['id']}",
            "public_metrics": t.get("public_metrics", {}),
            "metrics": _friendly(t.get("public_metrics", {})),
        }
        for t in payload.get("data", [])
    ]


# ---------------------------------------------------------------- post lookup


def fetch_metrics(ids: list[str]) -> dict[str, dict]:
    """{tweet_id: public_metrics} for up to any number of ids (batched by 100).

    Deleted/protected ids are silently omitted.
    """
    ids = [str(i) for i in ids if i]
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 100):
        payload = _get(
            "/tweets", {"ids": ",".join(ids[start : start + 100]), "tweet.fields": "public_metrics"}
        )
        for t in payload.get("data", []):
            out[t["id"]] = t.get("public_metrics", {})
    return out


# ---------------------------------------------------------------------- plugin


class XAPISource(Source):
    """Live X posts via X API v2 recent search. Enable with HARNESS_SOURCES=x_api."""

    name = "x_api"

    def fetch(self, mission: str) -> list[Signal]:
        return search_recent()
