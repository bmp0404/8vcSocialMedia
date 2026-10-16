"""Post lane: observe trends/narratives (orient) -> original posts from the minds -> eval."""

import json
from dataclasses import asdict

from harness import memory
from harness.lanes.common import LaneContext, evaluate
from harness.minds import ask, load_minds
from harness.models import Action, Narrative, Signal
from harness.orient import orient

LANE = "post"

PROMPT = """MISSION:
{mission}

MEMORY:
{memory}

NARRATIVES TO WORK WITH:
{narratives}

Propose up to {n} original X posts in your style, each tied to one narrative. No replies, quotes,
or @-mentions: these stand on their own. Under 280 characters each.

Return JSON: {{"actions": [{{"narrative_id": "...", "text": "...", "rationale": "..."}}]}}"""

MOCK_TEXT = {
    "meme_lord": [
        'nobody:\nabsolutely nobody:\nthe timeline at 2am: "{title}"',
        "me explaining {title} to my group chat for the fourth time this week",
    ],
    "rivalry_banterer": [
        "unpopular opinion on {title}: {angle} is the only honest read. replies are open.",
        "everyone has a take on {title}. most of them are the same take wearing a different hat.",
    ],
    "stat_nerd": [
        "{title}. the one number that matters here: [VERIFY: stat]. everything else is vibes.",
        "receipts on {title}: [VERIFY: stat]. {angle}, in one line.",
    ],
}


def _mock(name: str, narratives: list[Narrative], n: int) -> dict:
    templates = MOCK_TEXT.get(name, MOCK_TEXT["rivalry_banterer"])
    out = []
    for i, nar in enumerate(narratives[:n]):
        out.append({
            "narrative_id": nar.id,
            "text": templates[i % len(templates)].format(title=nar.title.lower(), angle=nar.angle),
            "rationale": f"mock: {nar.angle} on a narrative with relevance {nar.relevance:.1f}",
        })
    return {"actions": out}


def run(ctx: LaneContext, signals: list[Signal]) -> list[Action]:
    mem = memory.recall(LANE, context_text=" ".join(s.text for s in signals))
    mem_text = memory.format_recall(mem)
    narratives = orient(ctx.llm, ctx.mission, signals, top=ctx.limit, memory=mem_text)
    print(f"[post:observe] {len(narratives)} narratives: " + ", ".join(n.id for n in narratives))
    if not narratives:
        return []
    by_id = {n.id: n for n in narratives}
    prompt = PROMPT.format(
        mission=ctx.mission,
        memory=mem_text,
        narratives=json.dumps([asdict(n) for n in narratives], indent=1),
        n=ctx.per_mind,
    )
    proposals = ask(ctx.llm, load_minds(LANE), prompt, lambda name: _mock(name, narratives, ctx.per_mind), tag="post:act")

    actions = []
    for name, raws in proposals.items():
        for raw in raws[: ctx.per_mind]:
            if raw.get("type", "post") != "post":
                continue
            nar = by_id.get(raw.get("narrative_id"))
            actions.append(Action(
                type="post",
                text=(raw.get("text") or "").strip(),
                mind=name,
                narrative_id=nar.id if nar else str(raw.get("narrative_id", "")),
                rationale=raw.get("rationale", ""),
                context=asdict(nar) if nar else {},
            ))
    return evaluate(ctx, LANE, actions, mem)
