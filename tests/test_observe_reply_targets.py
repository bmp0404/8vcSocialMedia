"""Reply-target scoring, ranking and the snapshot store, plus the X backend selection and adapters."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.sources import reply_targets as rt
from harness.sources.common import SourceError, iso_utc
from harness.sources.x_common import XAccessError, XPost, get_backend, post_to_signal
from harness.sources.x_fx import FxXBackend

NOW = datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc)


def post(pid="1", minutes_old=30, likes=900, reposts=60, replies=30, quotes=10, author="WNBA", text=None, **kw) -> XPost:
    return XPost(
        id=pid,
        text=text or f"post {pid}",
        author=author,
        created_at=iso_utc(NOW - timedelta(minutes=minutes_old)),
        likes=likes,
        reposts=reposts,
        replies=replies,
        quotes=quotes,
        **kw,
    )


def test_engagement_sums_counts_and_is_none_when_unknown():
    assert rt.engagement(post()) == 1000
    assert rt.engagement(post(likes=None, reposts=None, replies=None, quotes=None)) is None
    assert rt.engagement(post(likes=5, reposts=None, replies=None, quotes=None)) == 5


def test_score_uses_average_rate_without_a_snapshot():
    t = rt.score_target(post(), NOW)
    assert t.age_minutes == 30.0
    assert t.engagement_per_hour == 2000.0  # 1000 engagements over 30 minutes
    assert not t.rate_from_delta
    # 2000/h * freshness 0.5**(30/60) * crowding 1/(1+30/50)
    assert t.score == pytest.approx(2000 * 0.5**0.5 * 0.625)


def test_score_uses_snapshot_delta_when_gap_is_long_enough():
    prev = rt.Snapshot(observed_at=iso_utc(NOW - timedelta(minutes=10)), engagement=800)
    t = rt.score_target(post(), NOW, prev)
    assert t.rate_from_delta
    assert t.engagement_per_hour == 1200.0  # 200 more engagements in 10 minutes


def test_score_ignores_snapshot_taken_moments_ago():
    prev = rt.Snapshot(observed_at=iso_utc(NOW - timedelta(minutes=1)), engagement=999)
    t = rt.score_target(post(), NOW, prev)
    assert not t.rate_from_delta and t.engagement_per_hour == 2000.0


def test_score_floors_age_for_brand_new_posts():
    t = rt.score_target(post(minutes_old=1, likes=3, reposts=0, replies=0, quotes=0), NOW)
    assert t.engagement_per_hour == 36.0  # 3 over a 5-minute floor, not 3 over 1 minute


def test_score_with_unknown_counts_is_zero_not_an_error():
    t = rt.score_target(post(likes=None, reposts=None, replies=None, quotes=None), NOW)
    assert t.engagement_per_hour is None and t.score == 0.0


def test_score_rejects_missing_timestamp():
    bad = post()
    bad.created_at = ""
    with pytest.raises(ValueError, match="no parseable created_at"):
        rt.score_target(bad, NOW)


def test_crowded_thread_ranks_below_equally_fast_quiet_one():
    quiet = post("quiet", likes=960, replies=10, reposts=20, quotes=10)
    crowded = post("crowded", likes=400, replies=580, reposts=10, quotes=10)
    ranked = rt.rank_targets([crowded, quiet], NOW, {}, max_age_minutes=180)
    assert [t.post.id for t in ranked] == ["quiet", "crowded"]


def test_rank_drops_replies_stale_posts_and_excluded_authors_and_dedupes():
    posts = [
        post("fresh"),
        post("fresh"),  # same post from timeline and search
        post("reply", is_reply=True),
        post("stale", minutes_old=200),
        post("mine", author="CCFanAcct"),
    ]
    ranked = rt.rank_targets(posts, NOW, {}, max_age_minutes=180, exclude_authors={"ccfanacct"})
    assert [t.post.id for t in ranked] == ["fresh"]


def test_target_signal_carries_reply_lane_metrics():
    s = rt.target_signal(rt.score_target(post(), NOW))
    assert s.kind == "reply_target" and s.source == "x_targets"
    assert s.url == "https://x.com/WNBA/status/1"
    assert s.metrics["engagement_per_hour"] == 2000.0
    assert s.metrics["age_minutes"] == 30.0
    assert s.metrics["rate_from_delta"] == 0
    assert s.metrics["likes"] == 900


def test_snapshots_round_trip_skip_unverified_and_expire(tmp_path):
    path = tmp_path / "snaps.json"
    unverified = post("grok", metrics_verified=False)
    rt.save_snapshots(path, {}, [post("a"), unverified], NOW)
    later = NOW + timedelta(hours=1)
    snaps = rt.load_snapshots(path, later)
    assert set(snaps) == {"a"} and snaps["a"].engagement == 1000
    assert rt.load_snapshots(path, NOW + rt.SNAPSHOT_TTL + timedelta(minutes=1)) == {}


def test_post_metrics_flag_unverified_counts():
    s = post_to_signal(post(views=None, metrics_verified=False), "x", "post")
    assert s.metrics["metrics_unverified"] == 1 and "views" not in s.metrics


# --- backend selection ---------------------------------------------------------------------


@pytest.fixture
def no_x_env(monkeypatch):
    for var in ("X_BEARER_TOKEN", "XAI_API_KEY", "X_BROWSER_PROFILE", "HARNESS_X_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_search_without_credentials_names_every_env_var(no_x_env):
    with pytest.raises(XAccessError, match="X_BEARER_TOKEN or X_BROWSER_PROFILE or XAI_API_KEY"):
        get_backend("search")


def test_forced_backend_that_cannot_search_says_so(no_x_env):
    no_x_env.setenv("HARNESS_X_BACKEND", "fx")
    with pytest.raises(XAccessError, match="can't search"):
        get_backend("search")


def test_unknown_backend_name_is_rejected(no_x_env):
    no_x_env.setenv("HARNESS_X_BACKEND", "nitter")
    with pytest.raises(XAccessError, match="expected auto or one of"):
        get_backend("search")


def test_auto_prefers_x_api_when_bearer_token_present(no_x_env):
    no_x_env.setenv("X_BEARER_TOKEN", "test-token")
    no_x_env.setenv("XAI_API_KEY", "test-key")
    built = []
    no_x_env.setattr("harness.sources.x_common._build", lambda name: built.append(name) or name)
    assert get_backend("search") == "api"
    assert get_backend("user_posts") == "api"


def test_auto_timelines_fall_back_to_fx_without_keys(no_x_env):
    no_x_env.setattr("harness.sources.x_common._build", lambda name: name)
    assert get_backend("user_posts") == "fx"


# --- fxtwitter adapter ----------------------------------------------------------------------


def fx_post(pid, minutes_old, **kw):
    base = dict(
        id=pid,
        text=f"fx {pid}",
        author_handle="WNBA",
        created_at=NOW - timedelta(minutes=minutes_old),
        likes=149,
        reposts=20,
        replies=4,
        quotes=1,
        views=8903,
        author_followers=951935,
        replying_to=None,
        is_repost=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeReader:
    def __init__(self, posts=None, error=None):
        self.posts, self.error = posts or [], error

    def profile_statuses(self, handle, limit=20):
        if self.error:
            raise self.error
        return self.posts


def test_fx_backend_maps_posts_and_drops_reposts_and_old_posts():
    reader = FakeReader([fx_post("1", 10), fx_post("2", 10, is_repost=True), fx_post("3", 500), fx_post("4", 5, replying_to="x")])
    got = FxXBackend(reader).user_posts("WNBA", NOW - timedelta(minutes=180), 20)
    assert [p.id for p in got] == ["1", "4"]
    p = got[0]
    assert (p.likes, p.reposts, p.replies, p.quotes, p.views, p.author_followers) == (149, 20, 4, 1, 8903, 951935)
    assert p.created_at == iso_utc(NOW - timedelta(minutes=10)) and p.metrics_verified and not p.is_reply
    assert got[1].is_reply


def test_fx_backend_turns_reader_errors_into_source_errors():
    with pytest.raises(SourceError, match="fxtwitter timeline for @WNBA: boom"):
        FxXBackend(FakeReader(error=RuntimeError("boom"))).user_posts("WNBA", NOW, 5)


def test_fx_backend_cannot_search():
    with pytest.raises(XAccessError, match="no search endpoint"):
        FxXBackend(FakeReader()).search("q", NOW, 5)


# --- the x_targets source end to end, with a fake backend -----------------------------------


class FakeBackend:
    def __init__(self, timelines=None, search_results=None, fail=()):
        self.timelines, self.search_results, self.fail = timelines or {}, search_results or [], set(fail)

    def user_posts(self, handle, since, limit):
        if handle in self.fail:
            raise SourceError(f"{handle} is down")
        return self.timelines.get(handle, [])

    def search(self, query, since, limit):
        return self.search_results


def _source_env(monkeypatch, tmp_path, backend, toml):
    cfg = tmp_path / "observe.toml"
    cfg.write_text(toml, encoding="utf-8")
    monkeypatch.setenv("HARNESS_OBSERVE_CONFIG", str(cfg))
    monkeypatch.setenv("HARNESS_X_SNAPSHOTS", str(tmp_path / "snaps.json"))
    monkeypatch.setattr(rt, "get_backend", lambda capability: backend)
    monkeypatch.setattr(rt, "now_utc", lambda: NOW)


TOML = """
[brand]
x_handle = "ccfan"
[x]
queries = ["Caitlin Clark"]
[reply_targets]
accounts = ["WNBA"]
broad_accounts = ["SportsCenter"]
keywords = ["WNBA", "Clark"]
max_age_minutes = 180
limit = 5
"""


def test_reply_target_source_filters_broad_accounts_by_keyword(monkeypatch, tmp_path):
    backend = FakeBackend(
        timelines={
            "WNBA": [post("w1", text="Finals tip Saturday")],  # topical account, no keyword needed
            "SportsCenter": [post("sc-hockey", author="SportsCenter"), post("sc-wnba", author="SportsCenter", likes=50)],
        },
        search_results=[post("s1", author="fan", likes=10), post("own", author="ccfan")],
    )
    backend.timelines["SportsCenter"][1].text = "WNBA Finals preview"
    backend.timelines["SportsCenter"][0].text = "Hockey highlight"
    backend.search_results[0].text = "Caitlin Clark 2K27 rating"
    backend.search_results[1].text = "Caitlin Clark is my whole personality"
    _source_env(monkeypatch, tmp_path, backend, TOML)
    got = rt.ReplyTargetSource().fetch("")
    assert {s.url.rsplit("/", 1)[1] for s in got} == {"w1", "sc-wnba", "s1"}
    assert all(s.kind == "reply_target" for s in got)
    assert (tmp_path / "snaps.json").exists()


def test_reply_target_source_survives_one_dead_account(monkeypatch, tmp_path, capsys):
    backend = FakeBackend(timelines={"WNBA": [post("w1")]}, fail={"SportsCenter"})
    _source_env(monkeypatch, tmp_path, backend, TOML)
    got = rt.ReplyTargetSource().fetch("")
    assert [s.url.rsplit("/", 1)[1] for s in got] == ["w1"]
    assert "@SportsCenter: SportsCenter is down" in capsys.readouterr().out


def test_reply_target_source_raises_when_every_read_fails(monkeypatch, tmp_path):
    backend = FakeBackend(fail={"WNBA", "SportsCenter"})
    backend.search = lambda *a, **kw: (_ for _ in ()).throw(SourceError("search down"))
    _source_env(monkeypatch, tmp_path, backend, TOML)
    with pytest.raises(SourceError, match="every read failed; first error: @WNBA: WNBA is down"):
        rt.ReplyTargetSource().fetch("")
