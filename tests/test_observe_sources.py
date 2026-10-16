"""Keyless sources, shared helpers and the observe config, tested offline against saved responses."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.sources.common import SourceError, iso_utc, parse_time
from harness.sources.espn import game_signals, news_signals
from harness.sources.google_news import rss_signals
from harness.sources.watch import DEFAULT_PATH, load_watch, parse_watch

FIXTURES = Path(__file__).resolve().parent.parent / "observation" / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-10-16T01:12:45Z", "2026-10-16T01:12:45Z"),
        ("2026-10-17T19:30Z", "2026-10-17T19:30:00Z"),  # ESPN scoreboard drops seconds
        ("Thu, 15 Oct 2026 20:58:22 GMT", "2026-10-15T20:58:22Z"),  # RSS pubDate
        ("2026-10-15T15:58:22-05:00", "2026-10-15T20:58:22Z"),
        (1792099451, "2026-10-15T21:24:11Z"),
        ("1792099451", "2026-10-15T21:24:11Z"),
    ],
)
def test_parse_time_formats(raw, expected):
    assert iso_utc(parse_time(raw)) == expected


@pytest.mark.parametrize("raw", [None, "", "yesterday-ish"])
def test_parse_time_rejects_rather_than_guesses(raw):
    assert parse_time(raw) is None


def test_espn_news_maps_articles_and_drops_headless_ones():
    signals = news_signals(json.loads(_fixture("espn_wnba_news.json")), "basketball/wnba")
    assert len(signals) == 3
    first = signals[0]
    assert first.source == "espn_news" and first.kind == "news"
    assert first.text.startswith("Dream, Allisha Gray hopeful she'll be available for Game 1\n")
    assert first.url == "https://www.espn.com/wnba/story/_/id/50192787/wnba-finals-game-1-dream-valkyries-allisha-gray-finger"
    assert first.created_at == "2026-10-16T01:12:45Z"
    assert first.metrics == {}


def test_espn_scoreboard_marks_live_games_and_scores():
    pre, live = game_signals(json.loads(_fixture("espn_wnba_scoreboard.json")), "basketball/wnba")
    assert pre.kind == "game" and pre.created_at == "2026-10-17T19:30:00Z"
    assert pre.metrics == {"game_live": 0, "game_final": 0, "score_GS": 0, "score_ATL": 0}
    assert "WNBA Finals - Game 1" in pre.text
    assert pre.url == "https://www.espn.com/wnba/game/_/gameId/401918305/dream-valkyries"
    assert live.metrics["game_live"] == 1
    assert live.metrics["score_GS"] == 61 and live.metrics["score_ATL"] == 58


def test_google_news_strips_publisher_suffix_and_parses_dates():
    signals = rss_signals(_fixture("google_news_rss.xml"), '"Caitlin Clark"')
    assert len(signals) == 3
    s = signals[0]
    assert s.text == "Caitlin Clark Sends Clear Message After Aliyah Boston Announcement"
    assert s.author == "Athlon Sports"
    assert s.created_at == "2026-10-15T20:58:22Z"
    assert s.url.startswith("https://news.google.com/rss/articles/")
    assert s.kind == "news"


def test_google_news_raises_on_bad_xml():
    with pytest.raises(SourceError, match="not valid XML"):
        rss_signals("<rss><channel>", "q")


def test_repo_observe_config_loads():
    watch = load_watch(DEFAULT_PATH)
    assert "basketball/wnba" in watch.espn.leagues
    assert watch.google_news.queries and watch.x.queries and watch.reply_targets.accounts


def test_parse_watch_strips_at_sign_from_handle():
    assert parse_watch({"brand": {"x_handle": "@ccfan"}}).brand_handle == "ccfan"


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"espn": {"league": ["x"]}}, r"unknown keys in \[espn\]"),
        ({"twitter": {}}, "unknown sections"),
        ({"brand": {"handle": "x"}}, r"\[brand\] only takes x_handle"),
    ],
)
def test_parse_watch_rejects_typos(raw, message):
    with pytest.raises(SourceError, match=message):
        parse_watch(raw)


def test_load_watch_names_missing_file(tmp_path):
    with pytest.raises(SourceError, match="observe config not found"):
        load_watch(tmp_path / "nope.toml")


def test_iso_utc_converts_to_utc():
    from datetime import timedelta

    dt = datetime(2026, 10, 15, 15, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert iso_utc(dt) == "2026-10-15T20:00:00Z"
