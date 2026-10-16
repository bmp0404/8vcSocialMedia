"""Read X through xAI's Grok and its server-side X Search tool on the Responses API.

Docs: https://docs.x.ai/developers/tools/x-search, https://docs.x.ai/developers/tools/citations,
https://docs.x.ai/developers/model-capabilities/text/structured-outputs,
https://docs.x.ai/developers/rest-api-reference/inference/responses, https://docs.x.ai/developers/models,
https://docs.x.com/fundamentals/x-ids (post ids are Snowflakes; epoch and shift from
https://github.com/twitter-archive/snowflake, IdWorker.scala)
"""

import json
import os
import re
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from email.message import Message
from urllib.request import Request, urlopen

from harness.sources.common import USER_AGENT, SourceError, iso_utc
from harness.sources.x_common import XAccessError, XBackend, XPost

URL = "https://api.x.ai/v1/responses"
SOURCE = "grok x search"
MODEL_ENV = "XAI_X_SEARCH_MODEL"
DEFAULT_MODEL = "grok-4.7"
# Grok runs several X searches server-side before it answers, so a reply takes far longer than a plain call.
TIMEOUT = 180

STATUS_URL = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/(?:i/web|([A-Za-z0-9_]{1,15}))/status(?:es)?/(\d+)")
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
SNOWFLAKE_EPOCH_MS = 1288834974657

_COUNT = {"type": ["integer", "null"]}
POSTS_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "author": {"type": "string"},
                    "text": {"type": "string"},
                    "is_reply": {"type": "boolean"},
                    "likes": _COUNT,
                    "reposts": _COUNT,
                    "replies": _COUNT,
                    "quotes": _COUNT,
                    "views": _COUNT,
                },
                "required": ["url", "author", "text", "is_reply", "likes", "reposts", "replies", "quotes", "views"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}

RULES = (
    "For each post give its status URL, the author's handle without @, the full text, whether it is a reply, "
    "and the like, repost, reply, quote and view counts X shows. Use null for a count X doesn't show. "
    "List only posts you retrieved with X search in this response, because each post is checked against your "
    "citations and any post that isn't among them is dropped."
)


class GrokXBackend(XBackend):
    name = "grok"

    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        self.model = os.getenv(MODEL_ENV) or DEFAULT_MODEL

    def search(self, query: str, since: datetime, limit: int) -> list[XPost]:
        prompt = (
            f"Search X for English posts matching this query: {query}\n\n"
            f"Return up to {limit} original posts (not reposts) created at or after {iso_utc(since)}, newest first. {RULES}"
        )
        return self._ask(prompt, since, limit)

    def user_posts(self, handle: str, since: datetime, limit: int) -> list[XPost]:
        handle = handle.lstrip("@")
        prompt = (
            f"Find @{handle}'s own posts on X created at or after {iso_utc(since)}.\n\n"
            f"Return up to {limit} of them, newest first, leaving out reposts. {RULES}"
        )
        return self._ask(prompt, since, limit, handle)

    def _ask(self, prompt: str, since: datetime, limit: int, handle: str | None = None) -> list[XPost]:
        tool = {"type": "x_search", "from_date": since.astimezone(timezone.utc).strftime("%Y-%m-%d")}
        if handle:
            tool["allowed_x_handles"] = [handle]
        payload = {
            "model": self.model,
            "input": [{"role": "user", "content": prompt}],
            "tools": [tool],
            "text": {"format": {"type": "json_schema", "name": "x_posts", "schema": POSTS_SCHEMA, "strict": True}},
            # Inline [[N]](url) markers would land inside the JSON strings; the citations list still comes back.
            "include": ["no_inline_citations"],
        }
        req = Request(URL, data=json.dumps(payload).encode("utf-8"), headers=self._headers, method="POST")
        status, headers, text = _send(req)
        if status != 200:
            raise self._http_error(status, headers, text)
        try:
            body = json.loads(text)
        except json.JSONDecodeError as e:
            raise SourceError(f"{SOURCE}: xAI did not return JSON: {text[:120]!r}") from e
        return _posts_from(body, since, limit, handle)

    def _http_error(self, status: int, headers: Message, text: str) -> SourceError:
        try:
            body = json.loads(text)
            error = body.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            detail = " - ".join(str(p) for p in (body.get("code"), message) if p)
        except (json.JSONDecodeError, AttributeError):
            detail = text[:200].replace("\n", " ")
        if status == 401:
            return XAccessError(f"{SOURCE}: xAI rejected the API key (HTTP 401); check XAI_API_KEY in .env. xAI said: {detail}")
        if status == 403:
            return XAccessError(f"{SOURCE}: xAI refused model {self.model!r} with X search (HTTP 403): the key's team lacks access. xAI said: {detail}")
        if status == 429:
            retry = headers.get("retry-after")
            when = f"; retry after {retry}s" if retry else ""
            return SourceError(f"{SOURCE}: xAI rate limited the request (HTTP 429){when}. xAI said: {detail}")
        return SourceError(f"{SOURCE}: xAI returned HTTP {status} for model {self.model!r} (set {MODEL_ENV} to change it). xAI said: {detail}")


