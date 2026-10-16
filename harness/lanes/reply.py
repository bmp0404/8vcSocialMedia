"""Reply lane: observe posts worth jumping into -> decide reply/quote/mention/follow/skip -> eval.

Every observed post is a potential target, so each action carries the target post's url/author.
"""

import json
import math
from dataclasses import asdict

from harness import memory
from harness.lanes.common import LaneContext, evaluate, gist
from harness.llm import mock_score
from harness.minds import ask, load_minds
from harness.models import LANE_TYPES, Action, Signal

LANE = "reply"

PROMPT = """MISSION:
{mission}

MEMORY:
{memory}

TARGETS (posts and conversations the account could jump into):
{targets}

For each target, decide what action to take in your style: reply, quote, mention, follow, or skip.
- reply / quote: `text` is the reply or quote comment, under 280 characters
- mention: a new post addressed to the target's author; include their @handle in `text`
- follow: follow the target's author (empty `text`); only if they're likely to engage back
- skip: not worth it for you
Propose at most {n} non-skip actions in total. Fewer, better actions beat filler.

Return JSON: {{"actions": [{{"target_id": "t1", "type": "reply", "text": "...", "rationale": "..."}}]}}"""

# Offline fixtures: (type, template) per mind, cycled across targets.
MOCK_PLAYS = {
    "reply_sniper": [
        ("reply", "the part everyone in this thread is skipping: {gist}. that's the whole story."),
        ("quote", "saving this for when the timeline pretends it didn't see \"{gist}\" coming."),
    ],
    "rivalry_banterer": [
        ("reply", "respectfully, this take has the confidence of someone who only watched the highlights."),
        ("quote", "the replies to this are a better product than most launches this year."),
    ],
    "stat_nerd": [
        ("reply", "worth adding the number nobody's quoting here: [VERIFY: key stat on {gist}]."),
        ("quote", "one stat settles this thread: [VERIFY: stat on {gist}]. receipts > vibes."),
    ],
    "community_builder": [
        ("follow", ""),
        ("mention", "@{handle} you keep asking the right questions about {gist}. what's the next thing you'd want to see?"),
    ],
}


def _handle(s: Signal) -> str | None:
    return (s.author or "").lstrip("@") or None


def _heat(s: Signal) -> float:
    m = s.metrics or {}
    if "reply_score" in m:
        return float(m["reply_score"])
    g = lambda *keys: next((m[k] for k in keys if m.get(k) is not None), 0)  # noqa: E731
    return math.log1p(g("likes", "like_count") + 2 * g("reposts", "retweet_count")
                      + 3 * g("replies", "reply_count") + 2 * g("quotes", "quote_count"))


def observe(signals: list[Signal], mem: dict, limit: int) -> dict[str, Signal]:
    """Pick the hottest posts we haven't already engaged with; returns {target_id: signal}."""
    seen = set(mem["engaged_urls"])
    pool = [s for s in signals if s.url and s.url not in seen and _handle(s)]
    pool.sort(key=_heat, reverse=True)
    targets = {f"t{i}": s for i, s in enumerate(pool[:limit], 1)}
    print(f"[reply:observe] {len(targets)} targets (of {len(signals)} signals, {len(seen)} already engaged)")
    return targets


def _mock(name: str, targets: dict[str, Signal], n: int) -> dict:
    plays = MOCK_PLAYS.get(name, MOCK_PLAYS["reply_sniper"])
    offset = int(mock_score(name, lo=0, hi=len(targets) - 0.01))  # minds start on different targets
    items = list(targets.items())
    actions = []
    for i in range(len(items)):
        tid, s = items[(i + offset) % len(items)]
        if len(actions) >= n:
            actions.append({"target_id": tid, "type": "skip", "rationale": "nothing to add"})
            break
        t, text = plays[i % len(plays)]
        actions.append({
            "target_id": tid, "type": t, "text": text.format(gist=gist(s.text), handle=_handle(s)),
            "rationale": f"mock: {t} on a post with heat {_heat(s):.1f}",
        })
    return {"actions": actions}


def _to_action(name: str, raw: dict, targets: dict[str, Signal], mem: dict) -> Action | None:
    kind = str(raw.get("type", "")).lower()
    s = targets.get(raw.get("target_id")) or next(
        (t for t in targets.values() if raw.get("target_url") and t.url == raw.get("target_url")), None
    )
    if kind not in LANE_TYPES[LANE] or not s:
        return None
    handle, text = _handle(s), (raw.get("text") or "").strip()
    if kind in {"follow", "mention"} and handle.lower() in mem["engaged_handles"]:
        return None
    if kind == "follow":
        text = ""
    if kind == "mention" and f"@{handle}".lower() not in text.lower():
        text = f"@{handle} {text}"
    return Action(
        type=kind,
        text=text,
        mind=name,
        narrative_id=f"sig:{s.id or raw.get('target_id')}",
        rationale=raw.get("rationale", ""),
        target_url=s.url if kind in {"reply", "quote"} else None,
        target_handle=handle,
        context=asdict(s),
    )


def run(ctx: LaneContext, signals: list[Signal]) -> list[Action]:
    mem = memory.recall(LANE, context_text=" ".join(s.text for s in signals))
    targets = observe(signals, mem, ctx.limit)
    if not targets:
        return []
    payload = json.dumps(
        [{"target_id": tid, "author": s.author, "text": s.text, "url": s.url, "created_at": s.created_at,
          "metrics": s.metrics} for tid, s in targets.items()],
        indent=1,
    )
    prompt = PROMPT.format(mission=ctx.mission, memory=memory.format_recall(mem), targets=payload, n=ctx.per_mind)
    proposals = ask(ctx.llm, load_minds(LANE), prompt, lambda name: _mock(name, targets, ctx.per_mind), tag="reply:act")

    actions, followed = [], set()
    for name, raws in proposals.items():
        kept = 0
        for raw in raws:  # skips and invalid drafts don't count toward the per-mind cap
            a = _to_action(name, raw, targets, mem)
            if not a or (a.type == "follow" and a.target_handle in followed) or kept >= ctx.per_mind:
                continue
            if a.type == "follow":
                followed.add(a.target_handle)
            actions.append(a)
            kept += 1
    return evaluate(ctx, LANE, actions, mem)
