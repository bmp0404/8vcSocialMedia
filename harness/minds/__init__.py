"""Action: a chorus of persona "media minds" each propose actions in parallel.

Add a mind by dropping a markdown file into minds/prompts/; its filename is the mind's name.
Personas are brand-agnostic (the MISSION supplies brand, audience and voice). `MIND_LANES` says
which lane(s) a mind works in; minds missing from it work in both.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from harness.llm import LLM

PROMPTS_DIR = Path(__file__).parent / "prompts"

MIND_LANES = {
    "reply_sniper": {"reply"},
    "community_builder": {"reply"},
    "rivalry_banterer": {"reply", "post"},
    "stat_nerd": {"reply", "post"},
    "meme_lord": {"post"},
}

SYSTEM_SUFFIX = """

You write for the brand account described in the MISSION you are given: its audience, subjects,
voice and red lines come from there, not from you. Stay inside the mission."""


def load_minds(lane: str | None = None) -> dict[str, str]:
    minds = {p.stem: p.read_text(encoding="utf-8") for p in sorted(PROMPTS_DIR.glob("*.md"))}
    return {k: v for k, v in minds.items() if lane is None or lane in MIND_LANES.get(k, {"reply", "post"})}


def ask(
    llm: LLM,
    minds: dict[str, str],
    prompt: str,
    mock: Callable[[str], Any],
    tag: str = "decide",
) -> dict[str, list[dict]]:
    """Ask every mind the same prompt in parallel; returns {mind: [raw action dicts]}.

    `mock(name)` builds the offline fixture for a given mind.
    """

    def one(name: str) -> tuple[str, list[dict]]:
        try:
            data = llm.complete_json(minds[name] + SYSTEM_SUFFIX, prompt, mock=lambda: mock(name))
        except Exception as e:  # noqa: BLE001 - one bad mind shouldn't sink the chorus
            print(f"[{tag}] {name} failed: {e}")
            return name, []
        return name, [a for a in data.get("actions", []) if isinstance(a, dict)]

    with ThreadPoolExecutor(max_workers=len(minds) or 1) as pool:
        out = dict(pool.map(one, minds))
    print(f"[{tag}] {len(minds)} minds proposed {sum(map(len, out.values()))} actions")
    return out
