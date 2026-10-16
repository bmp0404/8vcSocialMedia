"""WNBA facts from ESPN's keyless site API: rosters, each team's last game and next game, and injuries.

Each fact is memory's `Fact` kind with `as_of` and `valid_until` (harness/facts/ground.py says what
"live" means). `quote` holds the ESPN values the answer was written from, as compact JSON, so a reader
can check the answer against the source without refetching.

Validity windows are guesses; no measurement picked them:
- roster: 7 days from the fetch. Trades and signings are rare in season and a week is a refresh cycle.
- most recent game: 30 days from the game, or until the team plays again. Each team's result and each
  player's line share one fact id per subject ("most recent game"), so a newer game replaces the older
  fact instead of piling up beside it. A month of box scores held every stat value a player had hit
  and let 12 of 287 invented WNBA stats through, against 4 with the most recent game only
  (action/DESIGN.md).
- next game: from the fetch until the day after tip-off, when the result replaces it.
- injury: 3 days from ESPN's last update to the entry, because statuses turn over between games.

`facts_at(at)` builds only what was knowable at `at`: games that ended by then (tip-off plus
GAME_LENGTH), the next game after it, and injury entries ESPN last updated before it. Rosters come
from the live roster endpoint, so they are included only when `at` is now. That is what lets
`harness.minds.lab.fact_grounding` measure past threads without leaking later knowledge.
"""

import json
from datetime import datetime, timedelta, timezone

from harness.context.http import FetchError, PoliteClient
from harness.learning.kinds import Fact, short_hash, slug

SITE = "https://site.api.espn.com/apis/site/v2/sports/{league}"
LEAGUE = "basketball/wnba"
ROSTER_DAYS, GAME_DAYS, INJURY_DAYS = 7, 30, 3
# A WNBA game runs about two hours; three keeps an overtime game from counting as known before it ended.
GAME_LENGTH = timedelta(hours=3)
# Box-score columns, rendered with the stat words ground.STAT_WORDS reads back. Shooting labels are words:
# "2-4 3PT" made the gate read the 3 of 3PT as "3 points" for every player who played.
LINE = (("PTS", "points"), ("REB", "rebounds"), ("AST", "assists"), ("STL", "steals"), ("BLK", "blocks"),
        ("TO", "turnovers"), ("MIN", "minutes"))
SHOOTING = (("FG", "from the field"), ("3PT", "from three"), ("FT", "from the line"))


def entity(name: str) -> str:
    return "entity-" + slug(name)


def _fact(subject: str, question: str, answer: str, quote: dict, url: str, as_of: datetime, valid_until: datetime) -> Fact:
    return Fact(id="fact-espn-" + short_hash(subject, question), subject=subject, question=question, answer=answer,
                quote=json.dumps(quote, ensure_ascii=False, separators=(",", ":"), sort_keys=True), source_url=url,
                as_of=as_of.date().isoformat(), valid_until=valid_until.date().isoformat())


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------- parsers (payload in, facts out)


def roster_facts(payload: dict, team: str, url: str, fetched: datetime) -> list[Fact]:
    players = [(a.get("displayName", ""), a.get("jersey", ""), (a.get("position") or {}).get("abbreviation", ""))
               for a in payload.get("athletes") or [] if a.get("displayName")]
    if not players:
        return []
    coach = " ".join(c for c in ((payload.get("coach") or [{}])[0].get(k, "") for k in ("firstName", "lastName")) if c)
    listing = ", ".join(f"{n} (#{j}, {p})" if j else n for n, j, p in players)
    answer = f"{team} roster: {listing}." + (f" Head coach: {coach}." if coach else "")
    return [_fact(entity(team), f"Who is on the {team} roster?", answer,
                  {"athletes": [n for n, _, _ in players], "coach": coach}, url, fetched, fetched + timedelta(days=ROSTER_DAYS))]


