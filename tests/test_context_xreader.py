"""FxTwitterReader against a saved fxtwitter response, through a fake client (no network)."""

from pathlib import Path

from harness.context.http import CachedResponse
from harness.context.xreader import FxTwitterReader

FIXTURE = Path(__file__).parent / "fixtures" / "fx_profile_with_replies.json"


class FakeClient:
    def __init__(self, body: str):
        self.body, self.urls = body, []

    def get(self, url, max_age_s=None, headers=None, settled_after=None):
        self.urls.append(url)
        return CachedResponse(url, 200, "2026-10-16T05:00:00+00:00", self.body, True)


def reader():
    client = FakeClient(FIXTURE.read_text(encoding="utf-8"))
    return FxTwitterReader(client=client), client


def test_with_replies_requests_the_replies_tab():
    r, client = reader()
    r.profile_statuses("@CClarkReport", limit=20, with_replies=True)
    assert client.urls == ["https://api.fxtwitter.com/2/profile/CClarkReport/statuses?with_replies=1"]


def test_default_reads_the_posts_tab():
    r, client = reader()
    r.profile_statuses("CClarkReport", limit=20)
    assert client.urls == ["https://api.fxtwitter.com/2/profile/CClarkReport/statuses"]


def test_replies_and_their_parents_are_told_apart():
    r, _ = reader()
    posts = {p.id: p for p in r.profile_statuses("CClarkReport", limit=20, with_replies=True)}
    reply = posts["2106422136889856352"]
    assert (reply.author_handle, reply.replying_to, reply.replying_to_status) == (
        "CClarkReport", "CaitlinClark22", "2106419802117976116")
    parent = posts["2106419802117976116"]
    assert parent.timeline_context and not parent.is_repost and parent.author_handle == "CaitlinClark22"
    self_reply = posts["2110373599190405247"]
    assert self_reply.replying_to == "CClarkReport" and not self_reply.timeline_context
    own = [p for p in posts.values() if not p.timeline_context]
    assert {p.author_handle for p in own} == {"CClarkReport"} and len(own) == 4


def test_without_replies_flag_keeps_the_old_repost_fallback():
    r, _ = reader()
    posts = {p.id: p for p in r.profile_statuses("CClarkReport", limit=20)}
    assert posts["2106419802117976116"].is_repost and not posts["2106419802117976116"].timeline_context
