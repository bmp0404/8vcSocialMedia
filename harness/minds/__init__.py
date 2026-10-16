"""Decide: a chorus of persona "media minds" drafts the post lane; harness/minds/lanes.py runs the
reply lane and screens everything through the typed schema, the slop gate and selection.

Add a mind by dropping a markdown file into minds/prompts/; its filename is the mind's name.
"""

import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from harness.llm import LLM
from harness.minds import lanes, settings
from harness.minds.lanes import write_decision_log  # noqa: F401 - cli.py calls it after memory.save_run
from harness.models import ACTION_TYPES, Action, Narrative, Signal

PROMPTS_DIR = Path(__file__).parent / "prompts"

PROMPT = """MISSION:
{mission}

LESSONS FROM PAST ROUNDS:
{lessons}

NARRATIVES TO WORK WITH:
{narratives}

A narrative with "explore": true ranked lower and is here to test whether the ranking is right, so
treat it like the others.

Propose up to {n} X actions in your style. Allowed types: post, mention, follow (the reply lane
writes replies separately). Mentions and follows need a `target_handle` that appears in the narratives.
Posts must be under 280 characters.

Return JSON: {{"actions": [{{"type": "...", "text": "...", "narrative_id": "...",
"target_url": null, "target_handle": null, "rationale": "..."}}]}}"""

REVISE = """

THE CRITIC VETOED THESE DRAFTS OF YOURS:
{vetoed}

Rewrite each draft so it answers its critique, and keep its type, target and narrative_id. Leave out
any draft you can't fix."""


def target_narratives(narratives: list[Narrative], top: int = 2, explore: int = 1) -> list[dict]:
    """Orient's top narratives, plus random lower-ranked ones so we learn whether the ranking holds.
    When none of those carries reply targets, the best narrative that does joins them, so the minds
    see a live post to work with."""
    focus, rest = narratives[:top], narratives[top:]
    picked = focus + random.sample(rest, min(explore, len(rest)))
    fallback = None
    if not any(n.reply_targets for n in picked):
        with_targets = [n for n in narratives if n.reply_targets and n not in picked]
        if with_targets:
            fallback = max(with_targets, key=lambda n: n.relevance * n.velocity)
            picked.append(fallback)
    return [{**asdict(n), "explore": i >= len(focus) and n is not fallback, "reply_targets_fallback": n is fallback}
            for i, n in enumerate(picked)]


def load_minds() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(PROMPTS_DIR.glob("*.md"))}


def _run_mind(llm: LLM, name: str, persona: str, prompt: str, narratives: list[Narrative]) -> list[Action]:
    def mock():
        n = narratives[0] if narratives else None
        return {
            "actions": [
                {
                    # A post, never a reply: signal_urls hold news links nobody can reply to on X.
                    "type": "post",
                    "text": f"[{name}] mock draft about {n.title if n else 'nothing'}",
                    "narrative_id": n.id if n else "",
                    "rationale": "mock",
                }
            ]
        }

    try:
        data = llm.complete_json(persona, prompt, mock=mock)
    except Exception as e:  # noqa: BLE001 - one bad mind shouldn't sink the chorus
        print(f"[decide] {name} failed: {e}")
        return []
    actions = []
    for a in data.get("actions", []):
        if a.get("type") not in ACTION_TYPES:
            continue
        actions.append(
            Action(
                type=a["type"],
                text=a.get("text", ""),
                mind=name,
                narrative_id=a.get("narrative_id", ""),
                rationale=a.get("rationale", ""),
                target_url=a.get("target_url"),
                target_handle=a.get("target_handle"),
            )
        )
    return actions


def decide(
    llm: LLM,
    mission: str,
    lessons: str,
    narratives: list[Narrative],
    per_mind: int = 3,
    vetoed: list[Action] | None = None,
    budget: lanes.Budget | None = None,
    cfg: settings.ActionConfig | None = None,
    signals: list[Signal] | None = None,
) -> list[Action]:
    """Draft actions from every mind. With `vetoed`, each mind instead revises its own vetoed drafts.
    Returns only the actions that clear the lanes (see lanes.run). Every decision, abstentions
    included, waits in lanes.PENDING until write_decision_log(run_id). `signals` lets the post lane
    credit a link's publisher by name (Signal.publisher, else Signal.author)."""
    minds = load_minds()
    prompts = {}
    if vetoed and not [a for a in vetoed if a.mind in minds and a.type != "follow"]:
        # Only persona drafts can be revised, and persona text no longer ships (lanes.post_lane). The
        # lanes don't redraft vetoed replies or captions, so there's nothing to retry; returning
        # nothing ends cli.run's retry loop instead of captioning the same source twice.
        print(f"[decide] retry skipped: none of the {len(vetoed)} vetoed actions is a revisable persona draft")
        return []
    if vetoed:
        used = {a.narrative_id for a in vetoed}
        targets = [asdict(n) for n in narratives if n.id in used]
        for name in minds:
            own = [
                {"type": a.type, "text": a.text, "narrative_id": a.narrative_id, "target_url": a.target_url,
                 "target_handle": a.target_handle, "critique": a.critique}
                for a in vetoed if a.mind == name
            ]
            if own:
                prompts[name] = REVISE.format(vetoed=json.dumps(own, indent=1))
    else:
        targets = target_narratives(narratives)
        prompts = dict.fromkeys(minds, "")
    base = PROMPT.format(mission=mission, lessons=lessons, narratives=json.dumps(targets, indent=1), n=per_mind)
    with ThreadPoolExecutor(max_workers=len(prompts) or 1) as pool:
        results = pool.map(
            lambda kv: _run_mind(llm, kv[0], minds[kv[0]], base + kv[1], narratives), prompts.items()
        )
    actions = [a for batch in results for a in batch]
    verb = "revised into" if vetoed else "proposed"
    print(f"[decide] {len(prompts)} minds {verb} {len(actions)} actions")
    sources = {s.url: s.publisher or s.author for s in signals or [] if s.url and (s.publisher or s.author)}
    shipped, decisions = lanes.run(llm, mission, lessons, narratives, actions, revising=bool(vetoed),
                                   cfg=cfg or settings.load(), budget=budget, sources=sources)
    lanes.PENDING.extend(decisions)
    return shipped
