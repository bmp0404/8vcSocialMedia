"""Qualify: a simulated audience panel scores drafts; a critic vetoes anything off-brand or untrue.

Panelists come from eval/panel.json. `backtest` and evolve.py calibrate their weights against real outcomes,
so panelists whose predictions track reality count for more.
"""

import json

from harness import config, memory
from harness.llm import LLM
from harness.models import Action

PANEL_SYSTEM = """You simulate an X audience panel. For each draft, each panelist predicts how likely
they are to engage (like, reply, repost, quote) on a 0-1 scale. Be harsh: most posts get scrolled past.
Reserve scores above 0.7 for posts that panelist would actually repost."""

PANEL_PROMPT = """PANEL:
{panel}

DRAFTS:
{drafts}

Return JSON: {{"scores": {{"<draft id>": {{"<panelist>": 0.0}}}}}}"""

CRITIC_SYSTEM = """You are the brand-safety and accuracy critic for a company's X account. Veto any draft that:
- makes a product claim not supported by the mission's fact sheet (invented features, pricing, customers, numbers)
- presents a made-up story as a real event or real customer
- is harassing, punches at specific people, or is engagement bait
- is a reply/quote with no target, or is over 280 characters per post
Otherwise keep it, and give one line of feedback that would make it hit harder."""

CRITIC_PROMPT = """MISSION:
{mission}

DRAFTS:
{drafts}

Return JSON: {{"verdicts": {{"<draft id>": {{"veto": false, "critique": "..."}}}}}}"""


def _drafts(actions: list[Action]) -> str:
    return json.dumps(
        [{"id": a.id, "type": a.type, "text": a.text, "target_url": a.target_url, "target_handle": a.target_handle}
         for a in actions],
        indent=1,
    )


def panel_scores(llm: LLM, actions: list[Action]) -> dict:
    """{action_id: {panelist: 0-1}}"""
    panel = config.panel()
    return llm.complete_json(
        PANEL_SYSTEM,
        PANEL_PROMPT.format(panel=json.dumps(panel, indent=1), drafts=_drafts(actions)),
        mock=lambda: {"scores": {a.id: {p: 0.5 for p in panel} for a in actions}},
    ).get("scores", {})


def critic(llm: LLM, mission: str, actions: list[Action]) -> dict:
    """{action_id: {"veto": bool, "critique": str}}"""
    return llm.complete_json(
        CRITIC_SYSTEM,
        CRITIC_PROMPT.format(mission=mission, drafts=_drafts(actions)),
        mock=lambda: {"verdicts": {a.id: {"veto": False, "critique": "mock ok"} for a in actions}},
    ).get("verdicts", {})


def weighted_score(panel_scores: dict, weights: dict) -> float:
    if not panel_scores:
        return 0.0
    total = sum(weights.get(p, 1.0) for p in panel_scores)
    return round(sum(s * weights.get(p, 1.0) for p, s in panel_scores.items()) / total, 3)


def qualify(llm: LLM, mission: str, actions: list[Action]) -> list[Action]:
    if not actions:
        return []
    weights = memory.kv_get("panel_weights", {})
    scores = panel_scores(llm, actions)
    verdicts = critic(llm, mission, actions)

    for a in actions:
        a.panel = scores.get(a.id, {})
        a.predicted_score = weighted_score(a.panel, weights)
        v = verdicts.get(a.id, {})
        a.vetoed = bool(v.get("veto"))
        a.critique = v.get("critique", "")

    kept = sorted((a for a in actions if not a.vetoed), key=lambda a: a.predicted_score, reverse=True)
    print(f"[qualify] kept {len(kept)}/{len(actions)} drafts")
    return kept