def _posts_from(body: dict, since: datetime, limit: int, handle: str | None) -> list[XPost]:
    if body.get("status", "completed") != "completed":
        raise SourceError(f"{SOURCE}: response ended {body.get('status')}: {body.get('incomplete_details') or body.get('error')}")
    text, links = "", []
    for item in body.get("output", []):
        for block in item.get("content", []) if item.get("type") == "message" else []:
            if block.get("type") == "refusal":
                raise SourceError(f"{SOURCE}: Grok refused: {block.get('refusal')}")
            if block.get("type") == "output_text":
                text += block.get("text", "")
                links += [a.get("url") for a in block.get("annotations", [])]
    # The docs show sources in three places: a top-level citations list, a top-level sources list, and annotations.
    links += [c if isinstance(c, str) else c.get("url") for c in (body.get("citations") or []) + (body.get("sources") or [])]
    links = [url for url in links if url]
    cited = {m.group(2) for url in links if (m := STATUS_URL.search(url))}

    try:
        answer = json.loads(text)["posts"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise SourceError(f"{SOURCE}: Grok's answer wasn't the requested JSON: {text[:120]!r}") from e
    if not answer:
        calls = body.get("usage", {}).get("server_side_tool_usage_details", {}).get("x_search_calls")
        if calls == 0:
            raise SourceError(f"{SOURCE}: Grok answered without running X search, so its empty list says nothing about X")
        return []
    if not links:
        raise SourceError(f"{SOURCE}: Grok listed {len(answer)} posts but its response had no citations, so none can be checked against X")

    posts: dict[str, XPost] = {}
    verified = 0
    for raw in answer:
        match = STATUS_URL.search(raw.get("url", ""))
        if not match or match.group(2) not in cited:
            continue
        verified += 1
        post_id = match.group(2)
        url_handle = match.group(1)
        author = (url_handle if url_handle and url_handle != "i" else raw.get("author", "")).lstrip("@")
        if not HANDLE.fullmatch(author) or (handle and author.lower() != handle.lower()):
            continue
        created = _snowflake_time(post_id)
        if created < since:
            continue
        posts[post_id] = XPost(
            id=post_id,
            text=raw.get("text", ""),
            author=author,
            created_at=iso_utc(created),
            likes=raw.get("likes"),
            reposts=raw.get("reposts"),
            replies=raw.get("replies"),
            quotes=raw.get("quotes"),
            views=raw.get("views"),
            is_reply=bool(raw.get("is_reply")),
            metrics_verified=False,
        )
    if not verified:
        raise SourceError(f"{SOURCE}: none of the {len(answer)} posts Grok listed appear in its citations")
    return sorted(posts.values(), key=lambda p: int(p.id), reverse=True)[:limit]


def _snowflake_time(post_id: str) -> datetime:
    """Creation time encoded in the post id, so the timestamp comes from X rather than the model."""
    return datetime.fromtimestamp(((int(post_id) >> 22) + SNOWFLAKE_EPOCH_MS) / 1000, tz=timezone.utc)


def _send(req: Request, timeout: float = TIMEOUT) -> tuple[int, Message, str]:
    """Return (status, headers, body) for any HTTP status, so error handling can read the response."""
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
