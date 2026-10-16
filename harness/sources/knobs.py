"""Every tunable number in observe and orient, in one typed object.

The defaults are the values in use on 2026-10-15. None is calibrated against outcomes: each is
judgment or comes from X's ranking code, as the field comments say. sam/iterate tunes them
offline. To try other values, point HARNESS_OBSERVE_KNOBS at a JSON object of overrides; an
unknown key raises so a typo can't silently do nothing. Brand-level settings (queries, accounts,
the reply-target age window and count) live in mission/observe.toml instead.
"""

import json
import os
from dataclasses import dataclass, fields, replace
from functools import cache
from pathlib import Path

from harness.sources.common import SourceError

ENV = "HARNESS_OBSERVE_KNOBS"


@dataclass(frozen=True)
class ObserveKnobs:
    # --- orient: heat of one signal, 0-1 ---
    # Engagements per hour at which one X post (or social post) counts as fully hot; log scale below.
    x_rate_full: float = 10_000
    # Hours for a news item's heat to halve, and a brand-new item's heat. Below 1 so one headline
    # doesn't outrank a post people are piling onto.
    news_half_life_h: float = 12.0
    news_max_heat: float = 0.6
    # A scheduled game: peak heat before or after tip-off, and how fast anticipation builds.
    # Live games are always 1.0; finished games cool at news_half_life_h.
    game_max_heat: float = 0.8
    game_anticipation_half_life_h: float = 24.0
    # Spike ratio (recent attention over its median) at which attention heat reaches 1; log scale,
    # so the square root of this ratio gives 0.5. At or below 1x adds nothing.
    attention_full_ratio: float = 4.0
    # Total distinct-author heat at which a narrative's velocity reaches 1.
    velocity_full_heat: float = 5.0

    # --- orient: the prompt ---
    # 45k characters is about 11k tokens. The 60k slice it replaced cut reply targets off the end
    # once ESPN and Google News reached 58,828 characters.
    payload_chars: int = 45_000
    text_chars: int = 280
    # Items older than this are about a story that has moved on.
    max_signal_age_h: float = 72.0
    # The LLM may return top * this many narratives before code ranks and cuts to top.
    narrative_overdraw: int = 2
    # Reply targets listed per narrative. The reply-lane narrative (targets no narrative cited)
    # gets twice as many and this relevance, since the watch list chose them, not the LLM.
    targets_per_narrative: int = 5
    reply_lane_relevance: float = 0.5

    # --- reply targets ---
    # Half-life of a post's attention in minutes. No study measures reply position against age.
    target_half_life_min: float = 60.0
    # Floor on age so a two-minute-old post with 3 likes doesn't read as 90 likes an hour.
    min_age_min: float = 5.0
    # Snapshot deltas closer together than this are mostly noise.
    min_snapshot_gap_min: float = 2.0
    # Crowding = (1 + replies) ** -crowding_exponent: each reply pushes ours down, the first few most.
    crowding_exponent: float = 0.5
    # Above big_account followers X runs an LLM reply-quality check on replies and can label a
    # low-credibility one RiskyHighVizReply (x-algorithm grox/flows/reply_spam). The mid and small
    # edges are judgment: below ~10k a thread rarely has traffic worth the reply.
    big_account: int = 250_000
    mid_account: int = 10_000
    small_account: int = 5_000
    small_band: float = 0.5
    # A giant's post still counts, at giant_band, inside its first minutes before the thread fills.
    giant_band: float = 0.3
    giant_window_min: float = 10.0
    giant_max_replies: int = 50
    # Several fast replies to one account look like reply spam to X's fast_reply_spam_post check.
    max_per_author: int = 2
    # Direct replies attached to each emitted target, most-liked first. Each target costs one
    # fxtwitter request, and that 1/s budget is shared with the context worker's tracker.
    thread_replies: int = 15
    # A post with no replies despite this many likes, older than this, probably has replies
    # restricted (fxtwitter has no field for it), so a reply would be wasted. Its score is
    # multiplied by restricted_penalty. Uncalibrated: drawn from one @valkyries post with
    # 263 likes, 37 reposts and 0 replies at 75 minutes on 2026-10-15.
    restricted_min_likes: int = 50
    restricted_min_age_min: float = 15.0
    restricted_penalty: float = 0.1

    # --- observation agent ---
    # Planning rounds (the second reads round one's results and may dig deeper), feed calls per
    # round, and items per query or account list the agent may pass to one call.
    agent_rounds: int = 2
    agent_calls_per_round: int = 6
    agent_list_items: int = 6

    @classmethod
    def from_dict(cls, overrides: dict) -> "ObserveKnobs":
        known = {f.name: f.type for f in fields(cls)}
        unknown = set(overrides) - set(known)
        if unknown:
            raise SourceError(f"observe knobs: unknown keys {sorted(unknown)}; known: {sorted(known)}")
        for name, value in overrides.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise SourceError(f"observe knobs: {name} must be a number, got {value!r}")
        return replace(cls(), **overrides)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@cache
def current() -> ObserveKnobs:
    """Defaults, with the overrides in HARNESS_OBSERVE_KNOBS applied when it is set."""
    path = os.getenv(ENV)
    if not path:
        return ObserveKnobs()
    try:
        overrides = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SourceError(f"observe knobs: can't read {ENV}={path}: {e}") from e
    return ObserveKnobs.from_dict(overrides)
