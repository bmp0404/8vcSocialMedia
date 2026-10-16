"""Orient: cluster raw signals into narratives, score relevance with the LLM, velocity from data.

The LLM groups signals and judges fit with the mission. It no longer has to guess how fast a
story moves when the signals carry timestamps and counts: `signal_heat` turns each signal into a
0-1 heat from its age and engagement rate, and `narrative_velocity` combines a narrative's heats.
The LLM's velocity survives only for narratives whose signals have no timestamps (the sample
fixtures, for example), and `Narrative.velocity_basis` records which one was used. Measured
velocity is absolute: it compares a narrative with fixed scales, not with its own history,
because a single run has no baseline to compare against.

The prompt is built to a character budget rather than sliced: reply targets go first, stale
items are dropped, and the rest is interleaved across sources so no one source crowds out others.

The model is asked through BAML (`OrientSignals` in eval/baml_src/orient.baml) unless
HARNESS_ORIENT=json selects the older hand-written JSON prompt below. BAML's parser drops a
narrative it can't parse instead of failing, so the raw reply's count is compared with the parsed one.
"""

import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from harness import baml_adapter, config
from harness.baml_reply import json_closes, raw_array_length
from harness.llm import LLM
from harness.models import Narrative, Signal
from harness.sources.common import now_utc, parse_time
from harness.sources.knobs import ObserveKnobs, current

SYSTEM = """You are the strategist for the brand account described in the mission. Group the
observed signals into distinct narratives the account could join. Score each:
- relevance (0-1): fit with the mission's subjects and tone
- velocity (0-1): how fast attention is moving right now. Code replaces this with a measured
  value when the signals have timestamps, so judge it only from what the text says.
Suggest a one-line `angle` the account could take, in the tone the mission asks for.
Cite each narrative's evidence by signal id. Use only ids from the list."""

PROMPT = """MISSION:
{mission}

SIGNALS (age_h is hours since posted, negative for games not yet started; counts are as of now):
{signals}

Return at most {limit} narratives, the ones the account can best join. Keep each summary to one
sentence and cite at most 8 signal ids per narrative.

Return JSON: {{"narratives": [{{"id": "kebab-slug", "title": "...", "summary": "...",
"relevance": 0.0, "velocity": 0.0, "angle": "...", "signal_ids": [0]}}]}}"""

# HARNESS_ORIENT picks how orient asks the model: "baml" or "json". Time-travel recall@5 over 5 runs
# each (observation/reports/orient-baml-recall-20261016.json): json 0.727, baml 0.709, one story fewer
# in 55, inside the 0.545 to 0.818 spread of the json prompt alone. baml is the default for its role
# split, retries and count checks; never surfacing the nemotron story (0/5 against 3/5) is its known miss.
DEFAULT_BACKEND = "baml"
BACKENDS = ("baml", "json")


# Where a reply the checks below flagged is saved. A live json-path reply on 2026-10-16 parsed one
# narrative out of 2,578 output tokens, and nobody could say why because the reply was gone.
REPLY_LOG_DIR = config.OUT_DIR / "orient-replies"


class OrientReplyError(ValueError):
    """The model's reply held narratives and none of them parsed, twice."""


X_KINDS = {"post", "reply_target", "mention", "own_post"}
TARGET_KINDS = {"post", "reply_target", "mention"}
FIRST_KINDS = ("reply_target", "mention")
X_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.twitter.com"}
COUNT_KEYS = ("likes", "reposts", "replies", "quotes")
TARGET_COUNTS = (*COUNT_KEYS, "views", "author_followers", "views_per_hour", "replies_possibly_restricted")


def _when(s: Signal) -> datetime | None:
    """When the item happened: its post time, or a scheduled game's tip-off."""
    if s.kind == "schedule" and isinstance(s.metrics.get("starts_at_unix"), (int, float)):
        return datetime.fromtimestamp(s.metrics["starts_at_unix"], tz=timezone.utc)
    return parse_time(s.created_at)


def _age_hours(s: Signal, now: datetime) -> float | None:
    when = _when(s)
    return None if when is None else (now - when).total_seconds() / 3600


def is_x_post(url: str | None) -> bool:
    return bool(url) and urlsplit(url).netloc.lower() in X_HOSTS and "/status/" in url


def engagement_per_hour(s: Signal, now: datetime, k: ObserveKnobs | None = None) -> float | None:
    """The source's measured rate if it gave one, else total engagement over age (with an age floor)."""
    k = k or current()
    if "engagement_per_hour" in s.metrics:
        return float(s.metrics["engagement_per_hour"])
    counts = [s.metrics[k] for k in COUNT_KEYS if isinstance(s.metrics.get(k), (int, float))]
    age = _age_hours(s, now)
    if not counts or age is None:
        return None
    return sum(counts) / max(age, k.min_age_min / 60)