def game_facts(summary: dict, url: str, team: str | None = None) -> list[Fact]:
    """One result fact per team and one line per player who played, from a finished game's summary. With
    `team`, only that team's result and players: a game is each team's most recent only from its own side."""
    comp = ((summary.get("header") or {}).get("competitions") or [{}])[0]
    if not (comp.get("status") or {}).get("type", {}).get("completed"):
        return []
    start = _time(comp["date"])
    as_of, until = start + GAME_LENGTH, start + GAME_LENGTH + timedelta(days=GAME_DAYS)
    day = (start - timedelta(hours=8)).date().isoformat()  # ESPN's US calendar day of the game
    sides = {}
    for c in comp.get("competitors") or []:
        record = next((r.get("summary") for r in c.get("record") or [] if r.get("type") == "total"), "")
        sides[c["team"]["displayName"]] = {"score": str(c.get("score", "")), "record": record, "winner": bool(c.get("winner"))}
    if len(sides) != 2:
        return []
    if team and team not in sides:
        raise ValueError(f"game_facts: {team!r} didn't play in this game; competitors are {sorted(sides)}")
    season = ((summary.get("header") or {}).get("season") or {}).get("type")
    stage = {2: "regular season", 3: "postseason"}.get(season, "")
    series = "; ".join(s.get("summary", "") for s in comp.get("series") or [] if s.get("type") == "playoff" and s.get("summary"))
    facts = []
    for side, me in sides.items():
        if team and side != team:
            continue
        opp, them = next((t, s) for t, s in sides.items() if t != side)
        verb = "beat" if me["winner"] else "lost to"
        # The score both ways round, since people write either; the gate matches a pair only as written.
        answer = (f"On {day}{f' ({stage})' if stage else ''}, the {side} {verb} the {opp} "
                  f"{me['score']}-{them['score']} ({them['score']}-{me['score']})."
                  # Only this team's record: with both, "27-17" from the opponent grounded a draft about this team.
                  + (f" Record after the game: {side} {me['record']}." if me["record"] else "")
                  + (f" Series: {series}." if series else ""))
        facts.append(_fact(entity(side), f"What happened in the {side}'s most recent game?", answer,
                           {"team": side, "opponent": opp, **{f"team_{k}": v for k, v in me.items()},
                            **{f"opponent_{k}": v for k, v in them.items()}, "series": series}, url, as_of, until))
    for block in (summary.get("boxscore") or {}).get("players") or []:
        side = block["team"]["displayName"]
        if team and side != team:
            continue
        opp = next((t for t in sides if t != side), "")
        for table in block.get("statistics") or []:
            names = table.get("names") or []
            for a in table.get("athletes") or []:
                stats = dict(zip(names, a.get("stats") or []))
                if a.get("didNotPlay") or not stats:
                    continue
                name = a["athlete"]["displayName"]
                line = {**{w: stats.get(k, "") for k, w in LINE}, "threes": stats.get("3PT", "").split("-")[0]}
                parts = [f"{v} {w}" for w, v in line.items() if v != ""]
                parts += [f"{stats[k]} {w}" for k, w in SHOOTING if stats.get(k)]
                facts.append(_fact(entity(name), f"How did {name} play in their most recent game?",
                                   f"{name} ({side}) against the {opp} on {day}: {', '.join(parts)}.",
                                   {"athlete": name, "team": side, "stats": stats}, url, as_of, until))
    return facts


def next_game_fact(schedule: dict, team: str, url: str, at: datetime) -> list[Fact]:
    upcoming = sorted((e for e in schedule.get("events") or [] if _time(e["date"]) > at), key=lambda e: e["date"])
    if not upcoming:
        return []
    e = upcoming[0]
    start = _time(e["date"])
    comp = e["competitions"][0]
    opp = next((c["team"]["displayName"] for c in comp.get("competitors") or [] if c["team"]["displayName"] != team), "")
    home = next((c.get("homeAway") for c in comp.get("competitors") or [] if c["team"]["displayName"] == team), "")
    venue = (comp.get("venue") or {}).get("fullName", "")
    day = (start - timedelta(hours=8)).date().isoformat()
    where = ("against" if home == "home" else "at") + f" the {opp}"
    answer = f"The {team} next play {where} on {day}" + (f" at {venue}" if venue else "") + "."
    return [_fact(entity(team), f"When do the {team} play next?", answer,
                  {"team": team, "opponent": opp, "date": e["date"], "home_away": home, "venue": venue, "event": e.get("id")},
                  url, at, start + timedelta(days=1))]


