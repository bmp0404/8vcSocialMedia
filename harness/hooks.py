"""Hook pass: rewrite the opening line of the top drafts, where most of a post's reach is decided.

For each draft, the LLM writes 3 alternative hooks (only the opening changes). Each alternative plays
the original head-to-head; the first one that beats it and passes the critic is swapped in.
Every alternative is kept on the action so a reviewer can swap hooks in the dashboard.
"""

import json
import random
import re

from harness.actions import is_public
from harness.llm import LLM
from harness.models import Action
from harness.qualify import critic
from harness.tournament import judge

HOOK_SYSTEM = """You punch up the opening line ("hook") of draft X posts for a company's account. For each
draft, write 3 alternative hooks in clearly different styles (e.g. contrarian claim, hyper-specific detail,
"POV:"/relatable setup, blunt question). Each must flow into the unchanged rest of the post, keep the brand
voice, and add no product claims beyond the mission's fact sheet. No hashtags, no emoji openers."""

HOOK_PROMPT = """MISSION:
{mission}

DRAFTS (rewrite only `hook`; `rest` stays exactly as is):
{drafts}

Return JSON: {{"hooks": {{"<draft id>": ["...", "...", "..."]}}}}"""


def split_hook(text: str) -> tuple[str, str]:
    """Opening line = up to the first newline, else the first sentence. Returns (hook, rest)."""
    if "\n" in text:
        hook, rest = text.split("\n", 1)
        return hook, "\n" + rest
    m = re.search(r"(?<=[.!?])\s+", text)
    return (text[: m.start()], text[m.start():]) if m else (text, "")


def sharpen_hooks(llm: LLM, mission: str, ranked: list[Action], top: int = 5,
                  rng: random.Random | None = None) -> list[Action]:
    rng = rng or random.Random()
    targets = [a for a in ranked if is_public(a) and a.text.strip()][:top]
    if not targets:
        return ranked
    parts = {a.id: split_hook(a.text) for a in targets}

    alts = llm.complete_json(
        HOOK_SYSTEM,
        HOOK_PROMPT.format(mission=mission, drafts=json.dumps(
            [{"id": a.id, "type": a.type, "hook": parts[a.id][0], "rest": parts[a.id][1]} for a in targets], indent=1)),
        mock=lambda: {"hooks": {a.id: [f"(mock hook {i}) {parts[a.id][0]}" for i in range(1, 4)] for a in targets}},
    ).get("hooks", {})

    # Every alternative vs. its original, all judged in one call.
    challengers, pairs = [], []
    for a in targets:
        a.hook_alternatives = [h.strip() for h in alts.get(a.id, []) if isinstance(h, str) and h.strip()][:3]
        for h in a.hook_alternatives:
            c = Action(type=a.type, text=h + parts[a.id][1], mind=a.mind, narrative_id=a.narrative_id,
                       target_url=a.target_url, target_handle=a.target_handle, channel=a.channel, details=a.details)
            challengers.append((a, h, c))
            pairs.append((a, c))
    winners = judge(llm, mission, pairs, rng)

    # Hooks that beat the original must still pass the critic; take the first one that does.
    beat = [(orig, c) for (orig, _, c), w in zip(challengers, winners) if w is c]
    vetoes = critic(llm, mission, [c for _, c in beat]) if beat else {}
    for orig, c in beat:
        if orig.pre_hook_text is None and not vetoes.get(c.id, {}).get("veto"):
            orig.pre_hook_text, orig.text = orig.text, c.text
    kept = sum(a.pre_hook_text is not None for a in targets)
    print(f"[hooks] {len(targets)} drafts, {sum(len(a.hook_alternatives) for a in targets)} alternatives, {kept} hooks swapped in")
    return ranked
