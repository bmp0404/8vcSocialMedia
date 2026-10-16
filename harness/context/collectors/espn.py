"""Game calendar from ESPN's site API (keyless): when games started and ended.

"Minutes since the final buzzer" is the sports version of "minutes since the
launch": the moment a post can ride. The scoreboard takes one date per call
(ranges return HTTP 400 on site.api). A game's end is the latest play
`wallclock` in its summary; plays are not sorted, so take the max.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from harness.context.collectors.base import client
from harness.context.http import FetchError

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/{league}"


@dataclass
class Game:
    id: str
    name: str
    scheduled: datetime
    completed: bool
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    started: datetime | None = None  # first play wallclock
    ended: datetime | None = None  # last play wallclock


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def scoreboard(day: datetime, league: str = "wnba") -> list[Game]:
    """Games on one ESPN (US) calendar day."""
    url = f"{SITE.format(league=league)}/scoreboard?dates={day:%Y%m%d}"
    past = day.date() < (datetime.now(timezone.utc) - timedelta(days=2)).date()
    data = client().get_json(url, max_age_s=None if past else 300)
    games = []
    for e in data.get("events") or []:
        comp = e["competitions"][0]
        teams = {c["homeAway"]: c for c in comp["competitors"]}
        games.append(
            Game(
                id=e["id"],
                name=e.get("shortName") or e.get("name", ""),
                scheduled=_dt(e["date"]),
                completed=bool(e["status"]["type"].get("completed")),
                home=teams["home"]["team"]["abbreviation"],
                away=teams["away"]["team"]["abbreviation"],
                home_score=int(teams["home"]["score"]) if teams["home"].get("score") not in (None, "") else None,
                away_score=int(teams["away"]["score"]) if teams["away"].get("score") not in (None, "") else None,
            )
        )
    return games


def with_wallclock(game: Game, league: str = "wnba") -> Game:
    """Fill `started` and `ended` from the play-by-play wallclock."""
    url = f"{SITE.format(league=league)}/summary?event={game.id}"
    data = client().get_json(url, max_age_s=None if game.completed else 120)
    clocks = [_dt(p["wallclock"]) for p in data.get("plays") or [] if p.get("wallclock")]
    if not clocks:
        raise FetchError(f"ESPN summary for game {game.id} has no play wallclocks yet")
    game.started, game.ended = min(clocks), max(clocks)
    return game


def last_final_before(when: datetime, team: str | None = None, league: str = "wnba", lookback_days: int = 3) -> Game | None:
    """The most recent completed game (optionally involving `team`) that ended before `when`."""
    best = None
    for back in range(lookback_days + 1):
        for g in scoreboard(when - timedelta(days=back), league):
            if not g.completed or (team and team not in (g.home, g.away)):
                continue
            g = with_wallclock(g, league)
            if g.ended and g.ended <= when and (best is None or g.ended > best.ended):
                best = g
        if best:
            return best
    return best
