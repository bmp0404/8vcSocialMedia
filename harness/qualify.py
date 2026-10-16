"""Qualify: a brand-safety critic can veto drafts, and a ranker orders the post lane's drafts.

HARNESS_QUALIFY_RANKER picks the ranker; it is the `qualify.ranker` knob in harness/iterate/knobs.py.
- "post_ridge" (default): the TF-IDF ridge in harness/rank/post_ranker.py, fitted on the post benchmark's dev
  authors and loaded only when its bytes match the hash in its manifest. On the test authors it scored 0.709
  against the panel's 0.723, a difference of -0.014 [-0.081, 0.041] (eval/rankers/post_ridge.manifest.json),
  with no LLM call. It scores the post lane's posts and quotes (people's takes and captions) and reorders
  them among the places they already hold. Replies and follows keep decide's order: on the reply benchmark
  the wording added +0.002 [-0.034, 0.044] to timing and reach (data/bench/results/replies.json,
  ranker_pooled_test), so no text score orders replies.
- "panel": the simulated audience panel scores every draft in two orders and qualify sorts all of them by
  its mean, as before the ridge. It stays so the experiment engine can test it against the ridge.

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

RANKERS = ("post_ridge", "panel")
RANKER_ENV = "HARNESS_QUALIFY_RANKER"

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


def ranker_choice() -> str:
    """HARNESS_QUALIFY_RANKER, default "post_ridge". Any other value raises, so a typo can't quietly run the default."""
    choice = os.getenv(RANKER_ENV, "").strip() or RANKERS[0]
    if choice not in RANKERS:
        raise ValueError(f"{RANKER_ENV}={choice!r}; expected one of {list(RANKERS)}")
    return choice


def ready(ranker: str | None = None) -> str:
    """The ranker a run will use, after loading it: `harness run` calls this before it spends anything, so a typo
    in HARNESS_QUALIFY_RANKER or a model that fails its hash check stops the run before observe."""
    ranker = ranker or ranker_choice()
    if ranker not in RANKERS:
        raise ValueError(f"qualify: ranker {ranker!r}; expected one of {list(RANKERS)}")
    if ranker == "post_ridge":
        from harness.rank import post_ranker

        post_ranker.load()
    return ranker


def rankable(a: Action) -> bool:
    """The post lane's posts and quotes: people's takes and captions. Replies, follows and mentions aren't."""
    return a.lane == "post" and a.type in ("post", "quote") and bool(a.text.strip())


def rank_posts(actions: list[Action]) -> list[Action]:
    """Reorder the scored post-lane drafts by ranker score among the places they already hold. Every other
    action keeps its place, so replies stay in the order decide gave them."""
    places = [i for i, a in enumerate(actions) if "ranker" in a.draft_meta]
    by_score = sorted((actions[i] for i in places), key=lambda a: a.draft_meta["ranker"]["score"], reverse=True)
    out = list(actions)
    for i, a in zip(places, by_score):
        out[i] = a
    return out


def critic(llm: LLM, mission: str, actions: list[Action]) -> None:
    verdicts = llm.complete_json(
        CRITIC_SYSTEM,
        CRITIC_PROMPT.format(mission=mission, drafts=_critic_drafts(actions)),
        mock=lambda: {"verdicts": {a.id: {"veto": False, "critique": "mock ok"} for a in actions}},
    ).get("verdicts", {})
    for a in actions:
        v = verdicts.get(a.id, {})
        a.vetoed = bool(v.get("veto"))
        a.critique = v.get("critique", "")


def qualify(llm: LLM, mission: str, actions: list[Action], panel: dict[str, str] | None = None,
            ranker: str | None = None) -> list[Action]:
    """The drafts the critic kept, in the order the ranker gives (see the module docstring)."""
    if not actions:
        return []
    ranker = ranker or ranker_choice()
    if ranker not in RANKERS:
        raise ValueError(f"qualify: ranker {ranker!r}; expected one of {list(RANKERS)}")
    if ranker == "panel":
        panel = panel or load_panel()
        scores = panel_scores_two_orders(panel_llm(llm), actions, panel)
        critic(llm, mission, actions)
        for a in actions:
            a.panel = scores.get(a.id, {})
            a.predicted_score = panel_mean(a.panel)
        kept = sorted((a for a in actions if not a.vetoed), key=lambda a: a.predicted_score, reverse=True)
    else:
        from harness.rank import post_ranker

        model = post_ranker.load()  # before the critic's call, so a bad model file costs nothing
        critic(llm, mission, actions)
        for a in actions:
            if rankable(a):
                a.draft_meta["ranker"] = model.describe(a.text)
                a.predicted_score = a.draft_meta["ranker"]["percentile"]
        kept = rank_posts([a for a in actions if not a.vetoed])
    print(f"[qualify] kept {len(kept)}/{len(actions)} drafts; ranked by {ranker}")
    return kept
