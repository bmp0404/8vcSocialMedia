"""Orient: cluster raw signals into narratives and score them against the mission."""

import json
from dataclasses import asdict

from harness.llm import LLM
from harness.models import Narrative, Signal

SYSTEM = """You are the strategist for the brand account on X described in the MISSION. Group the
observed signals into distinct narratives the account could join with an original post. Score each:
- relevance (0-1): fit with the mission's subjects and tone
- velocity (0-1): how fast attention is moving right now
Suggest a one-line `angle` the account could take, in the mission's voice (a specific take beats
generic praise). Each narrative goes to a different writer, so make them genuinely distinct: no two
angles may rest on the same premise or joke (merge near-duplicates into one narrative instead).
Down-rank narratives the account has already covered recently (see MEMORY).
Leave out signals that are off-mission or low value instead of forcing them into a narrative, and
never build an angle on a brand fact (feature, number, customer, launch) that isn't in the MISSION."""

PROMPT = """MISSION:
{mission}

MEMORY:
{memory}

SIGNALS:
{signals}

Return JSON: {{"narratives": [{{"id": "kebab-slug", "title": "...", "summary": "...",
"relevance": 0.0, "velocity": 0.0, "angle": "...", "signal_urls": ["..."]}}]}}"""

MOCK_ANGLES = ["the contrarian take", "the receipts", "the inside joke", "the hype check", "the zoom-out"]


def orient(llm: LLM, mission: str, signals: list[Signal], top: int = 5, memory: str = "") -> list[Narrative]:
    payload = json.dumps([asdict(s) for s in signals], indent=1)[:60_000]

    def mock():
        return {
            "narratives": [
                {
                    "id": f"sample-{i}",
                    "title": " ".join(s.text.removeprefix("SAMPLE: ").split()[:9]).rstrip(".,;:"),
                    "summary": s.text,
                    "relevance": 0.8 - i * 0.1,
                    "velocity": 0.7,
                    "angle": MOCK_ANGLES[i % len(MOCK_ANGLES)],
                    "signal_urls": [s.url] if s.url else [],
                }
                for i, s in enumerate(signals[:top])
            ]
        }

    data = llm.complete_json(
        SYSTEM, PROMPT.format(mission=mission, memory=memory or "(none)", signals=payload), mock=mock
    )
    narratives = [Narrative(**n) for n in data.get("narratives", [])]
    narratives.sort(key=lambda n: n.relevance * n.velocity, reverse=True)
    return narratives[:top]
