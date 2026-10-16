"""Learn: compare predicted vs. actual performance and rewrite the lessons file the minds read."""

import json

from harness import config, memory
from harness.llm import LLM

SYSTEM = """You are the account's performance analyst. Compare what we predicted would work with what
actually happened. Write concise, actionable lessons (max 15 bullets) for the content minds:
which minds, formats, narratives, and timing worked; what to stop doing; where the audience-panel
predictions were miscalibrated. Keep any still-valid lessons from the previous version."""

PROMPT = """PREVIOUS LESSONS:
{lessons}

HISTORY (recommendation + actual metrics):
{history}

Write the new lessons file as markdown."""


def learn(llm: LLM) -> str | None:
    history = memory.scored_history()
    if not history:
        print("[learn] no outcomes recorded yet; run `python -m harness record <file>` first")
        return None
    text = llm.complete(SYSTEM, PROMPT.format(lessons=config.lessons(), history=json.dumps(history, indent=1)))
    config.LESSONS_PATH.parent.mkdir(exist_ok=True)
    config.LESSONS_PATH.write_text(text, encoding="utf-8")
    print(f"[learn] updated {config.LESSONS_PATH} from {len(history)} outcomes")
    return text
