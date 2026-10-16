"""Orient: cluster raw signals into narratives, score relevance with the LLM, velocity from data.

The LLM groups signals and judges fit with the mission. It no longer has to guess how fast a
story moves when the signals carry timestamps and counts: `signal_heat` turns each signal into a
0-1 heat from its age and engagement rate, and `narrative_velocity` combines a narrative's heats.
The LLM's velocity survives only for narratives whose signals have no timestamps (the sample
fixtures, for example), and `Narrative.velocity_basis` records which one was used.
"""

import json
import math
from datetime import datetime

from harness.llm import LLM
from harness.models import Narrative, Signal
from harness.sources.common import now_utc, parse_time

SYSTEM = """You are the strategist for the brand account described in the mission. Group the
observed signals into distinct narratives the account could join. Score each:
- relevance (0-1): fit with the mission's subjects and tone
- velocity (0-1): how fast attention is moving right now. Code replaces this with a measured
  value when the signals have timestamps, so judge it only from what the text says.
Suggest a one-line `angle` the account could take, in the tone the mission asks for.
Cite each narrative's evidence by signal id. Use only ids from the list."""

PROMPT = """MISSION:
{mission}

SIGNALS (age_h is hours since posted; counts are as of now):
{signals}

Return JSON: {{"narratives": [{{"id": "kebab-slug", "title": "...", "summary": "...",
"relevance": 0.0, "velocity": 0.0, "angle": "...", "signal_ids": [0]}}]}}"""

# The constants below have no calibration run behind them; they set the scale so a few clearly
# hot items saturate velocity. Treat them as a starting point for eval to tune.
# Engagements per hour at which one X post counts as maximally hot.
X_RATE_FULL = 10_000
# Hours for a news article's heat to halve, and the heat of a brand-new article. Kept below 1
# so one headline doesn't outrank a post people are actively piling onto.
NEWS_HALF_LIFE_H = 12.0
NEWS_MAX_HEAT = 0.6
# Total heat at which a narrative's velocity reaches 1.
VELOCITY_FULL_HEAT = 5.0
# Signals sent to the LLM, newest first, and characters of text per signal.
MAX_SIGNALS = 150
TEXT_CHARS = 280

X_KINDS = {"post", "reply_target", "mention", "own_post"}
TARGET_KINDS = {"post", "reply_target", "mention"}
COUNT_KEYS = ("likes", "reposts", "replies", "quotes")


def _age_hours(s: Signal, now: datetime) -> float | None:
    created = parse_time(s.created_at)
    return None if created is None else (now - created).total_seconds() / 3600


def engagement_per_hour(s: Signal, now: datetime) -> float | None:
    """The source's measured rate if it gave one, else total engagement over age (min 5 minutes)."""
    if "engagement_per_hour" in s.metrics:
        return float(s.metrics["engagement_per_hour"])
    counts = [s.metrics[k] for k in COUNT_KEYS if isinstance(s.metrics.get(k), (int, float))]
    age = _age_hours(s, now)
    if not counts or age is None:
        return None
    return sum(counts) / max(age, 5 / 60)


def signal_heat(s: Signal, now: datetime) -> float | None:
    """0-1 heat for one signal, or None when it has no timestamp to measure from."""
    age = _age_hours(s, now)
    if age is None:
        return None
    if s.kind == "game":
        if s.metrics.get("game_live"):
            return 1.0
        if age < 0:  # not started: anticipation builds as tip-off nears
            return 0.8 * 0.5 ** (-age / 24)
        return 0.8 * 0.5 ** (age / NEWS_HALF_LIFE_H)
    if s.kind == "attention":
        # Twice the usual attention is half-hot, four times is fully hot; at or below normal adds nothing.
        ratio = float(s.metrics.get("spike_ratio", 0.0))
        return min(1.0, max(0.0, math.log2(ratio) / 2)) if ratio > 0 else 0.0
    rate = engagement_per_hour(s, now)
    if rate is not None or s.kind in X_KINDS:
        rate = rate or 0.0
        return min(1.0, math.log10(1 + rate) / math.log10(1 + X_RATE_FULL))
    return NEWS_MAX_HEAT * 0.5 ** (max(age, 0.0) / NEWS_HALF_LIFE_H)


def narrative_velocity(heats: list[float]) -> float:
    """Diminishing returns on total heat: one hot item counts, ten lukewarm ones count more."""
    return round(min(1.0, math.log1p(sum(heats)) / math.log1p(VELOCITY_FULL_HEAT)), 3)


def reply_targets(members: list[Signal], now: datetime, limit: int = 5) -> list[dict]:
    """X posts a reply or quote can point at, fastest first."""
    rows = []
    for s in members:
        if s.kind not in TARGET_KINDS or not s.url:
            continue
        created = parse_time(s.created_at)
        rate = engagement_per_hour(s, now)
        rows.append(
            {
                "url": s.url,
                "author": s.author,
                "text": s.text[:200],
                "age_minutes": None if created is None else round((now - created).total_seconds() / 60),
                "engagement_per_hour": None if rate is None else round(rate, 1),
            }
        )
    rows.sort(key=lambda r: r["engagement_per_hour"] or 0, reverse=True)
    return rows[:limit]


