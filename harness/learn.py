"""Learn: compare predicted vs. actual performance and rewrite the lessons file both lanes recall."""

import json

from harness import config, memory
from harness.llm import LLM

SYSTEM = """You are the performance analyst for the brand account in the MISSION. Compare what we
predicted would work with what actually happened. Write concise, actionable lessons (max 15 bullets)
for the content minds: which minds, action types, narratives, targets, and timing worked; what to stop
doing; where the audience-panel predictions were miscalibrated. Keep any still-valid lessons from the
previous version. Use exactly these sections: `## General`, `## Reply lane`, `## Post lane`."""

PROMPT = """MISSION:
{mission}

PREVIOUS LESSONS:
{lessons}

HISTORY (lane, action, prediction, and actual metrics):
{history}

Write the new lessons file as markdown, starting with `# Lessons`."""


def learn(llm: LLM) -> str | None:
    history = memory.scored_history()
    if not history:
        print("[learn] no outcomes recorded yet; run `python -m harness record <file>` first")
        return None
    text = llm.complete(
        SYSTEM,
        PROMPT.format(mission=config.mission(), lessons=config.lessons(), history=json.dumps(history, indent=1)),
    )
    config.LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.LESSONS_PATH.write_text(text, encoding="utf-8")
    print(f"[learn] updated {config.LESSONS_PATH} from {len(history)} outcomes")
    return text
