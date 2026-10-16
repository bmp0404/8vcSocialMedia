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


def test_espn_scoreboard_emits_schedule_without_created_at():
    pre, live = game_signals(json.loads(_fixture("espn_wnba_scoreboard.json")), "basketball/wnba")
    assert pre.kind == "schedule" and pre.created_at is None
    assert pre.metrics == {
        "game_live": 0,
        "game_final": 0,
        "starts_at_unix": 1792265400,  # 2026-10-17T19:30Z
        "event_id": 401918305,
        "score_GS": 0,
        "score_ATL": 0,
    }
    assert "WNBA Finals - Game 1" in pre.text
    assert pre.url == "https://www.espn.com/wnba/game/_/gameId/401918305/dream-valkyries"
    assert live.metrics["game_live"] == 1
    assert live.metrics["score_GS"] == 61 and live.metrics["score_ATL"] == 58


def test_espn_odds_line_from_summary():
    from harness.sources.espn import odds_line

    assert odds_line(json.loads(_fixture("espn_wnba_summary_odds.json"))) == "GS -2.5, O/U 154.5"
    assert odds_line({}) == ""


def test_espn_injuries_dedupe_repeated_entries():
    from harness.sources.espn import injury_signals

    signals = injury_signals(json.loads(_fixture("espn_wnba_injuries.json")), "basketball/wnba")
    names = [s.text.split(" (")[0] for s in signals]
    assert names == ["Allisha Gray", "Matilde Villa", "Aliyah Boston", "Tyasha Harris"]
    boston = signals[2]
    assert boston.text.startswith("Aliyah Boston (Indiana Fever): Out.")
    assert boston.created_at == "2026-10-15T15:05:00Z" and boston.kind == "news"


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


def test_bluesky_maps_counts_skips_opt_outs_and_uses_link_titles():
    from harness.sources.bluesky import post_signals

    signals = post_signals(json.loads(_fixture("bluesky_search.json")))
    assert [s.author for s in signals] == ["kerithburke.bsky.social", "linker.bsky.social"]
    s = signals[0]
    assert s.kind == "social" and s.source == "bluesky"
    assert s.url == "https://bsky.app/profile/kerithburke.bsky.social/post/3mxrgv3vrzs24"
    assert s.created_at == "2026-10-13T16:21:48Z"
    assert s.metrics == {"likes": 103, "reposts": 30, "replies": 2, "quotes": 9}
    assert signals[1].text == "Finals preview video"


class FakeSeries:
    """Stands in for harness.context.collectors.base.Series: points plus a median baseline."""

    def __init__(self, points, term="Caitlin Clark", source="wikipedia"):
        self.points, self.term, self.source = points, term, source

    def baseline(self, end, days=14):
        from datetime import timedelta

        vals = sorted(v for ts, v in self.points if end - timedelta(days=days) <= ts < end)
        mid = len(vals) // 2
        return None if not vals else vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def test_attention_spike_compares_last_day_to_median():
    from datetime import timedelta

    from harness.sources.attention import attention_signal

    now = datetime(2026, 10, 15, 21, tzinfo=timezone.utc)
    daily = [(now - timedelta(days=d), 100.0) for d in range(14, 1, -1)] + [(now - timedelta(hours=3), 300.0)]
    s = attention_signal(FakeSeries(daily), now, baseline_days=14)
    assert s.kind == "attention" and s.metrics == {"spike_ratio": 3.0, "level": 300.0, "baseline": 100.0}
    assert s.text == "wikipedia attention to 'Caitlin Clark' is 3.0x its 14-day median"


def test_attention_uses_newest_bucket_when_source_lags():
    from datetime import timedelta

    from harness.sources.attention import spike

    now = datetime(2026, 10, 15, 21, tzinfo=timezone.utc)
    lagging = [(now - timedelta(days=d), 50.0) for d in range(10, 2, -1)] + [(now - timedelta(days=2), 75.0)]
    assert spike(FakeSeries(lagging), now, 14) == (1.5, 75.0, 50.0)


def test_attention_without_data_is_none():
    from harness.sources.attention import spike

    assert spike(FakeSeries([]), datetime(2026, 10, 15, tzinfo=timezone.utc), 14) is None


def test_devtools_profile_loads_and_differs():
    devtools = load_watch(DEFAULT_PATH.with_name("observe.devtools.toml"))
    assert devtools.espn.leagues == [] and "coderabbitai" in devtools.reply_targets.accounts
