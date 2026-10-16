import io
import json
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

import pytest

from harness.sources import x_api
from harness.sources.common import SourceError, iso_utc, now_utc
from harness.sources.x_api import XApiBackend
from harness.sources.x_common import XAccessError, XPost

FIXTURES = Path(__file__).resolve().parents[1] / "observation" / "fixtures"
TOKEN = "test-bearer-token"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
    return urllib.error.HTTPError("https://api.x.com/2/x", code, "error", msg, io.BytesIO(json.dumps(body).encode("utf-8")))


class FakeX:
    """Stands in for urlopen: answers each request with the next queued reply and records the request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)

    def call(self, i: int) -> tuple[str, dict]:
        parts = urllib.parse.urlsplit(self.requests[i].full_url)
        return parts.path, {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("test reached for the network")

    monkeypatch.setattr(x_api, "urlopen", refuse)


def install(monkeypatch, *replies) -> FakeX:
    fake = FakeX(*replies)
    monkeypatch.setattr(x_api, "urlopen", fake)
    return fake


def test_search_builds_query_and_maps_posts(monkeypatch):
    fake = install(monkeypatch, fixture("x_api_search.json"))
    since = now_utc() - timedelta(hours=24)

    posts = XApiBackend(TOKEN).search("caitlin clark OR #FeverTogether", since, 3)

    path, params = fake.call(0)
    assert path == "/2/tweets/search/recent"
    assert params == {
        "query": "(caitlin clark OR #FeverTogether) -is:retweet lang:en",
        "start_time": iso_utc(since),
        "sort_order": "recency",
        "tweet.fields": "created_at,public_metrics,author_id,in_reply_to_user_id,referenced_tweets",
        "expansions": "author_id",
        "user.fields": "username,public_metrics",
        "max_results": "10",
    }
    assert fake.requests[0].get_header("Authorization") == f"Bearer {TOKEN}"
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
            views=5400,
            author_followers=9120,
            is_reply=True,
            metrics_verified=True,
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
            author_followers=48210,
            is_reply=False,
            metrics_verified=True,
        ),
        XPost(
            id="2110855874737078349",
            text="This is the read that makes the Fever offense go. Quoting because the replay angle is perfect.",
            author="wnbanerd",
            created_at="2026-10-15T22:10:33Z",
            likes=655,
            reposts=40,
            replies=8,
            quotes=2,
            views=30120,
            author_followers=15877,
            is_reply=False,
            metrics_verified=True,
        ),
    ]
    assert posts[0].url == "https://x.com/hoopsdatagal/status/2110881577431994385"


def test_search_clamps_start_time_to_the_seven_day_window(monkeypatch):
    fake = install(monkeypatch, fixture("x_api_search_page2.json"))
    before = now_utc()

    XApiBackend(TOKEN).search("caitlin clark", before - timedelta(days=30), 10)

    start = datetime.fromisoformat(fake.call(0)[1]["start_time"].replace("Z", "+00:00"))
    assert before - timedelta(days=7) < start <= now_utc() - timedelta(days=7) + timedelta(minutes=1)


def test_search_stops_once_limit_is_reached_even_with_next_token(monkeypatch):
    fake = install(monkeypatch, fixture("x_api_search.json"))

    posts = XApiBackend(TOKEN).search("caitlin clark", now_utc() - timedelta(hours=1), 3)

    assert len(fake.requests) == 1
    assert [p.id for p in posts] == ["2110881577431994385", "2110878666585015194", "2110855874737078349"]


def test_search_follows_next_token_until_limit(monkeypatch):
    fake = install(monkeypatch, fixture("x_api_search.json"), fixture("x_api_search_page2.json"))

    posts = XApiBackend(TOKEN).search("caitlin clark", now_utc() - timedelta(hours=1), 4)

    assert len(fake.requests) == 2
    first, second = fake.call(0)[1], fake.call(1)[1]
    assert "next_token" not in first
    assert second["next_token"] == "b26v89c19zqg8o3fpzbkk3yuatvbgfw8d9m1ufxl2ac8t"
    assert second["max_results"] == "10"
    assert [p.id for p in posts] == [
        "2110881577431994385",
        "2110878666585015194",
        "2110855874737078349",
        "2110479708582840873",
    ]


def test_search_stops_when_meta_has_no_next_token(monkeypatch):
    fake = install(monkeypatch, fixture("x_api_search.json"), fixture("x_api_search_page2.json"))

    posts = XApiBackend(TOKEN).search("caitlin clark", now_utc() - timedelta(hours=1), 250)

    assert len(fake.requests) == 2
    assert fake.call(0)[1]["max_results"] == "100"
    assert fake.call(1)[1]["max_results"] == "100"
    assert len(posts) == 5


def test_search_with_no_matches_returns_empty(monkeypatch):
    install(monkeypatch, {"meta": {"result_count": 0}})

    assert XApiBackend(TOKEN).search("nothing matches this", now_utc() - timedelta(hours=1), 10) == []


def test_user_posts_looks_up_id_then_reads_timeline(monkeypatch):
    fake = install(monkeypatch, fixture("x_api_user_by_username.json"), fixture("x_api_user_tweets.json"))
    since = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)

    posts = XApiBackend(TOKEN).user_posts("@fever_film_room", since, 2)

    assert fake.call(0) == ("/2/users/by/username/fever_film_room", {"user.fields": "username,public_metrics"})
    path, params = fake.call(1)
    assert path == "/2/users/1452288739209347075/tweets"
    assert params == {
        "exclude": "retweets,replies",
        "start_time": "2026-10-14T00:00:00Z",
        "tweet.fields": "created_at,public_metrics,author_id,in_reply_to_user_id,referenced_tweets",
        "max_results": "5",
    }
    assert [(p.id, p.author, p.created_at, p.author_followers, p.likes, p.views) for p in posts] == [
        ("2110878666585015194", "fever_film_room", "2026-10-15T23:41:07Z", 48210, 2875, 98112),
        ("2110479708582840873", "fever_film_room", "2026-10-14T21:15:48Z", 48210, 1020, 41200),
    ]


def test_user_posts_paginates_with_pagination_token(monkeypatch):
    first_page = fixture("x_api_user_tweets.json")
    first_page["meta"]["next_token"] = "7140dibdnow9c7btw4b0y7yqbmkxl4rnpq5wd3c0tqpdp"
    second_page = {"meta": {"result_count": 0}}
    fake = install(monkeypatch, fixture("x_api_user_by_username.json"), first_page, second_page)

    posts = XApiBackend(TOKEN).user_posts("fever_film_room", now_utc() - timedelta(days=2), 20)

    assert fake.call(2)[1]["pagination_token"] == "7140dibdnow9c7btw4b0y7yqbmkxl4rnpq5wd3c0tqpdp"
    assert fake.call(2)[1]["max_results"] == "18"
    assert len(posts) == 2


def test_user_posts_unknown_handle_names_the_handle(monkeypatch):
    not_found = {
        "errors": [
            {
                "value": "nosuchhandle",
                "detail": "Could not find user with username: [nosuchhandle].",
                "title": "Not Found Error",
                "resource_type": "user",
                "parameter": "username",
                "resource_id": "nosuchhandle",
                "type": "https://api.x.com/2/problems/resource-not-found",
            }
        ]
    }
    install(monkeypatch, not_found)

    with pytest.raises(SourceError) as err:
        XApiBackend(TOKEN).user_posts("nosuchhandle", now_utc(), 5)

    assert str(err.value) == "x api: no X user @nosuchhandle: Not Found Error - Could not find user with username: [nosuchhandle]."


def test_401_says_the_token_was_rejected(monkeypatch):
    body = {"title": "Unauthorized", "type": "about:blank", "status": 401, "detail": "Unauthorized"}
    install(monkeypatch, http_error(401, body))

    with pytest.raises(XAccessError) as err:
        XApiBackend(TOKEN).search("caitlin clark", now_utc(), 10)

    assert str(err.value) == (
        "x api: X rejected the bearer token (HTTP 401) on recent search (GET /2/tweets/search/recent); "
        "check X_BEARER_TOKEN in .env. X said: Unauthorized"
    )
    assert TOKEN not in str(err.value)


def test_403_names_the_endpoint_the_plan_lacks(monkeypatch):
    body = {
        "title": "Client Forbidden",
        "detail": "This app is not enrolled for this endpoint.",
        "reason": "client-not-enrolled",
        "type": "https://api.x.com/2/problems/client-forbidden",
    }
    install(monkeypatch, fixture("x_api_user_by_username.json"), http_error(403, body))

    with pytest.raises(XAccessError) as err:
        XApiBackend(TOKEN).user_posts("fever_film_room", now_utc(), 5)

    assert str(err.value) == (
        "x api: X refused user posts timeline (GET /2/users/:id/tweets) (HTTP 403): this app's plan or access tier "
        "doesn't include it. X said: Client Forbidden - This app is not enrolled for this endpoint. - client-not-enrolled"
    )


def test_429_includes_the_reset_time(monkeypatch):
    body = {"title": "Too Many Requests", "detail": "Too Many Requests", "type": "about:blank", "status": 429}
    headers = {"x-rate-limit-limit": "450", "x-rate-limit-remaining": "0", "x-rate-limit-reset": "1792114339"}
    install(monkeypatch, http_error(429, body, headers))

    with pytest.raises(SourceError) as err:
        XApiBackend(TOKEN).search("caitlin clark", now_utc(), 10)

    assert str(err.value) == (
        "x api: rate limited on recent search (GET /2/tweets/search/recent) (HTTP 429); "
        "the window resets at 2026-10-16T01:32:19Z. X said: Too Many Requests"
    )


def test_429_without_reset_header_still_says_rate_limited(monkeypatch):
    install(monkeypatch, http_error(429, {"errors": [{"code": 88, "message": "Rate limit exceeded"}]}))

    with pytest.raises(SourceError) as err:
        XApiBackend(TOKEN).search("caitlin clark", now_utc(), 10)

    assert str(err.value) == "x api: rate limited on recent search (GET /2/tweets/search/recent) (HTTP 429). X said: Rate limit exceeded"


def test_429_usage_cap_is_not_reported_as_a_rate_limit(monkeypatch):
    body = {
        "title": "Usage Capped",
        "detail": "Usage cap exceeded: Monthly product cap",
        "type": "https://api.x.com/2/problems/usage-capped",
    }
    install(monkeypatch, http_error(429, body, {"x-rate-limit-reset": "1792114339"}))

    with pytest.raises(SourceError) as err:
        XApiBackend(TOKEN).search("caitlin clark", now_utc(), 10)

    assert str(err.value).startswith("x api: usage cap reached on recent search (GET /2/tweets/search/recent) (HTTP 429)")


def test_network_failure_raises_source_error(monkeypatch):
    install(monkeypatch, urllib.error.URLError("nodename nor servname provided"))

    with pytest.raises(SourceError, match=r"^x api: could not reach api\.x\.com: nodename"):
        XApiBackend(TOKEN).search("caitlin clark", now_utc(), 10)
