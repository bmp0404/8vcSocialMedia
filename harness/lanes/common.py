"""Shared by both lanes: the run context and the Eval step (score, critique, write to memory)."""

import re
from dataclasses import dataclass

from harness import memory
from harness.llm import LLM
from harness.models import Action
from harness.qualify import qualify


@dataclass
class LaneContext:
    llm: LLM
    mission: str
    run_id: int
    per_mind: int = 3  # max actions each mind may propose
    limit: int = 5  # reply lane: targets considered; post lane: narratives considered


def gist(text: str, words: int = 8) -> str:
    """First few words of a signal, for mock drafts."""
    text = re.sub(r"^SAMPLE:\s*", "", text or "").strip()
    return " ".join(text.split()[:words]).rstrip(".,:;!?'\"")


def evaluate(ctx: LaneContext, lane: str, actions: list[Action], mem: dict) -> list[Action]:
    """Eval: panel + critic + rules, then one row per candidate in memory. Returns all, best first."""
    for a in actions:
        a.lane, a.run_id = lane, ctx.run_id
    qualify(ctx.llm, ctx.mission, actions, mem.get("recent_texts", []))
    memory.save_actions(actions)
    return actions
