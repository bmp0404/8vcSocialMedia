"""Eval: a simulated audience panel scores drafts; hard rules and a brand-safety critic can veto them.

The panel comes from the mission's "Audience panel" section (`- name: description` bullets);
without one, a generic panel is used. Besides per-panelist engagement scores, the panel predicts the
sentiment split of the reactions a draft would draw. Every veto carries a structured `reason`
(models.REASONS). The critic also enforces grounding: claims about the brand itself must appear in
the mission, otherwise the draft is vetoed with reason `factual_risk`.
"""

import json
import re

from harness.llm import LLM, mock_score
from harness.models import REASONS, Action, norm_reason

DEFAULT_PANEL = {
    "insider": "Already deep in the mission's subject; rewards specific, correct insider takes, punishes errors.",
    "casual_scroller": "Follows the subject casually; scrolls fast; rewards funny and clear.",
    "skeptic": "Skeptic of the brand or fan of a competitor; engages with sharp banter, hates hype and marketing-speak.",
    "amplifier": "Media / aggregator / peer account; amplifies sharp, accurate, timely takes.",
}

PANEL_SYSTEM = """You simulate an X audience panel. For each draft, each panelist predicts how likely
they are to engage (like, reply, repost, follow back) on a 0-1 scale. Replies and quotes are judged in
the context of the post they respond to. Be harsh; most posts get ignored. `target_watchlist` says
when a reply goes to an account the brand is building a relationship with: a panelist who represents
peer accounts should judge it as that account would (does it add to their post, would they reply?).

Also predict the sentiment split of the reactions the draft would draw from this audience (replies,
quotes, the vibe of likes): fractions for positive, neutral and negative that sum to 1. Plugs,
smugness, and anything that reads as an ad pull negative up; a useful tip or a joke that lands pulls
positive up."""

PANEL_PROMPT = """PANEL:
{panel}

DRAFTS:
{drafts}

Return JSON: {{"scores": {{"<draft id>": {{"<panelist>": 0.0}}}},
"sentiment": {{"<draft id>": {{"positive": 0.0, "neutral": 0.0, "negative": 0.0}}}}}}"""

CRITIC_SYSTEM = f"""You are the brand-safety and quality critic for the brand account in the MISSION.
Veto a draft when any of these hold, and give exactly one reason code:
- factual_risk: it states anything about the brand itself (features, platforms, integrations,
  pricing, stats, benchmarks, customers, users, team, roadmap, launches) that is not in the MISSION,
  or contradicts it; or makes an unsupported claim about another company or person. Grounding is
  strict: if a brand fact isn't written in the MISSION, it's invented. General industry
  observations and jokes are fine; facts about the brand must come from the MISSION.
- off_tone: off-voice for the MISSION (marketing-speak, engagement bait, smug, generic praise,
  breaks a Don't), or harassing / cruel / punching down / disparaging a competitor.
- wrong_target: a reply/quote/mention/follow aimed at a post or account that doesn't fit, or that
  responds to something the post didn't say.
- nothing_to_add: agrees or restates without adding a take, tip, fact or joke.
- stale: responds to something old enough that joining now looks late.
- repetitive: a near-repeat of something the account said recently or of another draft in this batch,
  or one plug too many (the MISSION caps how often replies may mention the product; across this batch,
  veto the weakest plugs beyond that share).
- other: anything else that should not ship (say what in the critique).
Otherwise keep it (veto false, reason null). Be strict on factual_risk, fair on everything else: a
sharp, specific draft that respects the MISSION should ship. Reason codes: {", ".join(REASONS)}."""

CRITIC_PROMPT = """MISSION:
{mission}

ALREADY SAID RECENTLY:
{recent}

DRAFTS:
{drafts}

Return JSON: {{"verdicts": {{"<draft id>": {{"veto": false, "reason": null, "critique": "one line"}}}}}}"""


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


def rule_veto(a: Action, recent: set[str]) -> tuple[str, str]:
    """Deterministic checks that apply regardless of provider; returns (reason, critique) or ('', '')."""
    if a.type != "follow" and not a.text.strip():
        return "other", "empty draft"
    if len(a.text) > 280:
        return "other", f"over 280 characters ({len(a.text)})"
    if a.type in {"reply", "quote"} and not a.target_url:
        return "wrong_target", f"{a.type} with no target_url"
    if a.type in {"mention", "follow"} and not a.target_handle:
        return "wrong_target", f"{a.type} with no target_handle"
    if a.text and _norm(a.text) in recent:
        return "repetitive", "repeats a recent post or another draft in this run"
    return "", ""


