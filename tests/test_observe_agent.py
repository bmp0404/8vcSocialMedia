"""The observation agent: plan validation, per-call params, rounds, fallback and trace."""

import json
from datetime import datetime, timezone

import pytest

import harness.sources as sources_pkg
from harness.llm import MockLLM
from harness.models import Signal
from harness.sources import agent as ag
from harness.sources.base import Source
from harness.sources.common import iso_utc
from harness.sources.knobs import ObserveKnobs
from harness.sources.watch import load_watch, parse_watch

NOW = datetime(2026, 10, 15, 21, tzinfo=timezone.utc)
K = ObserveKnobs()


class NewsFeed(Source):
    """Fake Google News: one signal per query it was given."""

    name = "google_news"
    seen: list[list[str]] = []

    def fetch(self, mission):
        queries = load_watch().google_news.queries
        NewsFeed.seen.append(queries)
        return [Signal(source="google_news", text=f"story about {q}", url=f"https://n/{q}", created_at=iso_utc(NOW), kind="news") for q in queries]


class BrokenFeed(Source):
    """Fake ESPN that is down."""

    name = "espn_news"

    def fetch(self, mission):
        raise RuntimeError("espn down")


FAKE_REGISTRY = {"google_news": NewsFeed, "espn_news": BrokenFeed, "agent": ag.ObservationAgentSource}


