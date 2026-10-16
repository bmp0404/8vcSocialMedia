"""ESPN's public site API: league and team news, the scoreboard with odds, and injuries.

The endpoints need no key. Leagues and team ids come from the profile's `observe.toml`.
"""

from datetime import timedelta

from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, get_json, iso_utc, now_utc, parse_time
from harness.sources.watch import load_watch

BASE = "https://site.api.espn.com/apis/site/v2/sports"


def news_signals(payload: dict, league: str) -> list[Signal]:
    signals = []
    for a in payload.get("articles", []):
        headline = a.get("headline")
        if not headline:
            continue
        published = parse_time(a.get("published"))
        signals.append(
            Signal(
                source="espn_news",
                text=f"{headline}\n{a.get('description') or ''}".strip(),
                url=(a.get("links") or {}).get("web", {}).get("href"),
                author=a.get("byline") or f"ESPN {league.split('/')[-1]}",
                created_at=iso_utc(published) if published else None,
                metrics={},
                kind="news",
            )
        )
    return signals


# Fetch betting odds for games starting within this window (or under way for up to 3 hours).
ODDS_WINDOW = timedelta(hours=48)


def odds_line(summary: dict) -> str:
    """'GS -2.5, O/U 154.5' from the first pickcenter entry of an event summary."""
    picks = summary.get("pickcenter") or []
    if not picks:
        return ""
    pick = picks[0]
    parts = [pick.get("details", "")]
    if pick.get("overUnder") is not None:
        parts.append(f"O/U {pick['overUnder']}")
    return ", ".join(p for p in parts if p)


def game_signals(payload: dict, league: str) -> list[Signal]:
    """One signal per game, kind "schedule".

    `created_at` stays empty because tip-off isn't a publication time; a future tip-off would
    give a negative age to anything measuring recency. The start goes in `starts_at_unix`.
    Without a `dates` parameter ESPN returns the next day with games, not today.
    """
    signals = []
    for e in payload.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        status = (e.get("status") or {}).get("type", {})
        teams = comp.get("competitors", [])
        scoreline = ", ".join(f"{t['team']['displayName']} {t.get('score', '')}".strip() for t in teams)
        notes = "; ".join(n.get("headline", "") for n in comp.get("notes", []) if n.get("headline"))
        start = parse_time(e.get("date"))
        state = status.get("state", "")  # pre | in | post
        metrics = {"game_live": 1 if state == "in" else 0, "game_final": 1 if status.get("completed") else 0}
        if start:
            metrics["starts_at_unix"] = int(start.timestamp())
        if str(e.get("id", "")).isdigit():
            metrics["event_id"] = int(e["id"])
        for t in teams:
            if str(t.get("score", "")).isdigit():
                metrics[f"score_{t['team'].get('abbreviation', t['team']['id'])}"] = int(t["score"])
        links = e.get("links") or [{}]
        signals.append(
            Signal(
                source="espn_scores",
                text=f"{e.get('name', '')} ({status.get('detail', '')}). {notes} {scoreline}".strip(),
                url=links[0].get("href"),
                author=f"ESPN {league.split('/')[-1]}",
                created_at=None,
                metrics=metrics,
                kind="schedule",
            )
        )
    return signals


def injury_signals(payload: dict, league: str) -> list[Signal]:
    """One signal per listed injury, dated by ESPN's last update to it. The feed repeats some entries."""
    signals, seen = [], set()
    for team in payload.get("injuries", []):
        for inj in team.get("injuries", []):
            name = (inj.get("athlete") or {}).get("displayName", "")
            updated = parse_time(inj.get("date"))
            if (name, inj.get("date")) in seen:
                continue
            seen.add((name, inj.get("date")))
            signals.append(
                Signal(
                    source="espn_injuries",
                    text=f"{name} ({team.get('displayName', '')}): {inj.get('status', '')}. {inj.get('shortComment', '')}".strip(),
                    url=None,
                    author=f"ESPN {league.split('/')[-1]}",
                    created_at=iso_utc(updated) if updated else None,
                    metrics={},
                    kind="news",
                )
            )
    return signals


class EspnNewsSource(Source):
    """League news plus team-filtered news for the teams in observe.toml."""

    name = "espn_news"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().espn
        if not watch.leagues:
            raise SourceError("espn_news: no leagues in [espn] of the profile's observe.toml")
        signals: list[Signal] = []
        seen: set[str] = set()
        for league in watch.leagues:
            requests = [{}] + [{"team": t} for t in watch.teams.get(league, [])]
            for params in requests:
                payload = get_json(
                    f"{BASE}/{league}/news", source=self.name, params={"limit": watch.articles_per_league, **params}
                )
                for s in news_signals(payload, league):
                    key = s.url or s.text
                    if key not in seen:
                        seen.add(key)
                        signals.append(s)
        return signals


class EspnScoresSource(Source):
    """The next game day for each league in [espn] scoreboard_leagues, with odds for games soon."""

    name = "espn_scores"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().espn
        if not watch.scoreboard_leagues:
            raise SourceError("espn_scores: no scoreboard_leagues in [espn] of the profile's observe.toml")
        now = now_utc()
        signals: list[Signal] = []
        for league in watch.scoreboard_leagues:
            games = game_signals(get_json(f"{BASE}/{league}/scoreboard", source=self.name), league)
            for g in games:
                starts = g.metrics.get("starts_at_unix")
                soon = starts is not None and -3 * 3600 < starts - now.timestamp() < ODDS_WINDOW.total_seconds()
                if soon and g.metrics.get("event_id"):
                    summary = get_json(f"{BASE}/{league}/summary", source=self.name, params={"event": g.metrics["event_id"]})
                    line = odds_line(summary)
                    if line:
                        g.text += f" Odds: {line}."
            signals.extend(games)
        return signals


class EspnInjuriesSource(Source):
    """League injury reports. ESPN's team endpoint lagged here; the league one had Boston out at 15:05Z."""

    name = "espn_injuries"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().espn
        if not watch.scoreboard_leagues:
            raise SourceError("espn_injuries: no scoreboard_leagues in [espn] of the profile's observe.toml")
        signals: list[Signal] = []
        for league in watch.scoreboard_leagues:
            signals.extend(injury_signals(get_json(f"{BASE}/{league}/injuries", source=self.name), league))
        return signals
