"""Orient's data-driven velocity: per-signal heat, narrative velocity, reply targets, LLM fallback."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from harness import orient as o
from harness.llm import MockLLM
from harness.models import Signal
from harness.sources.common import iso_utc

NOW = datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc)


def sig(kind="news", hours_old=0.0, url=None, **metrics) -> Signal:
    return Signal(
        source="test",
        text=f"{kind} {hours_old}",
        url=url,
        author="someone",
        created_at=iso_utc(NOW - timedelta(hours=hours_old)),
        metrics=metrics,
        kind=kind,
    )


@pytest.mark.parametrize(
    "signal, expected",
    [
        (sig("news", 0), 0.6),
        (sig("news", 12), 0.3),
        (sig("game", 0, game_live=1), 1.0),
        (sig("game", -24), 0.4),  # tip-off in 24 hours
        (sig("game", 12, game_final=1), 0.4),
        (sig("post", 1, engagement_per_hour=10_000), 1.0),
        (sig("post", 1, engagement_per_hour=99), 2 / math.log10(10_001)),
        (sig("post", 1), 0.0),  # an X post with no counts is cold, not unknown
    ],
)
def test_signal_heat(signal, expected):
    assert o.signal_heat(signal, NOW) == pytest.approx(expected)


def test_signal_heat_is_none_without_timestamp():
    assert o.signal_heat(Signal(source="sample", text="x", metrics={"likes": 5}), NOW) is None


def test_engagement_rate_from_counts_when_source_gave_none():
    s = sig("post", 2, likes=150, reposts=30, replies=20, quotes=0)
    assert o.engagement_per_hour(s, NOW) == 100.0


def test_engagement_rate_prefers_source_measurement():
    s = sig("reply_target", 2, likes=150, engagement_per_hour=42)
    assert o.engagement_per_hour(s, NOW) == 42.0


@pytest.mark.parametrize(
    "heats, expected",
    [([], 0.0), ([1.0], math.log(2) / math.log(6)), ([5.0], 1.0), ([3.0, 3.0], 1.0)],
)
def test_narrative_velocity(heats, expected):
    assert o.narrative_velocity(heats) == pytest.approx(expected, abs=1e-3)


def test_more_fresh_coverage_means_higher_velocity():
    one = o.narrative_velocity([0.6])
    five = o.narrative_velocity([0.6] * 5)
    assert 0 < one < five < 1


def test_build_narratives_uses_ids_and_measured_velocity():
    signals = [
        sig("news", 1, url="https://espn.com/a"),
        sig("reply_target", 0.5, url="https://x.com/WNBA/status/9", engagement_per_hour=500),
        sig("news", 2, url="https://espn.com/a"),  # same URL twice collapses
    ]
    data = {"narratives": [{"id": "n", "title": "t", "summary": "s", "relevance": 0.9, "velocity": 0.1, "signal_ids": [0, 1, 2, 99]}]}
    (n,) = o.build_narratives(data, signals, NOW)
    assert n.signal_urls == ["https://espn.com/a", "https://x.com/WNBA/status/9"]
    assert n.velocity_basis == "data"
    heats = [o.signal_heat(s, NOW) for s in signals]
    assert n.velocity == o.narrative_velocity(heats)
    assert n.reply_targets == [
        {"url": "https://x.com/WNBA/status/9", "author": "someone", "text": "reply_target 0.5", "age_minutes": 30, "engagement_per_hour": 500.0}
    ]


def test_build_narratives_keeps_llm_velocity_when_no_timestamps():
    signals = [Signal(source="sample", text="x", url="https://x.com/a/status/1")]
    data = {"narratives": [{"id": "n", "relevance": 0.5, "velocity": 0.7, "signal_ids": [0]}]}
    (n,) = o.build_narratives(data, signals, NOW)
    assert n.velocity == 0.7 and n.velocity_basis == "llm"


def test_reply_targets_skip_news_and_sort_by_rate():
    members = [
        sig("news", 1, url="https://news.example/a"),
        sig("post", 1, url="https://x.com/a/status/1", engagement_per_hour=10),
        sig("mention", 1, url="https://x.com/b/status/2", engagement_per_hour=90),
    ]
    assert [r["url"] for r in o.reply_targets(members, NOW)] == ["https://x.com/b/status/2", "https://x.com/a/status/1"]


def test_uncited_targets_become_the_reply_lane():
    cited = sig("reply_target", 0.2, url="https://x.com/a/status/1", engagement_per_hour=50)
    loose = sig("reply_target", 0.2, url="https://x.com/b/status/2", engagement_per_hour=80)
    (n,) = o.build_narratives({"narratives": [{"id": "n", "signal_ids": [0]}]}, [cited, loose], NOW)
    lane = o.uncited_targets([n], [cited, loose], NOW)
    assert lane.id == "reply-lane"
    assert [r["url"] for r in lane.reply_targets] == ["https://x.com/b/status/2"]
    assert o.uncited_targets([n], [cited], NOW) is None


def test_orient_mock_ranks_by_relevance_times_measured_velocity_and_hides_own_posts():
    signals = [
        sig("own_post", 0.1, url="https://x.com/me/status/0", likes=2),
        sig("news", 40, url="https://n/old"),
        sig("news", 0.5, url="https://n/new"),
    ]
    narratives = o.orient(MockLLM(), "mission", signals, top=5, now=NOW)
    # own_post is never offered to the LLM; newest first, so the fresh story is id 0
    assert [n.signal_urls for n in narratives] == [["https://n/new"], ["https://n/old"]]
    assert all(n.velocity_basis == "data" for n in narratives)
