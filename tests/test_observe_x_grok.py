import io
import json
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

import pytest

from harness.sources import x_grok
from harness.sources.common import SourceError
from harness.sources.x_common import XAccessError, XPost
from harness.sources.x_grok import GrokXBackend

FIXTURES = Path(__file__).resolve().parents[1] / "observation" / "fixtures"
KEY = "test-xai-key"
SINCE = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)


def response() -> dict:
    return json.loads((FIXTURES / "grok_x_search_response.json").read_text(encoding="utf-8"))


def with_answer(body: dict, posts: list[dict]) -> dict:
    body["output"][-1]["content"][0]["text"] = json.dumps({"posts": posts})
    return body


class FakeResponse:
    def __init__(self, body: dict):
        self.status = 200
        self.headers = Message()
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, body: dict, headers: dict | None = None) -> urllib.error.HTTPError:
    msg = Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError(x_grok.URL, code, "error", msg, io.BytesIO(json.dumps(body).encode("utf-8")))


class FakeXai:
    """Stands in for urlopen: answers with one queued reply and keeps the request for inspection."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if isinstance(self.reply, Exception):
            raise self.reply
        return FakeResponse(self.reply)

    def payload(self) -> dict:
        return json.loads(self.requests[0].data.decode("utf-8"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("test reached for the network")

    monkeypatch.setattr(x_grok, "urlopen", refuse)
    monkeypatch.delenv(x_grok.MODEL_ENV, raising=False)


def install(monkeypatch, reply) -> FakeXai:
    fake = FakeXai(reply)
    monkeypatch.setattr(x_grok, "urlopen", fake)
    return fake


def test_search_request_uses_x_search_tool_with_from_date(monkeypatch):
    fake = install(monkeypatch, response())

    GrokXBackend(KEY).search("caitlin clark", datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc), 10)

    req = fake.requests[0]
    assert req.full_url == "https://api.x.ai/v1/responses"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == f"Bearer {KEY}"
    payload = fake.payload()
    assert payload["model"] == "grok-4.7"
    assert payload["tools"] == [{"type": "x_search", "from_date": "2026-10-15"}]
    assert payload["include"] == ["no_inline_citations"]
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    prompt = payload["input"][0]["content"]
    assert "caitlin clark" in prompt
    assert "up to 10 original posts" in prompt
    assert "2026-10-15T12:30:00Z" in prompt


def test_model_comes_from_env(monkeypatch):
    monkeypatch.setenv(x_grok.MODEL_ENV, "grok-4.6")
    fake = install(monkeypatch, response())

    GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    assert fake.payload()["model"] == "grok-4.6"


def test_search_keeps_only_cited_posts_and_maps_them(monkeypatch):
    install(monkeypatch, response())

    posts = GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    # courtside_takes/status/2110853240714168152 is in the answer but not in any citation, so it is dropped.
    assert posts == [
        XPost(
            id="2110881577431994385",
            text="@fever_film_room Watched the fourth quarter twice. The drop coverage never had an answer for it.",
            author="hoopsdatagal",
            created_at="2026-10-15T23:52:41Z",
            likes=210,
            reposts=12,
            replies=3,
            quotes=1,
            views=None,
            is_reply=True,
            metrics_verified=False,
        ),
        XPost(
            id="2110878666585015194",
            text="Clark's pull-up three off the high screen is the shot every defense is scheming for now. Film breakdown tomorrow.",
            author="fever_film_room",
            created_at="2026-10-15T23:41:07Z",
            likes=2875,
            reposts=312,
            replies=45,
            quotes=19,
            views=98112,
            is_reply=False,
            metrics_verified=False,
        ),
        XPost(
            id="2110664526394297323",
            text="Morning: the full replay of last night's game is up on the league app.",
            author="wnbanerd",
            created_at="2026-10-15T09:30:12Z",
            likes=64,
            reposts=7,
            replies=2,
            quotes=0,
            views=4100,
            is_reply=False,
            metrics_verified=False,
        ),
    ]
    assert posts[0].metrics()["metrics_unverified"] == 1


def test_since_is_applied_to_the_time_in_the_post_id(monkeypatch):
    install(monkeypatch, response())

    posts = GrokXBackend(KEY).search("caitlin clark", datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc), 10)

    assert [p.id for p in posts] == ["2110881577431994385", "2110878666585015194"]


def test_limit_truncates_newest_first(monkeypatch):
    install(monkeypatch, response())

    posts = GrokXBackend(KEY).search("caitlin clark", SINCE, 1)

    assert [p.id for p in posts] == ["2110881577431994385"]


def test_user_posts_restricts_tool_and_answer_to_the_handle(monkeypatch):
    fake = install(monkeypatch, response())

    posts = GrokXBackend(KEY).user_posts("@fever_film_room", SINCE, 5)

    assert fake.payload()["tools"] == [
        {"type": "x_search", "from_date": "2026-10-15", "allowed_x_handles": ["fever_film_room"]}
    ]
    assert "@fever_film_room's own posts" in fake.payload()["input"][0]["content"]
    assert [(p.id, p.author) for p in posts] == [("2110878666585015194", "fever_film_room")]


def test_response_without_citations_raises(monkeypatch):
    body = response()
    body.pop("citations")
    body["output"][-1]["content"][0]["annotations"] = []
    install(monkeypatch, body)

    with pytest.raises(SourceError) as err:
        GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    assert str(err.value) == (
        "grok x search: Grok listed 4 posts but its response had no citations, so none can be checked against X"
    )


def test_answer_with_no_cited_post_raises(monkeypatch):
    body = response()
    body["citations"] = ["https://x.com/i/user/1912644073896206336"]
    body["output"][-1]["content"][0]["annotations"] = []
    install(monkeypatch, body)

    with pytest.raises(SourceError) as err:
        GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    assert str(err.value) == "grok x search: none of the 4 posts Grok listed appear in its citations"


def test_empty_answer_after_searching_is_empty(monkeypatch):
    body = with_answer(response(), [])
    body.pop("citations")
    install(monkeypatch, body)

    assert GrokXBackend(KEY).search("caitlin clark", SINCE, 10) == []


def test_empty_answer_without_searching_raises(monkeypatch):
    body = with_answer(response(), [])
    body["usage"]["server_side_tool_usage_details"]["x_search_calls"] = 0
    install(monkeypatch, body)

    with pytest.raises(SourceError, match="Grok answered without running X search"):
        GrokXBackend(KEY).search("caitlin clark", SINCE, 10)


def test_incomplete_response_raises(monkeypatch):
    body = response()
    body["status"] = "incomplete"
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    install(monkeypatch, body)

    with pytest.raises(SourceError) as err:
        GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    assert str(err.value) == "grok x search: response ended incomplete: {'reason': 'max_output_tokens'}"


def test_401_says_the_key_was_rejected(monkeypatch):
    install(monkeypatch, http_error(401, {"code": "unauthenticated:bad-credentials", "error": "Incorrect API key provided."}))

    with pytest.raises(XAccessError) as err:
        GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    assert str(err.value) == (
        "grok x search: xAI rejected the API key (HTTP 401); check XAI_API_KEY in .env. "
        "xAI said: unauthenticated:bad-credentials - Incorrect API key provided."
    )
    assert KEY not in str(err.value)


def test_403_names_the_model(monkeypatch):
    body = {"code": "permission-denied", "error": "The caller does not have permission to execute the specified operation."}
    install(monkeypatch, http_error(403, body))

    with pytest.raises(XAccessError) as err:
        GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    assert str(err.value) == (
        "grok x search: xAI refused model 'grok-4.7' with X search (HTTP 403): the key's team lacks access. "
        "xAI said: permission-denied - The caller does not have permission to execute the specified operation."
    )


def test_429_says_rate_limited(monkeypatch):
    install(monkeypatch, http_error(429, {"code": "resource-exhausted", "error": "Too many requests"}, {"retry-after": "30"}))

    with pytest.raises(SourceError) as err:
        GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    assert str(err.value) == (
        "grok x search: xAI rate limited the request (HTTP 429); retry after 30s. xAI said: resource-exhausted - Too many requests"
    )


def test_other_status_names_the_model_env(monkeypatch):
    install(monkeypatch, http_error(400, {"code": "invalid-argument", "error": "Model not found: grok-9"}))
    monkeypatch.setenv(x_grok.MODEL_ENV, "grok-9")

    with pytest.raises(SourceError) as err:
        GrokXBackend(KEY).search("caitlin clark", SINCE, 10)

    assert str(err.value) == (
        "grok x search: xAI returned HTTP 400 for model 'grok-9' (set XAI_X_SEARCH_MODEL to change it). "
        "xAI said: invalid-argument - Model not found: grok-9"
    )
