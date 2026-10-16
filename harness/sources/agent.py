"""The observation agent: an LLM chooses which feeds to read this round, and with what queries.

This is the "Obs Agent" row of the whiteboard. Each source is a tool. Round one plans from the
mission and lessons; round two reads a summary of what round one found and may dig deeper,
for example more queries about a story that turned out to be hot, or stop. The default plan,
`[agent] default_sources` in the brand TOML or else every feed the brand has configured, is what
it runs with the mock provider and falls back to when the LLM fails. Feeds that can't run, such
as X search without a key, are left out of the tool list so the agent doesn't spend calls on them. Every call and its reason goes into a trace file, so when eval blames observe
for a bad action, the trace shows whether the agent skipped the feed that had the story.

Enable it with HARNESS_SOURCES=agent. It plans with the provider in HARNESS_PROVIDER; with the
mock provider it runs the default plan.
"""

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from harness import config
from harness.llm import LLM, get_llm
from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, iso_utc, now_utc
from harness.sources.knobs import ObserveKnobs, current
from harness.sources.watch import Watch, load_watch, use
from harness.sources.x_common import XAccessError, get_backend

# Feeds whose default list the agent may replace for one call: feed -> (TOML section, field).
PARAMS = {
    "google_news": ("google_news", "queries"),
    "bluesky": ("bluesky", "queries"),
    "x": ("x", "queries"),
    "x_targets": ("reply_targets", "accounts"),
    "attention": ("attention", "terms"),
}
# Never offered as tools: the agent itself, offline fixtures, and a source that always 403s.
NOT_TOOLS = {"agent", "sample", "reddit"}

SYSTEM = """You decide which feeds the observe stage reads this round for the brand in the mission.
Each call costs requests, and X reads can cost money, so choose the feeds whose results could
change what the account does next. Use only feed names from FEEDS. If a feed has a `param`, you
may pass a list for it that replaces the default for that one call; give null to use the default.
Say in `why` what you expect the call to find."""

PLAN = """MISSION:
{mission}

LESSONS FROM PAST ROUNDS:
{lessons}

FEEDS:
{feeds}

{results}Return at most {max_calls} calls{stop}.
Return JSON: {{"calls": [{{"feed": "...", "param": null, "why": "..."}}]}}"""


@dataclass
class Call:
    feed: str
    param: list[str] | None
    why: str
    round: int
    count: int = 0
    error: str = ""
    top: list[str] = field(default_factory=list)


def _can_search() -> str | None:
    try:
        get_backend("search")
    except XAccessError as e:
        return str(e)
    return None


def unavailable(watch: Watch, registry: dict) -> dict[str, str]:
    """Feeds that would only raise this round, with the reason. Checked before planning."""
    no_search = _can_search()
    reasons = {
        "espn_news": None if watch.espn.leagues else "no [espn] leagues",
        "espn_scores": None if watch.espn.scoreboard_leagues else "no [espn] scoreboard_leagues",
        "espn_injuries": None if watch.espn.scoreboard_leagues else "no [espn] scoreboard_leagues",
        "google_news": None if watch.google_news.queries else "no [google_news] queries",
        "bluesky": None if watch.bluesky.queries else "no [bluesky] queries",
        "attention": None if watch.attention.terms else "no [attention] terms",
        "x": no_search or (None if watch.x.queries else "no [x] queries"),
        "x_brand": None if watch.brand_handle else "no [brand] x_handle",
        "x_targets": None
        if watch.reply_targets.accounts or watch.reply_targets.broad_accounts or (watch.x.queries and not no_search)
        else "no watched accounts and no X search",
    }
    return {name: why for name, why in reasons.items() if why and name in registry}


def catalog(watch: Watch, registry: dict, skip: dict[str, str] | None = None) -> list[dict]:
    """One entry per usable tool: name, what it reads (its docstring's first line), param and default."""
    feeds = []
    for name, cls in registry.items():
        if name in NOT_TOOLS or name in (skip or {}):
            continue
        doc = (cls.__doc__ or "").strip().splitlines()
        entry = {"feed": name, "reads": doc[0] if doc else name}
        if name in PARAMS:
            section, attr = PARAMS[name]
            entry["param"] = attr
            entry["default"] = getattr(getattr(watch, section), attr)
        feeds.append(entry)
    return feeds


def parse_calls(
    data: dict,
    registry: dict,
    round_no: int,
    k: ObserveKnobs,
    skip: dict[str, str] | None = None,
    done: list["Call"] | None = None,
) -> tuple[list[Call], list[str]]:
    """Validate the LLM's plan. Returns the usable calls and a note for each one dropped.

    Drops unknown feeds, feeds in `skip` (they can't run), and exact repeats of a call in `done`.
    """
    calls, notes = [], []
    seen = {(c.feed, tuple(c.param or ())) for c in done or []}
    for raw in (data.get("calls") or [])[: k.agent_calls_per_round]:
        feed = raw.get("feed")
        if feed not in registry or feed in NOT_TOOLS:
            notes.append(f"round {round_no}: unknown feed {feed!r}")
            continue
        if feed in (skip or {}):
            notes.append(f"round {round_no}: {feed} can't run ({skip[feed]})")
            continue
        param = raw.get("param")
        if param is not None:
            if feed not in PARAMS:
                notes.append(f"round {round_no}: {feed} takes no param; ran it with defaults")
                param = None
            elif not isinstance(param, list) or not all(isinstance(p, str) and p.strip() for p in param):
                notes.append(f"round {round_no}: {feed} param must be a list of strings; ran it with defaults")
                param = None
            else:
                param = [p.strip() for p in param][: k.agent_list_items]
        key = (feed, tuple(param or ()))
        if key in seen:
            notes.append(f"round {round_no}: skipped a repeat of {feed}{' ' + str(param) if param else ''}")
            continue
        seen.add(key)
        calls.append(Call(feed, param, str(raw.get("why", "")), round_no))
    return calls, notes