def signal_heat(s: Signal, now: datetime, k: ObserveKnobs | None = None) -> float | None:
    """0-1 heat for one signal, or None when it has no timestamp to measure from."""
    k = k or current()
    age = _age_hours(s, now)
    if age is None:
        return None
    if s.kind == "schedule":
        if s.metrics.get("game_live"):
            return 1.0
        if age < 0:  # not started: anticipation builds as tip-off nears
            return k.game_max_heat * 0.5 ** (-age / k.game_anticipation_half_life_h)
        return k.game_max_heat * 0.5 ** (age / k.news_half_life_h)
    if s.kind == "attention":
        ratio = float(s.metrics.get("spike_ratio", 0.0))
        return min(1.0, max(0.0, math.log(ratio) / math.log(k.attention_full_ratio))) if ratio > 0 else 0.0
    rate = engagement_per_hour(s, now, k)
    if rate is not None or s.kind in X_KINDS:
        rate = rate or 0.0
        return min(1.0, math.log10(1 + rate) / math.log10(1 + k.x_rate_full))
    return k.news_max_heat * 0.5 ** (max(age, 0.0) / k.news_half_life_h)


def _text_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:60]


def member_heats(members: list[Signal], now: datetime, k: ObserveKnobs | None = None) -> list[float]:
    """One heat per distinct author or publisher, after collapsing near-identical text.

    Counting items would measure bots: the bursts found on 2026-10-15 were one account posting
    the same link six times (research/observe.md, finding 5). An author counts once, at its hottest.
    """
    best: dict[str, float] = {}
    seen: set[str] = set()
    for s in members:
        heat = signal_heat(s, now, k)
        key = _text_key(s.text)
        if heat is None or key in seen:
            continue
        seen.add(key)
        who = (s.author or s.source).lower()
        best[who] = max(best.get(who, 0.0), heat)
    return list(best.values())


def narrative_velocity(heats: list[float], k: ObserveKnobs | None = None) -> float:
    """Diminishing returns on total heat: one hot item counts, ten lukewarm ones count more."""
    k = k or current()
    return round(min(1.0, math.log1p(sum(heats)) / math.log1p(k.velocity_full_heat)), 3)


def reply_targets(members: list[Signal], now: datetime, limit: int = 5, k: ObserveKnobs | None = None) -> list[dict]:
    """X posts a reply or quote can point at, fastest first. Nothing that isn't an X status URL."""
    rows = []
    for s in members:
        if s.kind not in TARGET_KINDS or not is_x_post(s.url):
            continue
        created = parse_time(s.created_at)
        rate = engagement_per_hour(s, now, k)
        row = {
            "url": s.url,
            "author": s.author,
            "text": s.text[:200],
            "created_at": s.created_at,
            "age_minutes": None if created is None else round((now - created).total_seconds() / 60),
            "engagement_per_hour": None if rate is None else round(rate, 1),
        }
        # Raw counts, when the source had them, so a UI can render the post the way X shows it.
        row.update({key: s.metrics[key] for key in TARGET_COUNTS if key in s.metrics})
        if s.thread:
            row["thread"] = s.thread  # not "replies": that key is the reply count
        rows.append(row)
    rows.sort(key=lambda r: r["engagement_per_hour"] or 0, reverse=True)
    return rows[:limit]


def _row(i: int, s: Signal, now: datetime, k: ObserveKnobs) -> dict:
    row = {"id": i, "source": s.source, "text": s.text[: k.text_chars]}
    if s.kind:
        row["kind"] = s.kind
    if s.author:
        row["author"] = s.author
    age = _age_hours(s, now)
    if age is not None:
        row["age_h"] = round(age, 1)
    counts = {k: v for k, v in s.metrics.items() if isinstance(v, (int, float)) and k != "starts_at_unix"}
    if counts:
        row["metrics"] = counts
    return row


