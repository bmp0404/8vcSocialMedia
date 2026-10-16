"""Qualify: a simulated audience panel scores drafts; a brand-safety critic can veto them.

The panelists are brand-specific, so they live in the mission profile's panel.json, not in this module
(config.profile_file; HARNESS_PANEL points it elsewhere). The backtest passes eval/panels/devtools.json
to score the data team's dev-tool posts.
"""

import json
import os
import random
from pathlib import Path

from harness import config
from harness.llm import LLM
from harness.models import Action

# A separate model for the panel. On the dev-tool backtest (eval/DESIGN.md) gpt-6.1-sol separated a
# brand's viral posts from its flops at AUC 0.854 against claude-sonnet-5-5's 0.797 (difference inside
# the interval), and a judge from another family avoids rating its own drafts. Unset keeps the run's LLM.
PANEL_MODEL = os.getenv("HARNESS_PANEL_MODEL", "")

PANEL_SYSTEM = """You simulate an X audience panel. For each draft, each panelist predicts how likely
they are to engage (like, reply, repost) on a 0-1 scale. Be harsh; most posts get ignored."""

PANEL_PROMPT = """PANEL:
{panel}

DRAFTS:
{drafts}

Return JSON: {{"scores": {{"<draft id>": {{"<panelist>": 0.0}}}}}}"""

CRITIC_SYSTEM = """You are the brand-safety and quality critic for the account described in MISSION. Veto any
draft that is harassing, cruel, factually shaky, off-mission, or a reply/quote with no target. Otherwise keep it.
A draft's "context" is what the drafting stage checked it against: the post or source, the thread and the round's
facts. A claim that context supports is not factually shaky."""

CRITIC_PROMPT = """MISSION:
{mission}

DRAFTS:
{drafts}

Return JSON: {{"verdicts": {{"<draft id>": {{"veto": false, "critique": "..."}}}}}}"""


def load_panel(path: Path | None = None) -> dict[str, str]:
    """Read {panelist name: who they are and what makes them engage} from a JSON file."""
    path = path or config.profile_file("panel.json")
    if not path.exists():
        raise FileNotFoundError(f"no audience panel at {path}")
    panel = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(panel, dict) or not panel or not all(
        isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in panel.items()
    ):
        raise ValueError(f"{path} must be a non-empty JSON object of panelist name -> description")
    return panel


def _drafts(actions: list[Action]) -> str:
    return json.dumps(
        [{"id": a.id, "type": a.type, "text": a.text, "target_url": a.target_url, "target_handle": a.target_handle}
         for a in actions],
        indent=1,
    )


def _critic_drafts(actions: list[Action]) -> str:
    """The drafts with the context the action veto judged them against, so the critic and the veto see the same
    facts. In Integration run 37 the critic vetoed a caption as unsupported because only the veto had seen the
    narrative's facts. The panel keeps _drafts: it predicts engagement, and the backtest measured it on that input."""
    return json.dumps(
        [{"id": a.id, "type": a.type, "text": a.text, "target_url": a.target_url, "target_handle": a.target_handle,
          **({"context": a.draft_meta["veto_context"]} if a.draft_meta.get("veto_context") else {})}
         for a in actions],
        indent=1,
    )


def panel_scores(llm: LLM, actions: list[Action], panel: dict[str, str]) -> dict[str, dict[str, float]]:
    """{action id: {panelist: 0-1}}. The backtest calls this directly, so it measures the same prompt."""
    return llm.complete_json(
        PANEL_SYSTEM,
        PANEL_PROMPT.format(panel=json.dumps(panel, indent=1), drafts=_drafts(actions)),
        mock=lambda: {"scores": {a.id: {p: 0.5 for p in panel} for a in actions}},
    ).get("scores", {})


def panel_scores_two_orders(llm: LLM, actions: list[Action], panel: dict[str, str]) -> dict[str, dict[str, float]]:
    """Score the drafts in a shuffled order and in reverse, then average each panelist's score.

    A model scoring several posts in one call drifts toward its neighbours: on real dev-tool posts the
    panel's within-account AUC was 0.696 with viral posts first and 0.821 with them last
    (8vc-context/research/eval.md, finding 1). Averaging the two orders cancels most of that for one
    extra call. The shuffle uses Python's default random; order only needs to vary, not repeat.
    """
    order = list(actions)
    random.shuffle(order)
    runs = [panel_scores(llm, order, panel), panel_scores(llm, order[::-1], panel)]
    out = {}
    for a in actions:
        per = [r.get(a.id, {}) for r in runs]
        names = set.intersection(*(set(x) for x in per)) if all(per) else set()
        out[a.id] = {n: sum(float(x[n]) for x in per) / len(per) for n in names}
    return out


def panel_mean(scores: dict[str, float]) -> float:
    return round(sum(scores.values()) / len(scores), 3) if scores else 0.0


def panel_llm(llm: LLM) -> LLM:
    """The run's LLM, or HARNESS_PANEL_MODEL on the same Anthropic-compatible gateway."""
    from harness.llm import AnthropicLLM

    if PANEL_MODEL and isinstance(llm, AnthropicLLM):
        return AnthropicLLM(PANEL_MODEL)
    return llm


def qualify(llm: LLM, mission: str, actions: list[Action], panel: dict[str, str] | None = None) -> list[Action]:
    if not actions:
        return []
    panel = panel or load_panel()
    scores = panel_scores_two_orders(panel_llm(llm), actions, panel)

    verdicts = llm.complete_json(
        CRITIC_SYSTEM,
        CRITIC_PROMPT.format(mission=mission, drafts=_critic_drafts(actions)),
        mock=lambda: {"verdicts": {a.id: {"veto": False, "critique": "mock ok"} for a in actions}},
    ).get("verdicts", {})

    for a in actions:
        a.panel = scores.get(a.id, {})
        a.predicted_score = panel_mean(a.panel)
        v = verdicts.get(a.id, {})
        a.vetoed = bool(v.get("veto"))
        a.critique = v.get("critique", "")

    kept = sorted((a for a in actions if not a.vetoed), key=lambda a: a.predicted_score, reverse=True)
    print(f"[qualify] kept {len(kept)}/{len(actions)} drafts")
    return kept
