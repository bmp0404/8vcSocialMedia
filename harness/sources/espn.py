"""ESPN's public site API: league and team news, and the scoreboard.

The endpoints need no key. Leagues and team ids come from `mission/observe.toml`.
"""

from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, get_json, iso_utc, parse_time
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


def game_signals(payload: dict, league: str) -> list[Signal]:
    """One signal per game. Scores and state are the only numbers ESPN gives."""
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
                created_at=iso_utc(start) if start else None,
                metrics=metrics,
                kind="game",
            )
        )
    return signals


class EspnNewsSource(Source):
    """League news plus team-filtered news for the teams in observe.toml."""

    name = "espn_news"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().espn
        if not watch.leagues:
            raise SourceError("espn_news: no leagues in [espn] of mission/observe.toml")
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
    """Today's scoreboard (ESPN's default day) for each league in observe.toml."""

    name = "espn_scores"

    def fetch(self, mission: str) -> list[Signal]:
        watch = load_watch().espn
        if not watch.leagues:
            raise SourceError("espn_scores: no leagues in [espn] of mission/observe.toml")
        signals: list[Signal] = []
        for league in watch.leagues:
            signals.extend(game_signals(get_json(f"{BASE}/{league}/scoreboard", source=self.name), league))
        return signals
