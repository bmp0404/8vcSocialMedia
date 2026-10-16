"""Post lane: observe trends/narratives (orient) -> original posts from the minds -> eval."""

import json
from dataclasses import asdict

from harness import learn, memory
from harness.lanes.common import LaneContext, evaluate, recall
from harness.minds import ask, load_minds
from harness.models import Action, Narrative, Signal
from harness.orient import orient

LANE = "post"

PROMPT = """MISSION:
{mission}

MEMORY:
{memory}

YOUR NARRATIVES (assigned to you; other minds write on other narratives):
{narratives}

Propose up to {n} original X posts in your style, at most one per narrative, each tied to one of YOUR
NARRATIVES by `narrative_id`. No replies, quotes, or @-mentions: these stand on their own. One post
each, not a thread: hard limit 280 characters (newlines count), and most land best at 120-220. The
narrative's `angle` is a hint, not a script.{shared}
Build each post on what is specific to its narrative (the tool, the failure, the moment in the
signals). The MISSION's example lines show the voice; don't recycle their jokes or images, or the
premise of anything in ALREADY SAID RECENTLY.

Return JSON: {{"actions": [{{"narrative_id": "...", "text": "...", "rationale": "..."}}]}}"""

MOCK_TEXT = {
    "agent_wrangler": [
        "{title}: two agents, one file, both sure they were first. a love story.",
        "me explaining {title} to my agents for the fourth time this week",
    ],
    "technical_explainer": [
        "{title}, in one line: {angle}. everything else is implementation detail.",
        "the failure mode in {title} nobody names: no shared state between the agents doing the work.",
    ],
    "build_in_public": [
        "what we keep coming back to on {title}: {angle}.",
        "belief we're building on: {title} is a coordination problem before it's a model problem.",
    ],
}


def _mock(name: str, narratives: list[Narrative], n: int) -> dict:
    templates = MOCK_TEXT.get(name, MOCK_TEXT["technical_explainer"])
    out = []
    for i, nar in enumerate(narratives[:n]):
        out.append({
            "narrative_id": nar.id,
            "text": templates[i % len(templates)].format(title=nar.title.lower(), angle=nar.angle),
            "rationale": f"mock: {nar.angle} on a narrative with relevance {nar.relevance:.1f}",
        })
    return {"actions": out}


def assign(narratives: list[Narrative], minds: list[str], cap: dict[str, int]) -> dict[str, list[Narrative]]:
    """Deal narratives (best first) round-robin so each mind writes on its own ones instead of every mind
    piling onto the top narrative with the same angle. Each mind gets ceil(N / M) narratives (capped by its
    quota); when there are fewer narratives than slots the deal wraps, and shared ones are flagged in the
    prompt so the second mind takes a different angle."""
    if not narratives or not minds:
        return {m: [] for m in minds}
    per = {m: max(1, min(cap.get(m, 1), -(-len(narratives) // len(minds)))) for m in minds}
    out: dict[str, list[Narrative]] = {m: [] for m in minds}
    k = 0
    for _ in range(max(per.values())):
        for m in minds:
            if len(out[m]) < per[m]:
                nar = narratives[k % len(narratives)]
                if nar not in out[m]:
                    out[m].append(nar)
                k += 1
    return out


def run(ctx: LaneContext, signals: list[Signal]) -> list[Action]:
    mem = recall(ctx, LANE, signals)
    mem_text = memory.format_recall(mem)
    narratives = orient(ctx.llm, ctx.mission, signals, top=ctx.limit, memory=mem_text)
    print(f"[post:observe] {len(narratives)} narratives: " + ", ".join(n.id for n in narratives))
    if not narratives:
        return []
    by_id = {n.id: n for n in narratives}
    minds = load_minds(LANE)
    cap = learn.quota(LANE, minds, ctx.per_mind)  # per_mind scaled by the learned mind weight

    mine = assign(narratives, [m for m in minds if cap[m] > 0], cap)
    print("[post:assign] " + "; ".join(f"{m}: {', '.join(n.id for n in ns)}" for m, ns in mine.items()))

    def prompt(name: str) -> str:
        shared = sorted({o for n in mine[name] for o, ns in mine.items() if o != name and n in ns})
        return PROMPT.format(
            mission=ctx.mission,
            memory=mem_text,
            narratives=json.dumps([asdict(n) for n in mine[name]], indent=1),
            n=min(cap[name], len(mine[name])),
            shared=(f" {', '.join(shared)} also has one of these narratives: take a different angle than the "
                    "obvious one." if shared else ""),
        )

    proposals = ask(ctx.llm, {m: minds[m] for m in mine}, prompt,
                    lambda name: _mock(name, mine[name], cap[name]), tag="post:act")

    actions = []
    for name, raws in proposals.items():
        allowed = {n.id for n in mine[name]}
        used: set[str] = set()
        for raw in raws:
            if len(used) >= cap[name]:
                break
            nid = raw.get("narrative_id")
            if raw.get("type", "post") != "post" or nid not in allowed or nid in used:
                continue  # one post per assigned narrative per mind
            used.add(nid)
            nar = by_id.get(nid)
            actions.append(Action(
                type="post",
                text=(raw.get("text") or "").strip(),
                mind=name,
                narrative_id=nar.id if nar else str(raw.get("narrative_id", "")),
                rationale=raw.get("rationale", ""),
                context=asdict(nar) if nar else {},
            ))
    return evaluate(ctx, LANE, actions, mem)