@pytest.fixture
def fake_world(monkeypatch, tmp_path):
    cfg = tmp_path / "observe.toml"
    cfg.write_text(
        '[espn]\nleagues = ["basketball/wnba"]\n[google_news]\nqueries = ["Clark"]\n'
        '[agent]\ndefault_sources = ["google_news", "espn_news"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_OBSERVE_CONFIG", str(cfg))
    monkeypatch.setattr(sources_pkg, "REGISTRY", FAKE_REGISTRY)
    NewsFeed.seen = []


class ScriptedLLM:
    """Returns one canned plan per round."""

    def __init__(self, *plans):
        self.plans, self.prompts = list(plans), []

    def complete_json(self, system, prompt, mock=None):
        self.prompts.append(prompt)
        plan = self.plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return plan


def test_parse_calls_validates_feeds_and_params():
    data = {
        "calls": [
            {"feed": "google_news", "param": ["a", " b ", "c"], "why": "x"},
            {"feed": "nitter", "param": None, "why": "x"},
            {"feed": "espn_news", "param": ["q"], "why": "x"},
            {"feed": "google_news", "param": "not a list", "why": "x"},
            {"feed": "agent", "param": None, "why": "recursion"},
        ]
    }
    calls, notes = ag.parse_calls(data, FAKE_REGISTRY, 1, ObserveKnobs(agent_list_items=2))
    assert [(c.feed, c.param) for c in calls] == [("google_news", ["a", "b"]), ("espn_news", None), ("google_news", None)]
    assert notes == [
        "round 1: unknown feed 'nitter'",
        "round 1: espn_news takes no param; ran it with defaults",
        "round 1: google_news param must be a list of strings; ran it with defaults",
        "round 1: unknown feed 'agent'",
    ]


def test_parse_calls_caps_calls_per_round():
    data = {"calls": [{"feed": "google_news", "param": [f"q{i}"]} for i in range(9)]}
    calls, _ = ag.parse_calls(data, FAKE_REGISTRY, 1, ObserveKnobs(agent_calls_per_round=3))
    assert len(calls) == 3


def test_watch_for_swaps_only_the_called_feeds_list():
    base = parse_watch({"google_news": {"queries": ["Clark"]}, "reply_targets": {"accounts": ["WNBA"]}})
    w = ag.watch_for(ag.Call("x_targets", ["valkyries"], "", 1), base)
    assert w.reply_targets.accounts == ["valkyries"] and w.google_news.queries == ["Clark"]
    assert ag.watch_for(ag.Call("google_news", None, "", 1), base) is base


def test_agent_runs_params_then_digs_deeper_and_stops(fake_world):
    llm = ScriptedLLM(
        {"calls": [{"feed": "google_news", "param": ["Boston surgery"], "why": "injury news"}, {"feed": "espn_news", "param": None, "why": "x"}]},
        {"calls": [{"feed": "google_news", "param": ["Boston timeline"], "why": "dig in"}]},
    )
    signals, calls, notes = ag.run_agent(llm, "mission", "lessons", NOW, K)
    assert NewsFeed.seen == [["Boston surgery"], ["Boston timeline"]]
    assert [(c.round, c.feed, c.count, bool(c.error)) for c in calls] == [
        (1, "google_news", 1, False),
        (1, "espn_news", 0, True),
        (2, "google_news", 1, False),
    ]
    assert calls[1].error == "espn down"
    assert {s.text for s in signals} == {"story about Boston surgery", "story about Boston timeline"}
    # round two saw round one's results, including the failure
    assert "ROUND 1 RESULTS" in llm.prompts[1] and "espn down" in llm.prompts[1]
    # the brand's own config is untouched outside the agent's calls
    assert load_watch().google_news.queries == ["Clark"]


def test_agent_falls_back_to_default_plan_when_planner_fails(fake_world):
    llm = ScriptedLLM(RuntimeError("gateway down"), {"calls": []})
    signals, calls, notes = ag.run_agent(llm, "m", "l", NOW, K)
    assert [c.feed for c in calls] == ["google_news", "espn_news"]
    assert NewsFeed.seen == [["Clark"]]
    assert notes[0].startswith("round 1: planning failed (gateway down); ran the default plan")


def test_agent_with_mock_llm_runs_default_plan_once(fake_world):
    signals, calls, _ = ag.run_agent(MockLLM(), "m", "l", NOW, K)
    assert [(c.round, c.feed) for c in calls] == [(1, "google_news"), (1, "espn_news")]


def test_agent_source_writes_trace(fake_world, monkeypatch, tmp_path):
    trace = tmp_path / "trace.json"
    monkeypatch.setenv("HARNESS_OBSERVE_TRACE", str(trace))
    monkeypatch.setattr(ag, "get_llm", lambda: MockLLM())
    got = ag.ObservationAgentSource().fetch("m")
    assert [s.text for s in got] == ["story about Clark"]
    data = json.loads(trace.read_text(encoding="utf-8"))
    assert [c["feed"] for c in data["calls"]] == ["google_news", "espn_news"]
    assert data["calls"][1]["error"] == "espn down"


def test_catalog_lists_tools_with_params_and_defaults(fake_world):
    feeds = ag.catalog(load_watch(), FAKE_REGISTRY)
    assert feeds == [
        {"feed": "google_news", "reads": "Fake Google News: one signal per query it was given.", "param": "queries", "default": ["Clark"]},
        {"feed": "espn_news", "reads": "Fake ESPN that is down."},
    ]


def test_unavailable_names_feeds_that_would_only_raise(monkeypatch):
    monkeypatch.setattr(ag, "_can_search", lambda: "no X read access")
    w = parse_watch({"google_news": {"queries": ["Clark"]}, "x": {"queries": ["Clark"]}, "reply_targets": {"accounts": ["WNBA"]}})
    registry = {name: object for name in ["espn_news", "google_news", "x", "x_brand", "x_targets", "bluesky"]}
    assert ag.unavailable(w, registry) == {
        "espn_news": "no [espn] leagues",
        "bluesky": "no [bluesky] queries",
        "x": "no X read access",
        "x_brand": "no [brand] x_handle",
    }


def test_parse_calls_drops_unavailable_feeds_and_repeats():
    done = [ag.Call("google_news", ["a"], "", 1)]
    data = {"calls": [{"feed": "google_news", "param": ["a"]}, {"feed": "google_news", "param": ["b"]}, {"feed": "espn_news"}]}
    calls, notes = ag.parse_calls(data, FAKE_REGISTRY, 2, K, skip={"espn_news": "no [espn] leagues"}, done=done)
    assert [(c.feed, c.param) for c in calls] == [("google_news", ["b"])]
    assert notes == ["round 2: skipped a repeat of google_news ['a']", "round 2: espn_news can't run (no [espn] leagues)"]


def test_default_plan_is_every_configured_feed_when_toml_names_none(monkeypatch, tmp_path):
    cfg = tmp_path / "observe.toml"
    cfg.write_text('[google_news]\nqueries = ["Clark"]\n', encoding="utf-8")
    monkeypatch.setenv("HARNESS_OBSERVE_CONFIG", str(cfg))
    monkeypatch.setattr(sources_pkg, "REGISTRY", FAKE_REGISTRY)
    NewsFeed.seen = []
    signals, calls, notes = ag.run_agent(MockLLM(), "m", "l", NOW, K)
    assert [c.feed for c in calls] == ["google_news"]  # espn_news has no leagues, so it's left out
    assert notes == ["left out espn_news: no [espn] leagues"]