def watch_for(call: Call, base: Watch) -> Watch:
    """The brand's watch with this call's param swapped in."""
    if call.param is None:
        return base
    section, attr = PARAMS[call.feed]
    return replace(base, **{section: replace(getattr(base, section), **{attr: call.param})})


def summarize(calls: list[Call]) -> str:
    rows = [{"feed": c.feed, "param": c.param, "count": c.count, "error": c.error or None, "top": c.top} for c in calls]
    return json.dumps(rows, ensure_ascii=False)


def run_agent(
    llm: LLM, mission: str, lessons: str, now: datetime | None = None, knobs: ObserveKnobs | None = None
) -> tuple[list[Signal], list[Call], list[str]]:
    """Plan, read and optionally dig deeper. Returns the signals, the calls made and planning notes."""
    from harness.orient import signal_heat  # orient imports this package; import late to avoid a cycle
    from harness.sources import REGISTRY, RUN_LAST

    k = knobs or current()
    now = now or now_utc()
    base = load_watch()
    skip = unavailable(base, REGISTRY)
    tools = catalog(base, REGISTRY, skip)
    feeds = json.dumps(tools, ensure_ascii=False)
    planned = base.agent.default_sources or [t["feed"] for t in tools]
    defaults = [f for f in planned if f in REGISTRY and f not in NOT_TOOLS and f not in skip]
    signals: dict[tuple, Signal] = {}
    done: list[Call] = []
    notes: list[str] = [f"left out {name}: {why}" for name, why in skip.items()]

    for round_no in range(1, k.agent_rounds + 1):
        first = round_no == 1

        def mock():
            # Offline: round one runs the default plan, round two stops.
            return {"calls": [{"feed": f, "param": None, "why": "default plan"} for f in defaults] if first else []}

        prompt = PLAN.format(
            mission=mission,
            lessons=lessons,
            feeds=feeds,
            results="" if first else f"ROUND {round_no - 1} RESULTS:\n{summarize(done)}\n\n",
            max_calls=k.agent_calls_per_round,
            stop="" if first else ' that dig into these results, or {"calls": []} if more reading would not change the next decision',
        )
        try:
            data = llm.complete_json(SYSTEM, prompt, mock=mock)
            calls, dropped = parse_calls(data, REGISTRY, round_no, k, skip, done)
            notes += dropped
        except Exception as e:  # noqa: BLE001 - a planning failure falls back to the static plan
            notes.append(f"round {round_no}: planning failed ({e}); {'ran the default plan' if first else 'stopped'}")
            calls = [Call(f, None, "default plan (planner failed)", round_no) for f in defaults] if first else []
        if not calls:
            break
        # Same reason as observe(): reply targets are read last so their ages stay fresh.
        calls.sort(key=lambda c: c.feed in RUN_LAST)
        for call in calls:
            try:
                with use(watch_for(call, base)):
                    got = REGISTRY[call.feed]().fetch(mission)
            except Exception as e:  # noqa: BLE001 - one feed failing shouldn't end the round
                call.error = str(e)[:300]
                got = []
            call.count = len(got)
            hottest = sorted(got, key=lambda s: signal_heat(s, now, k) or 0.0, reverse=True)[:3]
            call.top = [s.text[:120] for s in hottest]
            for s in got:
                signals.setdefault((s.source, s.url, s.text), s)
            done.append(call)
            status = f"error: {call.error}" if call.error else f"{call.count} signals"
            print(f"[observe] agent round {round_no}: {call.feed}{' ' + str(call.param) if call.param else ''} -> {status}")
    return list(signals.values()), done, notes


def trace_path() -> Path:
    return Path(os.getenv("HARNESS_OBSERVE_TRACE") or config.OUT_DIR / "observe-agent-trace.json")


def write_trace(path: Path, calls: list[Call], notes: list[str], now: datetime) -> None:
    rows = [{k: v for k, v in c.__dict__.items()} for c in calls]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": iso_utc(now), "calls": rows, "notes": notes}, indent=1, ensure_ascii=False), encoding="utf-8")


class ObservationAgentSource(Source):
    """An LLM picks the feeds and queries to read this round, in up to two rounds."""

    name = "agent"

    def fetch(self, mission: str) -> list[Signal]:
        now = now_utc()
        signals, calls, notes = run_agent(get_llm(), mission, config.lessons(), now)
        for note in notes:
            print(f"[observe] agent: {note}")
        write_trace(trace_path(), calls, notes, now)
        if not calls:
            raise SourceError("agent: no feed was called; check [agent] default_sources in the observe TOML")
        if not signals and all(c.error for c in calls):
            raise SourceError(f"agent: every feed failed; first error: {calls[0].feed}: {calls[0].error}")
        return signals
