"""Parsers and feature extractors with one correct answer, on inline fixtures (no network)."""

from datetime import datetime, timezone

from harness.context.collectors import google_trends, x_trends
from harness.context.collectors.base import Series
from harness.context.features import text_features, time_features
from harness.context.xreader import _parse

TRENDS24 = (
    '<div class=list-container><h3 class=title data-timestamp=1792112179.47>Fri Oct 16 2026 00:56:19 GMT</h3>'
    '<ol class=trend-card__list>'
    '<li><span class=trend-name><a href="https://twitter.com/search?q=Broncos" class=trend-link>Broncos</a>'
    '<span class=tweet-count data-count=""></span></span></li>'
    '<li><span class=trend-name><a href="https://twitter.com/search?q=%23911onABC" class=trend-link>#911onABC</a>'
    '<span class=tweet-count data-count=""></span></span></li>'
    "</ol></div>"
    '<div class=list-container><h3 class=title data-timestamp=1792108800>Thu Oct 15 2026</h3>'
    '<ol class=trend-card__list><li><span class=trend-name><a href="x" class=trend-link>Caitlin Clark</a>'
    "</span></li></ol></div>"
)


def test_parse_trends24_orders_lists_by_time_and_keeps_rank():
    lists = x_trends.parse_trends24(TRENDS24, "united-states")
    assert [t.at for t in lists] == [
        datetime.fromtimestamp(1792108800, tz=timezone.utc),
        datetime.fromtimestamp(1792112179.47, tz=timezone.utc),
    ]
    assert lists[1].trends == ["Broncos", "#911onABC"]
    assert x_trends.by_day(lists) == {"2026-10-16": ["Caitlin Clark", "Broncos", "#911onABC"]}


def test_google_trends_strips_the_xssi_prefix():
    assert google_trends._strip(')]}\',\n{"default": {"timelineData": []}}') == {"default": {"timelineData": []}}
    assert google_trends._strip(')]}\'\n{"widgets": []}') == {"widgets": []}


def test_series_value_at_and_baseline():
    t = [datetime(2026, 10, d, tzinfo=timezone.utc) for d in range(1, 6)]
    s = Series("x", "test", "1d", points=list(zip(t, [10.0, 20.0, 30.0, 40.0, 50.0])))
    assert s.value_at(datetime(2026, 10, 3, 12, tzinfo=timezone.utc)) == 30.0
    assert s.value_at(datetime(2026, 9, 30, tzinfo=timezone.utc)) is None
    assert s.window_sum(t[1], t[3]) == 50.0
    assert s.baseline(t[4], days=4) == 25.0


def test_time_features_use_the_audience_zone():
    created = datetime(2026, 9, 17, 13, 0, 52, tzinfo=timezone.utc)  # 06:00 PDT, a Thursday
    f = time_features(created, "America/Los_Angeles")
    assert (f["hour_utc"], f["hour_local"], f["dow_local"], f["part_of_day_local"]) == (13, 6, "Thu", "early_morning")
    assert f["round_minute"] is True
    assert f["minute_of_week_local"] == 3 * 1440 + 6 * 60


def test_text_features_discount_the_media_link():
    f = text_features("Introducing Deno 3 https://t.co/abc https://t.co/media", media="photo")
    assert f["n_links_text"] == 1 and f["hook_announce"] and f["has_media"]
    assert text_features("no media https://t.co/abc", media="none")["n_links_text"] == 1


def test_text_features_word_boundaries():
    f = text_features("We're hiring! Join the team @vercel #jobs 🚀")
    assert f["hook_hiring"] and f["n_mentions"] == 1 and f["n_hashtags"] == 1 and f["n_emoji"] == 1
    assert not text_features("Users praised the new docs")["hook_funding_deal"]  # "raised" inside "praised"
    assert text_features("We raised a Series A")["hook_funding_deal"]


def test_parse_fxtwitter_status_and_timeline_shapes():
    status = {
        "id": "2108543358197207048", "text": "We are excited", "likes": 6678, "retweets": 872, "replies": 290,
        "quotes": 618, "bookmarks": 691, "views": 951644, "replying_to": None,
        "author": {"screen_name": "deno_land", "followers": 100841, "verification": {"verified": True}},
        "raw_text": {"facets": [{"type": "url"}, {"type": "mention"}]}, "media": {"all": [{"type": "photo"}]},
    }
    p = _parse(status, "2026-10-16T01:00:00+00:00")
    assert (p.reposts, p.engagement, p.n_links, p.n_mentions, p.media_types) == (872, 8458, 1, 1, ["photo"])
    assert p.created_at == datetime(2026, 10, 9, 13, 1, 26, 92000, tzinfo=timezone.utc)  # 1791550886092 ms
    reply = dict(status, reposts=5, replying_to={"screen_name": "deno_land", "status": "1"}, reposted_by={"screen_name": "x"})
    q = _parse(reply, "2026-10-16T01:00:00+00:00")
    assert (q.reposts, q.replying_to, q.replying_to_status, q.is_repost, q.reposted_by) == (5, "deno_land", "1", True, "x")
