"""Decide: a chorus of "media minds" each draft actions in parallel.

Each mind can have several prompt variants (see memory.variants); the bandit picks which variants
draft each run, and evolve.py writes new ones. Seed prompts live in harness/minds/prompts/*.md.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from harness.llm import LLM
from harness.models import ACTION_TYPES, Action, Narrative

PROMPT = """MISSION:
{mission}

VIRAL PLAYBOOK (distilled from what has worked):
{playbook}

LESSONS FROM PAST ROUNDS:
{lessons}

POSTS THAT WORKED (match the energy, never copy):
{examples}

NARRATIVES TO WORK WITH:
{narratives}

Propose up to {n} X actions in your style. Every draft should be a real shot at going viral with
developers: a scroll-stopping first line, one idea, specific over generic, true to the fact sheet.
Never invent a fact or stat; if you need one you can't verify from the inputs, write [VERIFY: ...] instead.
Allowed types: post, reply, quote, mention, follow. Replies and quotes need `target_url`; mentions and
follows need `target_handle`. Each post is under 280 characters; for a thread, separate posts with a line
containing only ---.

Return JSON: {{"actions": [{{"type": "...", "text": "...", "narrative_id": "...",
"target_url": null, "target_handle": null, "rationale": "..."}}]}}"""


def _run_variant(llm: LLM, variant: dict, prompt: str, narratives: list[Narrative]) -> list[Action]:
    mind = variant["mind"]

    def mock():
        n = narratives[variant["version"] % len(narratives)] if narratives else None
        return {
            "actions": [
                {
                    "type": "reply" if n and n.signal_urls else "post",
                    "text": f"[{variant['id']}] mock draft about {n.title if n else 'nothing'}",
                    "narrative_id": n.id if n else "",
                    "target_url": n.signal_urls[0] if n and n.signal_urls else None,
                    "rationale": "mock",
                }
            ]
        }

    try:
        data = llm.complete_json(variant["prompt"], prompt, mock=mock)
    except Exception as e:  # noqa: BLE001 - one bad mind shouldn't sink the chorus
        print(f"[decide] {variant['id']} failed: {e}")
        return []
    return [
        Action(
            type=a["type"],
            text=a.get("text", ""),
            mind=mind,
            variant=variant["id"],
            narrative_id=a.get("narrative_id", ""),
            rationale=a.get("rationale", ""),
            target_url=a.get("target_url"),
            target_handle=a.get("target_handle"),
        )
        for a in data.get("actions", [])
        if a.get("type") in ACTION_TYPES
    ]


def decide(
    llm: LLM,
    mission: str,
    variants: list[dict],
    narratives: list[Narrative],
    *,
    lessons: str,
    playbook: str,
    examples: list[str],
    per_mind: int = 3,
) -> list[Action]:
    prompt = PROMPT.format(
        mission=mission,
        playbook=playbook,
        lessons=lessons,
        examples="\n".join(f"- {e}" for e in examples) or "(none yet)",
        narratives=json.dumps([asdict(n) for n in narratives], indent=1),
        n=per_mind,
    )
    with ThreadPoolExecutor(max_workers=len(variants) or 1) as pool:
        results = pool.map(lambda v: _run_variant(llm, v, prompt, narratives), variants)
    actions = [a for batch in results for a in batch]
    print(f"[decide] {len(variants)} variants ({', '.join(v['id'] for v in variants)}) proposed {len(actions)} actions")
    return actions