def injury_facts(payload: dict, url: str, at: datetime) -> list[Fact]:
    """One fact per player ESPN lists, from entries last updated by `at`."""
    out, seen = [], set()
    for team in payload.get("injuries") or []:
        for inj in team.get("injuries") or []:
            name = (inj.get("athlete") or {}).get("displayName", "")
            if not name or not inj.get("date") or name in seen:
                continue
            updated = _time(inj["date"])
            if updated > at:
                continue
            seen.add(name)
            status, note = inj.get("status", ""), inj.get("shortComment", "")
            answer = f"{name} ({team.get('displayName', '')}) injury status: {status}." + (f" {note}" if note else "")
            out.append(_fact(entity(name), f"What is {name}'s injury status?", answer,
                             {"athlete": name, "team": team.get("displayName", ""), "status": status, "comment": note,
                              "date": inj["date"]}, url, updated, updated + timedelta(days=INJURY_DAYS)))
    return out


# ---------------------------------------------------------------- fetching


class Espn:
    """ESPN's site API through the shared polite client: one request per second per host, disk cache."""

    def __init__(self, client: PoliteClient | None = None, league: str = LEAGUE):
        self.client = client or PoliteClient(min_interval_s=1.0)
        self.base = SITE.format(league=league)
        self.skipped: dict[str, str] = {}  # event id -> why its box score couldn't be read

    def get(self, path: str, settled_after: datetime | None = None, max_age_s: float | None = 3600) -> tuple[dict, str]:
        url = f"{self.base}/{path}"
        return self.client.get(url, max_age_s=max_age_s, settled_after=settled_after).json(), url

    def teams(self) -> list[dict]:
        data, _ = self.get("teams", max_age_s=86400)
        return [t["team"] for t in data["sports"][0]["leagues"][0]["teams"]]

    def game(self, event: dict, team: str) -> list[Fact]:
        summary, url = self.get(f"summary?event={event['id']}", settled_after=_time(event["date"]) + GAME_LENGTH)
        return game_facts(summary, url, team)

    def facts_at(self, at: datetime, rosters: bool = False, season: int | None = None, history_days: int = 0) -> list[Fact]:
        """Every team's last finished game and next game as of `at`, injuries updated by then, and the
        current rosters when `rosters` is set (only true for now). `history_days` also reads every game that
        ended in the days before `at`; since a newer game replaces an older one's facts, the result is what
        daily refreshes would have left in the store, including lines for players who sat out the last game."""
        season = season or at.year
        facts: list[Fact] = []
        for team in self.teams():
            name = team["displayName"]
            sched, sched_url = self.get(f"teams/{team['id']}/schedule?season={season}")
            done = [e for e in sched.get("events") or []
                    if _time(e["date"]) + GAME_LENGTH <= at
                    and e["competitions"][0].get("status", {}).get("type", {}).get("completed")]
            if done:
                last = max(done, key=lambda e: e["date"])
                since = at - timedelta(days=history_days)
                for event in [e for e in done if since <= _time(e["date"]) + GAME_LENGTH and e is not last] + [last]:
                    try:
                        facts += self.game(event, name)
                    except FetchError as e:  # one missing box score shouldn't cost the league's other facts
                        if event["id"] not in self.skipped:
                            print(f"[facts] skipped ESPN game {event['id']} ({event.get('shortName', '')}): {e}")
                        self.skipped[event["id"]] = str(e)
            facts += next_game_fact(sched, name, sched_url, at)
        if rosters:
            facts += self.rosters(at)
        injuries, url = self.get("injuries")
        facts += injury_facts(injuries, url, at)
        return _dedupe(facts)

    def rosters(self, at: datetime) -> list[Fact]:
        """Today's rosters, dated `at`. Only honest when `at` is now; the grounding measurement also dates
        them a few days back to estimate what a refresh on the thread's day would have added."""
        facts = []
        for team in self.teams():
            roster, url = self.get(f"teams/{team['id']}/roster")
            facts += roster_facts(roster, team["displayName"], url, at)
        return facts


def _dedupe(facts: list[Fact]) -> list[Fact]:
    """One fact per id, keeping the newest: what writing them to the store in date order would leave."""
    out: dict[str, Fact] = {}
    for f in facts:
        if f.id not in out or f.as_of > out[f.id].as_of:
            out[f.id] = f
    return list(out.values())