def _payload(signals: list[Signal], now: datetime) -> str:
    rows = []
    for i, s in enumerate(signals):
        age = _age_hours(s, now)
        row = {"id": i, "source": s.source, "kind": s.kind or None, "author": s.author, "text": s.text[:TEXT_CHARS]}
        if age is not None:
            row["age_h"] = round(age, 1)
        counts = {k: v for k, v in s.metrics.items() if isinstance(v, (int, float))}
        if counts:
            row["metrics"] = counts
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False)


def _select(signals: list[Signal], now: datetime) -> list[Signal]:
    """Drop the brand's own posts (nothing to join) and keep the newest MAX_SIGNALS."""
    usable = [s for s in signals if s.kind != "own_post"]
    # abs(): a game two weeks out is no more current than a story two weeks old.
    usable.sort(key=lambda s: abs(_age_hours(s, now)) if parse_time(s.created_at) else float("inf"))
    return usable[:MAX_SIGNALS]


def build_narratives(data: dict, signals: list[Signal], now: datetime) -> list[Narrative]:
    """Turn the LLM's clusters into Narratives with URLs, data velocity and reply targets."""
    narratives = []
    for raw in data.get("narratives", []):
        ids = [i for i in raw.get("signal_ids", []) if isinstance(i, int) and 0 <= i < len(signals)]
        members = [signals[i] for i in ids]
        heats = [h for h in (signal_heat(s, now) for s in members) if h is not None]
        urls = list(dict.fromkeys(s.url for s in members if s.url)) or list(raw.get("signal_urls", []))
        narratives.append(
            Narrative(
                id=raw["id"],
                title=raw.get("title", ""),
                summary=raw.get("summary", ""),
                relevance=float(raw.get("relevance", 0.0)),
                velocity=narrative_velocity(heats) if heats else float(raw.get("velocity", 0.0)),
                velocity_basis="data" if heats else "llm",
                angle=raw.get("angle", ""),
                signal_urls=urls,
                reply_targets=reply_targets(members, now),
            )
        )
    return narratives


def uncited_targets(narratives: list[Narrative], signals: list[Signal], now: datetime) -> Narrative | None:
    """Reply targets no narrative cited, kept so the reply lane never starves when the LLM skips them."""
    cited = {u for n in narratives for u in n.signal_urls}
    left = [s for s in signals if s.kind == "reply_target" and s.url not in cited]
    if not left:
        return None
    heats = [h for h in (signal_heat(s, now) for s in left) if h is not None]
    return Narrative(
        id="reply-lane",
        title="Fast-moving posts from watched accounts",
        summary="Fresh posts the reply-target source ranked highest that no other narrative covers.",
        relevance=0.5,  # chosen by the watch list, not scored by the LLM
        velocity=narrative_velocity(heats) if heats else 0.0,
        velocity_basis="data" if heats else "",
        angle="reply early with something the thread doesn't have yet",
        signal_urls=[s.url for s in left],
        reply_targets=reply_targets(left, now, limit=10),
    )


def orient(llm: LLM, mission: str, signals: list[Signal], top: int = 5, now: datetime | None = None) -> list[Narrative]:
    now = now or now_utc()
    chosen = _select(signals, now)

    def mock():
        return {
            "narratives": [
                {
                    "id": f"sample-{i}",
                    "title": s.text[:60],
                    "summary": s.text,
                    "relevance": 0.8 - i * 0.1,
                    "velocity": 0.7,
                    "angle": "playful rivalry banter",
                    "signal_ids": [i],
                }
                for i, s in enumerate(chosen[:top])
            ]
        }

    data = llm.complete_json(SYSTEM, PROMPT.format(mission=mission, signals=_payload(chosen, now)), mock=mock)
    narratives = build_narratives(data, chosen, now)
    for n in narratives:
        print(f"[orient] {n.id}: relevance {n.relevance:.2f} velocity {n.velocity:.2f} ({n.velocity_basis})")
    narratives.sort(key=lambda n: n.relevance * n.velocity, reverse=True)
    narratives = narratives[:top]
    lane = uncited_targets(narratives, chosen, now)
    if lane:
        # Ranked like any narrative: decide() drafts for the top two, so a hot reply lane competes
        # for those slots instead of trailing the list where only the random exploration pick finds it.
        print(f"[orient] {lane.id}: {len(lane.reply_targets)} reply targets no narrative cited, velocity {lane.velocity:.2f}")
        narratives.append(lane)
        narratives.sort(key=lambda n: n.relevance * n.velocity, reverse=True)
    return narratives
