"""Eval: a simulated audience panel scores drafts; hard rules and a brand-safety critic can veto them.

The panel comes from the mission's "Audience panel" section (`- name: description` bullets);
without one, a generic panel is used.
"""

import json
import re

from harness.llm import LLM, mock_score
from harness.models import Action

DEFAULT_PANEL = {
    "insider": "Already deep in the mission's subject; rewards specific, correct insider takes, punishes errors.",
    "casual_scroller": "Follows the subject casually; scrolls fast; rewards funny and clear.",
    "skeptic": "Skeptic of the brand or fan of a competitor; engages with sharp banter, hates hype and marketing-speak.",
    "amplifier": "Media / aggregator / peer account; amplifies sharp, accurate, timely takes.",
}

PANEL_SYSTEM = """You simulate an X audience panel. For each draft, each panelist predicts how likely
they are to engage (like, reply, repost, follow back) on a 0-1 scale. Replies and quotes are judged in
the context of the post they respond to. Be harsh; most posts get ignored."""

PANEL_PROMPT = """PANEL:
{panel}

DRAFTS:
{drafts}

Return JSON: {{"scores": {{"<draft id>": {{"<panelist>": 0.0}}}}}}"""

CRITIC_SYSTEM = """You are the brand-safety and quality critic for the brand account in the MISSION.
Veto any draft that is harassing, cruel, factually shaky, off-mission, off-voice, a near-repeat of
something the account said recently, or a reply/quote/mention/follow with no target. Otherwise keep it."""

CRITIC_PROMPT = """MISSION:
{mission}

ALREADY SAID RECENTLY:
{recent}

DRAFTS:
{drafts}

Return JSON: {{"verdicts": {{"<draft id>": {{"veto": false, "critique": "..."}}}}}}"""


def panel_from_mission(mission: str) -> dict[str, str]:
    panel, inside = {}, False
    for line in mission.splitlines():
        if "audience panel" in line.lower():
            inside = True
        elif inside and (m := re.match(r"\s*[-*]\s*\**([\w ]+?)\**\s*:\s*(.+)", line)):
            panel[m[1].strip().lower().replace(" ", "_")] = m[2].strip()
        elif inside and line.strip() and panel:
            break
    return panel or DEFAULT_PANEL


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def rule_veto(a: Action, recent: set[str]) -> str:
    """Deterministic checks that apply regardless of provider; returns a critique or ''."""
    if a.type != "follow" and not a.text.strip():
        return "empty draft"
    if len(a.text) > 280:
        return f"over 280 characters ({len(a.text)})"
    if a.type in {"reply", "quote"} and not a.target_url:
        return f"{a.type} with no target_url"
    if a.type in {"mention", "follow"} and not a.target_handle:
        return f"{a.type} with no target_handle"
    if a.text and _norm(a.text) in recent:
        return "repeats a recent post or another draft in this run"
    return ""


def _drafts(actions: list[Action]) -> str:
    return json.dumps(
        [
            {
                "id": a.id, "lane": a.lane, "type": a.type, "text": a.text,
                "target_url": a.target_url, "target_handle": a.target_handle,
                "responding_to": (a.context.get("text") or a.context.get("summary") or "")[:400],
            }
            for a in actions
        ],
        indent=1,
    )


def qualify(llm: LLM, mission: str, actions: list[Action], recent_texts: list[str] = ()) -> list[Action]:
    """Score and critique every draft in place. Returns all of them (vetoed flagged), best first."""
    if not actions:
        return []
    panel = panel_from_mission(mission)
    recent = {_norm(t) for t in recent_texts}
    drafts = _drafts(actions)

    scores = llm.complete_json(
        PANEL_SYSTEM,
        PANEL_PROMPT.format(panel=json.dumps(panel, indent=1), drafts=drafts),
        mock=lambda: {"scores": {a.id: {p: mock_score(a.text, a.target_handle, p) for p in panel} for a in actions}},
    ).get("scores", {})

    verdicts = llm.complete_json(
        CRITIC_SYSTEM,
        CRITIC_PROMPT.format(mission=mission, recent=json.dumps(list(recent_texts)[:15]), drafts=drafts),
        mock=lambda: {"verdicts": {a.id: {"veto": False, "critique": "mock: on-mission"} for a in actions}},
    ).get("verdicts", {})

    seen: set[str] = set()
    for a in actions:
        a.panel = {k: float(v) for k, v in scores.get(a.id, {}).items() if isinstance(v, (int, float))}
        a.predicted_score = round(sum(a.panel.values()) / len(a.panel), 3) if a.panel else 0.0
        v = verdicts.get(a.id, {})
        a.vetoed = bool(v.get("veto"))
        a.critique = v.get("critique", "")
        if why := rule_veto(a, recent | seen):
            a.vetoed, a.critique = True, f"rule: {why}"
        seen |= {_norm(a.text)} if a.text else set()

    actions.sort(key=lambda a: (not a.vetoed, a.predicted_score), reverse=True)
    print(f"[eval] kept {sum(not a.vetoed for a in actions)}/{len(actions)} drafts")
    return actions