def order_signals(signals: list[Signal], now: datetime, k: ObserveKnobs | None = None) -> tuple[list[Signal], Counter]:
    """Priority order for the prompt, and how many items per source were dropped as stale.

    The brand's own posts are left out (there is nothing to join). Reply targets and mentions go
    first, newest first. The rest is interleaved across sources, each newest first, so a source
    with 60 headlines can't push out one with 5 posts.
    """
    k = k or current()
    stale: Counter = Counter()
    first, by_source = [], defaultdict(list)
    for s in signals:
        if s.kind == "own_post":
            continue
        age = _age_hours(s, now)
        if age is not None and age > k.max_signal_age_h:
            stale[s.source] += 1
            continue
        (first if s.kind in FIRST_KINDS else by_source[s.source]).append(s)

    def recency(s: Signal) -> float:
        age = _age_hours(s, now)
        return abs(age) if age is not None else float("inf")  # a game days out is not current either

    ordered = sorted(first, key=recency)
    queues = [sorted(q, key=recency) for q in by_source.values()]
    while any(queues):
        for q in queues:
            if q:
                ordered.append(q.pop(0))
    return ordered, stale


def fit_payload(ordered: list[Signal], now: datetime, k: ObserveKnobs | None = None) -> tuple[list[Signal], str, Counter]:
    """Take signals in order until the JSON reaches k.payload_chars. Returns kept, JSON, dropped per source."""
    k = k or current()
    kept, rows, size, dropped = [], [], 2, Counter()
    for s in ordered:
        row = json.dumps(_row(len(kept), s, now, k), ensure_ascii=False)
        if size + len(row) + 1 > k.payload_chars:
            dropped[s.source] += 1
            continue
        kept.append(s)
        rows.append(row)
        size += len(row) + 1
    return kept, "[" + ",".join(rows) + "]", dropped


def build_narratives(data: dict, signals: list[Signal], now: datetime, k: ObserveKnobs | None = None) -> list[Narrative]:
    """Turn the LLM's clusters into Narratives with URLs, data velocity and reply targets."""
    k = k or current()
    narratives = []
    for raw in data.get("narratives", []):
        ids = [i for i in raw.get("signal_ids", []) if isinstance(i, int) and 0 <= i < len(signals)]
        members = [signals[i] for i in ids]
        heats = member_heats(members, now, k)
        # X post URLs first: anything downstream that takes signal_urls[0] as a reply target
        # (main's mock decide does) then points at a post, not a news article.
        member_urls = sorted(dict.fromkeys(s.url for s in members if s.url), key=lambda u: not is_x_post(u))
        urls = member_urls or list(raw.get("signal_urls", []))
        narratives.append(
            Narrative(
                id=raw["id"],
                title=raw.get("title", ""),
                summary=raw.get("summary", ""),
                relevance=float(raw.get("relevance", 0.0)),
                velocity=narrative_velocity(heats, k) if heats else float(raw.get("velocity", 0.0)),
                velocity_basis="data" if heats else "llm",
                angle=raw.get("angle", ""),
                signal_urls=urls,
                reply_targets=reply_targets(members, now, k.targets_per_narrative, k),
            )
        )
    return narratives


def checked_narratives(value, raw: str) -> dict:
    """The parsed `Orientation` as orient's JSON shape, minus narratives with a score outside 0..1.

    Logs every narrative the parser or this check removed, since a short list otherwise looks like
    a quiet news day. The scores are floats because BAML rounds a fraction into an int literal (0.8
    becomes 1 in 0..10), which would be worse than an out-of-range float caught here.
    """
    problems = []
    sent, parsed = raw_array_length(raw, "narratives"), len(value.narratives)
    if sent != parsed:
        problems.append(f"reply had {sent if sent is not None else 'no'} narratives; the parser kept {parsed}")
    if not json_closes(raw, "narratives"):
        problems.append("reply was cut off before its JSON closed")
    kept = []
    for n in value.narratives:
        bad = {f: getattr(n, f) for f in ("relevance", "velocity") if not 0 <= getattr(n, f) <= 1}
        if bad:
            problems.append(f"dropped {n.id}: {bad} outside 0..1")
            continue
        kept.append(n.model_dump(mode="json"))
    if problems:
        saved = save_reply(raw)
        for problem in problems:
            print(f"[orient] {problem}")
        print(f"[orient] raw reply saved to {saved}")
    return {"narratives": kept}


def save_reply(raw: str) -> Path:
    REPLY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = REPLY_LOG_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.txt"
    path.write_text(raw, encoding="utf-8")
    return path


def ask_baml(llm: LLM, mission: str, payload: str, limit: int, mock) -> dict:
    """OrientSignals through the harness LLM's settings, asked once more if no narrative parsed."""
    if getattr(llm, "is_mock", False):
        return baml_adapter.call(llm, "OrientSignals", mission, payload, limit, mock=mock).value
    for attempt in range(2):
        result = baml_adapter.call(llm, "OrientSignals", mission, payload, limit, mock=mock)
        if result.raw is None:
            raise OrientReplyError("BAML recorded no reply from the model, so orient can't count its narratives")
        data = checked_narratives(result.value, result.raw)
        if data["narratives"] or raw_array_length(result.raw, "narratives") == 0:
            return data
        if attempt == 0:
            print("[orient] no narrative in the reply parsed; asking once more")
    raise OrientReplyError("orient's reply held narratives and none parsed, twice")


