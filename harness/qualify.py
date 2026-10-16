"""Qualify: simulated reactions score drafts; a critic vetoes anything off-brand or untrue.

Public actions are scored by the audience panel from eval/panel.json. `backtest` and evolve.py calibrate
panelists' weights against real outcomes, so panelists whose predictions track reality count for more.
One-to-one outreach is scored by simulating its recipient from their CRM entry. Internal actions (hold,
shortlists) aren't scored, only critiqued.
"""

import json

from harness import config, memory
from harness.actions import BY_TYPE, Context
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

RECIPIENT_SYSTEM = """You simulate the recipients of one-to-one outreach (DMs, pitches, invitations, intros,
follow-ups, gifts). For each draft, become the person described and predict the probability (0-1) that
they respond positively and substantively. Be harsh: most cold outreach is ignored. Generic, self-serving,
or badly timed messages score below 0.1; specific, useful, well-timed ones from someone they know can
score above 0.5."""

RECIPIENT_PROMPT = """DRAFTS (with what we know about each recipient):
{drafts}

Return JSON: {{"scores": {{"<draft id>": 0.0}}}}"""

CRITIC_SYSTEM = """You are the brand-safety and accuracy critic for a company's X account and outreach.
Veto any draft that:
- makes a product claim not supported by the mission's fact sheet (invented features, pricing, customers, numbers)
- presents a made-up story as a real event or real customer
- is harassing, punches at specific people, or is engagement bait
- is outreach that is pushy, misleading about why we're reaching out, or asks too much of a stranger
Targets and lengths are already checked in code. Otherwise keep it, and give one line of feedback that would
make it land better."""

CRITIC_PROMPT = """MISSION:
{mission}

DRAFTS:
{drafts}

Return JSON: {{"verdicts": {{"<draft id>": {{"veto": false, "critique": "..."}}}}}}"""


def _drafts(actions: list[Action], ctx: Context | None = None) -> str:
    rows = []
    for a in actions:
        row = {"id": a.id, "type": a.type, "channel": a.channel, "text": a.text,
               "target_url": a.target_url, "target_handle": a.target_handle, "to": a.target_person,
               "details": a.details}
        if ctx:
            row["recipient"] = ctx.contact(a.target_person or a.target_handle) or "not in CRM; known from the conversation"
        rows.append({k: v for k, v in row.items() if v})
    return json.dumps(rows, indent=1)


def panel_scores(llm: LLM, actions: list[Action]) -> dict:
    """{action_id: {panelist: 0-1}}"""
    panel = config.panel()
    return llm.complete_json(
        PANEL_SYSTEM,
        PANEL_PROMPT.format(panel=json.dumps(panel, indent=1), drafts=_drafts(actions)),
        mock=lambda: {"scores": {a.id: {p: 0.5 for p in panel} for a in actions}},
    ).get("scores", {})


def recipient_scores(llm: LLM, actions: list[Action]) -> dict:
    """{action_id: 0-1}, the simulated recipient's chance of a positive, substantive response."""
    ctx = Context(contacts=config.contacts())
    return llm.complete_json(
        RECIPIENT_SYSTEM,
        RECIPIENT_PROMPT.format(drafts=_drafts(actions, ctx)),
        mock=lambda: {"scores": {a.id: 0.3 for a in actions}},
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
    audience = {a.id: BY_TYPE[a.type].audience for a in actions}
    public = [a for a in actions if audience[a.id] == "public"]
    direct = [a for a in actions if audience[a.id] == "person"]
    scores = panel_scores(llm, public) if public else {}
    recipient = recipient_scores(llm, direct) if direct else {}
    verdicts = critic(llm, mission, actions)

    for a in actions:
        if audience[a.id] == "person":
            a.panel = {"recipient": float(recipient.get(a.id, 0.0))}
        else:
            a.panel = scores.get(a.id, {})
        a.predicted_score = weighted_score(a.panel, weights)
        v = verdicts.get(a.id, {})
        a.vetoed = bool(v.get("veto"))
        a.critique = v.get("critique", "")

    kept = sorted((a for a in actions if not a.vetoed), key=lambda a: a.predicted_score, reverse=True)
    print(f"[qualify] kept {len(kept)}/{len(actions)} drafts")
    return kept
