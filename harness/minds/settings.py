"""Every knob the Action stage has, in one typed object, so a tuning loop can change them.

Defaults are tonight's values. Each comment says where the number came from, and says so plainly
when nothing measured it. Override per run with `decide(..., config=ActionConfig(...))`, or point
HARNESS_ACTION_CONFIG at a JSON file of field overrides.
"""

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path


@dataclass(frozen=True)
class GateLimits:
    # X's hard limit. The tighter 160-char reply cap rejected 12.7% of 670 real replies
    # (action/experiments/gate_calibration.json), so length is no longer a style rule.
    max_chars: dict = field(default_factory=lambda: {"reply": 280, "quote": 280, "post": 280, "mention": 280})
    # None turns the rule off. A 2-sentence cap rejected 14.3% of real replies.
    max_reply_sentences: int | None = None
    # 4.5% of real replies use more than 2 distinct emoji; kept because the account doesn't need 3.
    max_distinct_emoji: int = 2
    max_hashtags: dict = field(default_factory=lambda: {"reply": 1, "quote": 1, "post": 2, "mention": 1})


@dataclass(frozen=True)
class ActionConfig:
    # --- reply lane: which posts to answer
    # Research report (8vc-context/research/action.md, finding 7): answer fresh big-account posts.
    # Our fixture: top-3 replies landed a median 28 min after the root, 53 of 72 within 60.
    reply_max_age_minutes: float = 30.0
    # --- reply lane: writing
    reply_samples: int = 6  # research report: 4 to 8; selection gains flatten past ~10 (Stroebl et al.)
    page_replies: int = 12  # real replies shown in the continuation page; research used 12
    min_page_replies: int = 5  # below this the page has too little to copy; use the styled fallback (no measurement)
    continuation_model: str = "claude-opus-4-5-20251101"  # the only gateway model that accepts assistant prefill
    styled_model: str = "claude-opus-4-5-20251101"  # research condition F, AUC 0.79 vs 0.88 for Sonnet 5.5
    # --- veto judge
    veto_model: str = "claude-opus-5-5"  # caught 90-100% of generated replies where GPT caught 40-77%
    veto_max_p_ai: int = 50  # a draft the judge puts at or above this P(AI), in either order, is vetoed (no measurement)
    post_judge_min_grounding: int = 3  # Dhilan's 0-5 grounding score; below this is a veto (no measurement)
    # --- exemplars and the page: drop scraped replies that read as bots
    corpus_max_p_ai: int = 50  # research finding 10; corpus scored by harness.minds.lab.score_corpus
    # --- post lane
    caption_max_chars: int = 100  # research finding 8: House of Highlights-style captions
    caption_samples: int = 4
    # X multiplies repeat posts by one author in a feed batch by 0.75*0.5^k + 0.25 (context
    # research report), so posts are spaced out. The gap itself has no measurement.
    post_spacing_minutes: int = 120
    # --- budgets, by action type. No measurement: they keep Grok's queue short and stay clear of
    # X's burst filters, which flag fast reply bursts and several replies under one root.
    run_caps: dict = field(default_factory=lambda: {"reply": 3, "quote": 2, "post": 1, "mention": 1, "follow": 3})
    daily_caps: dict = field(default_factory=lambda: {"reply": 12, "quote": 4, "post": 2, "mention": 2, "follow": 10})
    gate: GateLimits = field(default_factory=GateLimits)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ActionConfig":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"ActionConfig: unknown field(s) {sorted(unknown)}; known: {sorted(known)}")
        data = dict(data)
        if isinstance(data.get("gate"), dict):
            gate_known = {f.name for f in fields(GateLimits)}
            bad = set(data["gate"]) - gate_known
            if bad:
                raise ValueError(f"GateLimits: unknown field(s) {sorted(bad)}; known: {sorted(gate_known)}")
            data["gate"] = GateLimits(**data["gate"])
        return replace(cls(), **data)


def load() -> ActionConfig:
    """Defaults, or the JSON overrides at HARNESS_ACTION_CONFIG."""
    path = os.getenv("HARNESS_ACTION_CONFIG")
    if not path:
        return ActionConfig()
    return ActionConfig.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
