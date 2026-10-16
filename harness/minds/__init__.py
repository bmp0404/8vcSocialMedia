"""Decide: a chorus of "media minds" each draft actions in parallel.

Each mind can have several prompt variants (see memory.variants); the bandit picks which variants
draft each run, and evolve.py writes new ones. Seed prompts live in harness/minds/prompts/*.md. Minds
draft by calling action tools (harness/actions); a seed file's `tools:` first line sets which ones it gets.
Every call is checked against what the minds were shown, the CRM, and recent actions before it counts.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from harness import actions, config, memory
from harness.llm import LLM
from harness.models import Action, Narrative

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

PEOPLE WE KNOW (CRM; relationship actions may only target these people or people in the narratives):
{contacts}

RECENT ACTIONS (approved in the last 30 days; don't repeat them, follow up on them if due):
{recent}

CLIENT MEDIA AVAILABLE FOR CLIPS:
{media}

Propose up to {n} actions in your lane by calling the action tools, one call per action, all in this
reply. Every draft should earn its place: specific over generic, true to the fact sheet, and for public
posts a real shot at spreading with developers, with a scroll-stopping first line and one idea.
Never invent a fact or stat; if you need one you can't verify from the inputs, write [VERIFY: ...] instead.
Each X post is under 280 characters; for a thread, separate posts with a line containing only ---. Only
target posts and people that appear above. If the best move is to wait, say so with hold_off."""


def _compact_contacts(contacts: list[dict]) -> str:
    keep = ("name", "role", "x", "github", "email", "org", "outlet", "stage", "tags", "notes", "knows")
    rows = [{k: c[k] for k in keep if c.get(k)} for c in contacts if not c.get("do_not_contact")]
    return "\n".join(json.dumps(r) for r in rows) or "(none)"


def _compact_recent(history: list[dict]) -> str:
    rows = [{"type": h["type"], "to": h.get("target_person") or h.get("target_handle") or h.get("target_url"),
             "channel": h.get("channel", "x"), "when": h["reviewed_at"][:10], "text": (h.get("text") or "")[:120]}
            for h in history[:40]]
    return "\n".join(json.dumps(r) for r in rows) or "(none yet)"


def _mock_calls(variant: dict, types: list[str], ctx: actions.Context, narratives: list[Narrative]):
    """Offline backend: one call per tool the mind has, so every action type gets exercised."""
    n = narratives[variant["version"] % len(narratives)] if narratives else None
    return [(actions.BY_TYPE[t].tool, actions.mock_args(actions.BY_TYPE[t], ctx, variant["id"], n.id if n else ""))
            for t in types]


def _run_variant(llm: LLM, variant: dict, prompt: str, narratives: list[Narrative], ctx: actions.Context) -> list[Action]:
    types = [t for t in config.mind_tools().get(variant["mind"], actions.X_TYPES) if t in actions.BY_TYPE]
    try:
        calls = llm.complete_tools(
            variant["prompt"], prompt, actions.tool_schemas(types),
            mock=lambda: _mock_calls(variant, types, ctx, narratives),
        )
    except Exception as e:  # noqa: BLE001 - one bad mind shouldn't sink the chorus
        print(f"[decide] {variant['id']} failed: {e}")
        return []
    out = []
    for tool, args in calls:
        try:
            a = actions.from_tool_call(tool, args, variant["mind"])
        except (KeyError, TypeError) as e:
            print(f"[decide] {variant['id']}: bad call to {tool}: {e}")
            continue
        a.variant = variant["id"]
        if a.type not in types:
            print(f"[decide] {variant['id']}: dropped {a.type}, not in its tool set")
        elif errors := actions.validate(a, ctx):
            print(f"[decide] {variant['id']}: dropped {a.type} ({'; '.join(errors)})")
        else:
            out.append(a)
    return out


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
    ctx = actions.Context.build(
        narratives, contacts=config.contacts(), history=memory.action_history(),
        swag=config.swag(), media=config.media(),
    )
    prompt = PROMPT.format(
        mission=mission,
        playbook=playbook,
        lessons=lessons,
        examples="\n".join(f"- {e}" for e in examples) or "(none yet)",
        narratives=json.dumps([asdict(n) for n in narratives], indent=1),
        contacts=_compact_contacts(ctx.contacts),
        recent=_compact_recent([h for h in ctx.history if h["reviewed_at"] >= _days_ago(30)]),
        media="\n".join(json.dumps(m) for m in ctx.media) or "(none)",
        n=per_mind,
    )
    with ThreadPoolExecutor(max_workers=len(variants) or 1) as pool:
        results = pool.map(lambda v: _run_variant(llm, v, prompt, narratives, ctx), variants)
    proposed = [a for batch in results for a in batch]
    print(f"[decide] {len(variants)} variants ({', '.join(v['id'] for v in variants)}) proposed {len(proposed)} valid actions")
    return proposed


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
