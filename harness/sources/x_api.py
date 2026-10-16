"""Read X through the X API v2 with an app-only bearer token.

Docs: https://docs.x.com/x-api/posts/recent-search, https://docs.x.com/x-api/users/get-user-by-username,
https://docs.x.com/x-api/users/get-posts, https://docs.x.com/x-api/fundamentals/fields,
https://docs.x.com/x-api/fundamentals/data-dictionary, https://docs.x.com/x-api/fundamentals/rate-limits,
https://docs.x.com/x-api/fundamentals/response-codes-and-errors
"""

import json
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from email.message import Message
from urllib.request import Request, urlopen

from harness.sources.common import USER_AGENT, SourceError, iso_utc, now_utc, parse_time
from harness.sources.x_common import XAccessError, XBackend, XPost

API = "https://api.x.com/2"
SOURCE = "x api"
TIMEOUT = 20
TWEET_FIELDS = "created_at,public_metrics,author_id,in_reply_to_user_id,referenced_tweets"
USER_FIELDS = "username,public_metrics"

SEARCH = "recent search (GET /2/tweets/search/recent)"
LOOKUP = "user lookup (GET /2/users/by/username/:username)"
TIMELINE = "user posts timeline (GET /2/users/:id/tweets)"


class XApiBackend(XBackend):
    name = "api"

    def __init__(self, bearer_token: str):
        self._headers = {"Authorization": f"Bearer {bearer_token}", "User-Agent": USER_AGENT}

    def search(self, query: str, since: datetime, limit: int) -> list[XPost]:
        # Recent search only reaches back seven days and answers an older start_time with HTTP 400.
        floor = now_utc() - timedelta(days=7) + timedelta(minutes=1)
        params = {
            "query": f"({query}) -is:retweet lang:en",
            "start_time": iso_utc(max(since, floor)),
            "sort_order": "recency",
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": USER_FIELDS,
        }
        return self._paginate("/tweets/search/recent", params, limit, (10, 100), "next_token", SEARCH)

    def user_posts(self, handle: str, since: datetime, limit: int) -> list[XPost]:
        handle = handle.lstrip("@")
        found = self._get(f"/users/by/username/{urllib.parse.quote(handle)}", {"user.fields": USER_FIELDS}, LOOKUP)
        user = found.get("data")
        if not user:
            raise SourceError(f"{SOURCE}: no X user @{handle}: {_detail(found)}")
        params = {"exclude": "retweets,replies", "start_time": iso_utc(since), "tweet.fields": TWEET_FIELDS}
        path = f"/users/{user['id']}/tweets"
        return self._paginate(path, params, limit, (5, 100), "pagination_token", TIMELINE, {user["id"]: user})

    def _paginate(
        self,
        path: str,
        params: dict,
        limit: int,
        page_range: tuple[int, int],
        token_param: str,
        endpoint: str,
        users: dict | None = None,
    ) -> list[XPost]:
        lo, hi = page_range
        posts: list[XPost] = []
        token = None
        while len(posts) < limit:
            page = {**params, "max_results": min(max(limit - len(posts), lo), hi)}
            if token:
                page[token_param] = token
            body = self._get(path, page, endpoint)
            if "data" not in body and body.get("errors"):
                raise SourceError(f"{SOURCE}: {endpoint} returned an error instead of posts: {_detail(body)}")
            known = {**(users or {}), **{u["id"]: u for u in body.get("includes", {}).get("users", [])}}
            posts += [p for t in body.get("data", []) if (p := _to_post(t, known))]
            token = body.get("meta", {}).get("next_token")
            if not token or not body.get("data"):
                break
        return posts[:limit]

    def _get(self, path: str, params: dict, endpoint: str) -> dict:
        status, headers, text = _send(Request(f"{API}{path}?{urllib.parse.urlencode(params)}", headers=self._headers))
        if status != 200:
            raise _http_error(status, headers, text, endpoint)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise SourceError(f"{SOURCE}: {endpoint} did not return JSON: {text[:120]!r}") from e


def _to_post(tweet: dict, users: dict) -> XPost | None:
    user = users.get(tweet.get("author_id"))
    if not user:
        return None  # author withheld or suspended, so there is no handle to build a URL from
    created = parse_time(tweet.get("created_at"))
    if created is None:
        raise SourceError(f"{SOURCE}: post {tweet.get('id')} came back without a readable created_at")
    m = tweet.get("public_metrics", {})
    # The guides use the tweet names and the OpenAPI spec the post names; accept either.
    refs = tweet.get("referenced_tweets") or tweet.get("referenced_posts") or []
    return XPost(
        id=tweet["id"],
        text=tweet["text"],
        author=user["username"],
        created_at=iso_utc(created),
        likes=m.get("like_count"),
        reposts=m.get("retweet_count", m.get("repost_count")),
        replies=m.get("reply_count"),
        quotes=m.get("quote_count"),
        views=m.get("impression_count"),
        author_followers=user.get("public_metrics", {}).get("followers_count"),
        is_reply=bool(tweet.get("in_reply_to_user_id")) or any(r.get("type") == "replied_to" for r in refs),
        metrics_verified=True,
    )


def _http_error(status: int, headers: Message, text: str, endpoint: str) -> SourceError:
    try:
        detail = _detail(json.loads(text))
    except (json.JSONDecodeError, AttributeError):
        detail = text[:200].replace("\n", " ")
    if status == 401:
        return XAccessError(f"{SOURCE}: X rejected the bearer token (HTTP 401) on {endpoint}; check X_BEARER_TOKEN in .env. X said: {detail}")
    if status == 403:
        return XAccessError(f"{SOURCE}: X refused {endpoint} (HTTP 403): this app's plan or access tier doesn't include it. X said: {detail}")
    if status == 429:
        if "usage-capped" in text:
            return SourceError(f"{SOURCE}: usage cap reached on {endpoint} (HTTP 429): the app hit its spending limit or monthly read cap. X said: {detail}")
        reset = headers.get("x-rate-limit-reset")
        reset_at = parse_time(reset) if reset else None
        when = f"; the window resets at {iso_utc(reset_at)}" if reset_at else ""
        return SourceError(f"{SOURCE}: rate limited on {endpoint} (HTTP 429){when}. X said: {detail}")
    return SourceError(f"{SOURCE}: {endpoint} returned HTTP {status}. X said: {detail}")


def _detail(body: dict) -> str:
    """X's own words for an error, from a problem+json body or a legacy errors array."""
    parts = [body.get("title"), body.get("detail"), body.get("reason")]
    if not any(parts) and body.get("errors"):
        first = body["errors"][0]
        parts = [first.get("title"), first.get("detail") or first.get("message")]
    return " - ".join(dict.fromkeys(p for p in parts if p))[:300] or json.dumps(body)[:300]


def _send(req: Request, timeout: float = TIMEOUT) -> tuple[int, Message, str]:
    """Return (status, headers, body) for any HTTP status, so error handling can read the rate-limit headers."""
    host = urllib.parse.urlsplit(req.full_url).netloc
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers or Message(), e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise SourceError(f"{SOURCE}: could not reach {host}: {e.reason}") from e
    except TimeoutError as e:
        raise SourceError(f"{SOURCE}: {host} timed out after {timeout}s") from e