def _drafts(actions: list[Action]) -> str:
    return json.dumps(
        [
            {
                "id": a.id, "lane": a.lane, "type": a.type, "text": a.text,
                "target_url": a.target_url, "target_handle": a.target_handle,
                "responding_to": (a.context.get("text") or a.context.get("summary") or "")[:400],
                "responding_to_created_at": a.context.get("created_at"),
                # who the target is to the brand (targets file), e.g. a priority peer whose reply matters
                "target_watchlist": {k: w.get(k) for k in ("relationship", "priority")}
                if (w := a.context.get("watchlist")) else None,
            }
            for a in actions
        ],
        indent=1,
    )


def _sentiment(raw) -> dict:
    """Normalize a {positive, neutral, negative} split so it sums to 1; {} if unusable."""
    if not isinstance(raw, dict):
        return {}
    vals = {k: max(0.0, float(raw.get(k) or 0)) for k in ("positive", "neutral", "negative")
            if isinstance(raw.get(k, 0), (int, float))}
    total = sum(vals.values())
    return {k: round(v / total, 2) for k, v in vals.items()} if total > 0 else {}


def _mock_sentiment(a: Action) -> dict:
    pos = mock_score(a.text, "pos", lo=0.2, hi=0.7)
    neg = mock_score(a.text, "neg", lo=0.02, hi=0.25)
    return {"positive": pos, "neutral": round(max(0.0, 1 - pos - neg), 2), "negative": neg}


def qualify(llm: LLM, mission: str, actions: list[Action], recent_texts: list[str] = ()) -> list[Action]:
    """Score and critique every draft in place. Returns all of them (vetoed flagged), best first."""
    if not actions:
        return []
    panel = panel_from_mission(mission)
    recent = {_norm(t) for t in recent_texts}
    drafts = _drafts(actions)

    panel_out = llm.complete_json(
        PANEL_SYSTEM,
        PANEL_PROMPT.format(panel=json.dumps(panel, indent=1), drafts=drafts),
        mock=lambda: {
            "scores": {a.id: {p: mock_score(a.text, a.target_handle, p) for p in panel} for a in actions},
            "sentiment": {a.id: _mock_sentiment(a) for a in actions},
        },
    )
    scores, sentiments = panel_out.get("scores", {}), panel_out.get("sentiment", {})

    verdicts = llm.complete_json(
        CRITIC_SYSTEM,
        CRITIC_PROMPT.format(mission=mission, recent=json.dumps(list(recent_texts)[:15]), drafts=drafts),
        mock=lambda: {"verdicts": {a.id: {"veto": False, "reason": None, "critique": "mock: on-mission"}
                                   for a in actions}},
    ).get("verdicts", {})

    seen: set[str] = set()
    for a in actions:
        a.panel = {k: float(v) for k, v in scores.get(a.id, {}).items() if isinstance(v, (int, float))}
        a.predicted_score = round(sum(a.panel.values()) / len(a.panel), 3) if a.panel else 0.0
        a.sentiment = _sentiment(sentiments.get(a.id))
        v = verdicts.get(a.id, {})
        a.vetoed = bool(v.get("veto"))
        a.critique = v.get("critique", "")
        a.reason = norm_reason(v.get("reason")) if a.vetoed else None
        reason, why = rule_veto(a, recent | seen)
        if why:
            a.vetoed, a.reason, a.critique = True, reason, f"rule: {why}"
        seen |= {_norm(a.text)} if a.text else set()

    actions.sort(key=lambda a: (not a.vetoed, a.predicted_score), reverse=True)
    vetoed = [a for a in actions if a.vetoed]
    why = ", ".join(f"{r}={sum(a.reason == r for a in vetoed)}" for r in REASONS if any(a.reason == r for a in vetoed))
    print(f"[eval] kept {len(actions) - len(vetoed)}/{len(actions)} drafts" + (f" (vetoed: {why})" if why else ""))
    return actions
