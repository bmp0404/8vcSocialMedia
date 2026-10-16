"""Learn: rewrite memory/lessons.md from what was approved, killed, and what actually performed."""

import json

from harness import config, memory
from harness.llm import LLM
from harness.reward import score

SYSTEM = """You are the performance analyst for a company's X account. From what humans approved or
killed and what actually performed on X, write concise, actionable lessons (max 15 bullets) for the content
writers: hooks, formats, topics, and tones that work; what to stop doing. Real X metrics outrank human
reviews. Keep still-valid lessons from the previous version."""


def write_lessons(llm: LLM, examples: list[dict]) -> None:
    """examples: scored drafts, best first (see evolve._examples)."""
    prompt = f"PREVIOUS LESSONS:\n{config.lessons()}\n\nEVIDENCE (best first):\n{json.dumps(examples[:40], indent=1)}"
    config.LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.LESSONS_PATH.write_text(llm.complete(SYSTEM, prompt), encoding="utf-8")
    print(f"[learn] lessons -> {config.LESSONS_PATH}")


def learn(llm: LLM) -> None:
    from harness.evolve import _examples  # evolve imports this module

    rows = memory.scored_actions()
    rewards = score(rows)
    if not rewards:
        print("[learn] nothing scored yet: review drafts in the dashboard or `record` real metrics first")
        return
    write_lessons(llm, _examples(rows, rewards))
