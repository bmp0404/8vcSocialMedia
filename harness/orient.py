"""Orient: cluster raw signals into narratives and score them against the mission."""

import json
from dataclasses import asdict

from harness.llm import LLM
from harness.models import Narrative, Signal

SYSTEM = """You are the social strategist for a company's X account. Group the observed signals into
distinct narratives the account could credibly join. Score each:
- relevance (0-1): how naturally the brand's point of view (see mission) fits the conversation
- velocity (0-1): how fast attention is moving right now
Suggest a one-line `angle`: the specific take that would make the brand's post the one people repost."""

PROMPT = """MISSION:
{mission}

SIGNALS:
{signals}

Return JSON: {{"narratives": [{{"id": "kebab-slug", "title": "...", "summary": "...",
"relevance": 0.0, "velocity": 0.0, "angle": "...", "signal_urls": ["..."]}}]}}"""


def orient(llm: LLM, mission: str, signals: list[Signal], top: int = 5) -> list[Narrative]:
    payload = json.dumps([asdict(s) for s in signals], indent=1)[:60_000]

    def mock():
        return {
            "narratives": [
                {
                    "id": f"sample-{i}",
                    "title": s.text[:60],
                    "summary": s.text,
                    "relevance": 0.8 - i * 0.1,
                    "velocity": 0.7,
                    "angle": "the coordination angle",
                    "signal_urls": [s.url] if s.url else [],
                }
                for i, s in enumerate(signals[:top])
            ]
        }

    data = llm.complete_json(SYSTEM, PROMPT.format(mission=mission, signals=payload), mock=mock)
    narratives = [Narrative(**n) for n in data.get("narratives", [])]
    narratives.sort(key=lambda n: n.relevance * n.velocity, reverse=True)
    return narratives[:top]
