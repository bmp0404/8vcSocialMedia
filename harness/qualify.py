"""Qualify: a simulated audience panel scores drafts; a brand-safety critic can veto them."""

import json

from harness.llm import LLM
from harness.models import Action

PANEL = {
    "iowa_diehard": "Iowa Hawkeyes superfan who watched every Clark game in college.",
    "casual_wnba_fan": "Follows the WNBA casually; scrolls fast; rewards funny and clear.",
    "rival_fan": "Fan of a rival team; engages with banter but hates lazy hype.",
    "sports_media": "Beat writer / aggregator account; amplifies sharp, accurate takes.",
}

PANEL_SYSTEM = """You simulate an X audience panel. For each draft, each panelist predicts how likely
they are to engage (like, reply, repost) on a 0-1 scale. Be harsh; most posts get ignored."""

PANEL_PROMPT = """PANEL:
{panel}

DRAFTS:
{drafts}

Return JSON: {{"scores": {{"<draft id>": {{"<panelist>": 0.0}}}}}}"""

CRITIC_SYSTEM = """You are the brand-safety and quality critic for a fan account. Veto any draft that is
harassing, cruel, factually shaky, off-mission, or a reply/quote with no target. Otherwise keep it."""

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


def qualify(llm: LLM, mission: str, actions: list[Action]) -> list[Action]:
    if not actions:
        return []
    drafts = _drafts(actions)

    scores = llm.complete_json(
        PANEL_SYSTEM,
        PANEL_PROMPT.format(panel=json.dumps(PANEL, indent=1), drafts=drafts),
        mock=lambda: {"scores": {a.id: {p: 0.5 for p in PANEL} for a in actions}},
    ).get("scores", {})

    verdicts = llm.complete_json(
        CRITIC_SYSTEM,
        CRITIC_PROMPT.format(mission=mission, drafts=drafts),
        mock=lambda: {"verdicts": {a.id: {"veto": False, "critique": "mock ok"} for a in actions}},
    ).get("verdicts", {})

    for a in actions:
        a.panel = scores.get(a.id, {})
        a.predicted_score = round(sum(a.panel.values()) / len(a.panel), 3) if a.panel else 0.0
        v = verdicts.get(a.id, {})
        a.vetoed = bool(v.get("veto"))
        a.critique = v.get("critique", "")

    kept = sorted((a for a in actions if not a.vetoed), key=lambda a: a.predicted_score, reverse=True)
    print(f"[qualify] kept {len(kept)}/{len(actions)} drafts")
    return kept