def ask_json(llm: LLM, mission: str, payload: str, limit: int, mock) -> dict:
    """The hand-written JSON prompt through llm.complete_json."""
    return llm.complete_json(SYSTEM, PROMPT.format(mission=mission, signals=payload, limit=limit), mock=mock)


def uncited_targets(
    narratives: list[Narrative], signals: list[Signal], now: datetime, k: ObserveKnobs | None = None
) -> Narrative | None:
    """Reply targets no narrative cited, kept so the reply lane never starves when the LLM skips them."""
    k = k or current()
    cited = {u for n in narratives for u in n.signal_urls}
    left = [s for s in signals if s.kind == "reply_target" and s.url not in cited]
    if not left:
        return None
    heats = member_heats(left, now, k)
    return Narrative(
        id="reply-lane",
        title="Fast-moving posts from watched accounts",
        summary="Fresh posts the reply-target source ranked highest that no other narrative covers.",
        relevance=k.reply_lane_relevance,
        velocity=narrative_velocity(heats, k) if heats else 0.0,
        velocity_basis="data" if heats else "",
        angle="reply early with something the thread doesn't have yet",
        signal_urls=[s.url for s in left],
        reply_targets=reply_targets(left, now, 2 * k.targets_per_narrative, k),
    )


def replay_now(signals: list[Signal], wall_clock: datetime, k: ObserveKnobs) -> datetime:
    """`wall_clock`, unless every timestamped signal is past the staleness cut; then the newest signal's time."""
    times = [t for t in (_when(s) for s in signals) if t is not None and t <= wall_clock]
    if not times or (wall_clock - max(times)).total_seconds() / 3600 <= k.max_signal_age_h:
        return wall_clock
    newest = max(times)
    print(
        f"[orient] every signal is older than {k.max_signal_age_h:.0f}h; measuring ages from the newest one "
        f"({newest:%Y-%m-%dT%H:%MZ}). Pass now= to orient() for a replay."
    )
    return newest


def orient(
    llm: LLM,
    mission: str,
    signals: list[Signal],
    top: int = 5,
    now: datetime | None = None,
    knobs: ObserveKnobs | None = None,
) -> list[Narrative]:
    """Narratives ranked by relevance x velocity.

    `now` is the moment ages are measured from. Leave it out for a live run. For a replay or
    backtest, pass the replay cutoff: ages, velocity and the staleness cut all depend on it. If
    `now` is left out and every timestamped signal is older than `max_signal_age_h`, orient
    assumes a replay, measures from the newest signal's time instead, and says so, rather than
    dropping everything as stale. `knobs` defaults to harness.sources.knobs.current().
    """
    k = knobs or current()
    now = now or replay_now(signals, now_utc(), k)
    ordered, stale = order_signals(signals, now, k)
    chosen, payload, dropped = fit_payload(ordered, now, k)
    backend = os.getenv("HARNESS_ORIENT", DEFAULT_BACKEND)
    if backend not in BACKENDS:
        raise ValueError(f"HARNESS_ORIENT must be one of {BACKENDS}, not {backend!r}")
    print(f"[orient] sending {len(chosen)} of {len(signals)} signals ({len(payload):,} chars) via {backend}")
    if stale:
        print(f"[orient] dropped as older than {k.max_signal_age_h:.0f}h: {dict(stale)}")
    if dropped:
        print(f"[orient] dropped for prompt budget: {dict(dropped)}")

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

    ask = ask_baml if backend == "baml" else ask_json
    data = ask(llm, mission, payload, top * k.narrative_overdraw, mock)
    narratives = build_narratives(data, chosen, now, k)
    for n in narratives:
        print(f"[orient] {n.id}: relevance {n.relevance:.2f} velocity {n.velocity:.2f} ({n.velocity_basis})")
    narratives.sort(key=lambda n: n.relevance * n.velocity, reverse=True)
    narratives = narratives[:top]
    lane = uncited_targets(narratives, chosen, now, k)
    if lane:
        # Ranked like any narrative: decide() drafts for the top two, so a hot reply lane competes
        # for those slots instead of trailing the list where only the random exploration pick finds it.
        print(f"[orient] {lane.id}: {len(lane.reply_targets)} reply targets no narrative cited, velocity {lane.velocity:.2f}")
        narratives.append(lane)
        narratives.sort(key=lambda n: n.relevance * n.velocity, reverse=True)
    return narratives
