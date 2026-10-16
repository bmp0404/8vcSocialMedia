"""Reply lane: observe posts worth jumping into -> decide reply/quote/mention/follow or decline -> eval.

Every observed post is a potential target, so each action carries the target post's url/author.
Posts by watchlist accounts (the targets file) are boosted, priority ones more, and each target's
`relationship` and `notes` go into the drafting prompt. Follows and mentions are limited to watchlist
accounts when a watchlist exists. A target no mind wants to engage with becomes one `wait` or
`no_response` row in memory with a structured reason; declines are never emailed.
"""

import json
import math
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone

from harness import config, memory
from harness.lanes.common import LaneContext, evaluate, gist
from harness.llm import mock_score
from harness.minds import ask, load_minds
from harness.models import DECLINE_TYPES, LANE_TYPES, REASONS, Action, Signal, norm_reason

LANE = "reply"
PRIORITY_BOOST = 2.0  # added to a post's heat (log-scale engagement) when its author is a priority target
WATCHLIST_BOOST = 1.0  # ... or any other watchlist account
MAX_CANDIDATES = 12  # watchlist accounts offered for follow / mention

PROMPT = """MISSION:
{mission}

MEMORY:
{memory}

TARGETS (posts and conversations the account could jump into). `watchlist` says who the author is to
us: `relationship` (peer, near_competitor, adjacent_infra, ...) and `notes` on how to engage them;
null means the author isn't on our watchlist. `age_hours` is how old the post is.
{targets}

FOLLOW / MENTION CANDIDATES (watchlist accounts; follows and mentions may only go to these):
{candidates}

For each target, decide what to do in your style:
- reply / quote: `text` is the reply or quote comment, under 280 characters
- mention: a new post addressed to a candidate; include their @handle in `text`. Use `target_id` if it
  riffs on one of their posts, or `handle` for a candidate with no post here
- follow: follow a candidate (empty `text`; `target_id` or `handle`); only if they're likely to engage back
- wait: worth revisiting later (thread still forming, nothing sharp to say yet)
- no_response: not worth engaging (off-topic, low value, stale, wrong audience, would need invented facts)
Every wait / no_response needs a `reason` from: {reasons}. Decline because of the target itself (off-topic,
low value, stale, risky). If a target is fine but you just didn't pick it, leave it out; don't decline it.
Propose at most {n} engagement actions (reply/quote/mention/follow) in total. Fewer, better actions beat
filler. Other minds are drafting on the same targets in parallel and only one reply per post ships.
YOUR FOCUS TARGETS: {focus}. For each of these, either draft your best engagement or decline it with a
reason; the account's decision on that post depends on you. Declining is the right call for posts off
the MISSION's subjects, however popular; posts squarely on them, from priority accounts, deserve a real
attempt. Draft on other targets only when you have something clearly better than a focus target. Replies read best under ~200 characters.

Return JSON: {{"actions": [{{"target_id": "t1", "type": "reply", "text": "...", "rationale": "..."}},
{{"target_id": "t2", "type": "no_response", "reason": "nothing_to_add", "rationale": "..."}}]}}"""

# Offline fixtures: (type, template) per mind, cycled across targets.
MOCK_PLAYS = {
    "reply_sniper": [
        ("reply", "the part everyone in this thread is skipping: {gist}. that's the whole story."),
        ("quote", "saving this for the next time two agents swear they each fixed it: \"{gist}\""),
    ],
    "agent_wrangler": [
        ("reply", "two agents, one file, both confident. what could go wrong."),
        ("no_response", ""),
    ],
    "technical_explainer": [
        ("reply", "the failure mode here isn't the model, it's that \"{gist}\" happens with no shared state."),
        ("wait", ""),
    ],
    "friendly_rival": [
        ("follow", ""),
        ("mention", "@{handle} congrats on shipping. \"{gist}\" is where this is all going."),
    ],
}


def _handle(s: Signal) -> str | None:
    return (s.author or "").lstrip("@") or None


def _watch(handle: str | None) -> dict | None:
    return config.watchlist().get((handle or "").lower())


def _age_hours(s: Signal) -> float | None:
    if not s.created_at:
        return None
    try:
        ts = datetime.fromisoformat(str(s.created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1)


def _heat(s: Signal) -> float:
    m = s.metrics or {}
    if "reply_score" in m:
        base = float(m["reply_score"])
    else:
        g = lambda *keys: next((m[k] for k in keys if m.get(k) is not None), 0)  # noqa: E731
        base = math.log1p(g("likes", "like_count") + 2 * g("reposts", "retweet_count")
                          + 3 * g("replies", "reply_count") + 2 * g("quotes", "quote_count"))
    acct = _watch(_handle(s))
    if acct:
        base += PRIORITY_BOOST if acct.get("priority") else WATCHLIST_BOOST
    return base


def observe(signals: list[Signal], mem: dict, limit: int) -> dict[str, Signal]:
    """Pick the hottest posts (watchlist-boosted) we haven't engaged with; returns {target_id: signal}."""
    seen = set(mem["engaged_urls"]) | set(mem.get("declined_urls", []))
    pool = [s for s in signals if s.url and s.url not in seen and _handle(s)]
    pool.sort(key=_heat, reverse=True)
    targets = {f"t{i}": s for i, s in enumerate(pool[:limit], 1)}
    on_list = sum(bool(_watch(_handle(s))) for s in targets.values())
    print(f"[reply:observe] {len(targets)} targets ({on_list} from the watchlist) of {len(signals)} signals; "
          f"{len(mem['engaged_urls'])} already engaged, {len(mem.get('declined_urls', []))} recently declined")
    return targets


def candidates(targets: dict[str, Signal], mem: dict) -> list[dict]:
    """Watchlist accounts offered for follow/mention: authors of current targets first, then priority."""
    engaged = set(mem["engaged_handles"])
    accounts = config.targets()["accounts"]
    authors = [(_handle(s) or "").lower() for s in targets.values()]
    order = sorted(
        accounts,
        key=lambda a: (a["handle"].lower() not in authors, not a.get("priority"), a["handle"].lower()),
    )
    out = []
    for a in order:
        if a["handle"].lower() in engaged:
            continue
        out.append({k: a.get(k) for k in ("handle", "name", "relationship", "priority", "notes")})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def _focus(target_ids: list[str], minds: list[str]) -> dict[str, list[str]]:
    """Round-robin targets (hottest first) across minds so every target gets a considered decision
    instead of every mind piling onto the busiest post."""
    out = {m: [] for m in minds}
    for i, tid in enumerate(target_ids):
        if minds:
            out[minds[i % len(minds)]].append(tid)
    return out


def _mock(name: str, targets: dict[str, Signal], n: int) -> dict:
    plays = MOCK_PLAYS.get(name, MOCK_PLAYS["reply_sniper"])
    offset = int(mock_score(name, lo=0, hi=len(targets) - 0.01))  # minds start on different targets
    items = list(targets.items())
    actions = []
    for i in range(len(items)):
        tid, s = items[(i + offset) % len(items)]
        if len(actions) >= n:
            actions.append({"target_id": tid, "type": "no_response", "reason": "nothing_to_add",
                            "rationale": "mock: nothing to add"})
            break
        t, text = plays[i % len(plays)]
        actions.append({
            "target_id": tid, "type": t, "text": text.format(gist=gist(s.text), handle=_handle(s)),
            "reason": "nothing_to_add" if t in DECLINE_TYPES else None,
            "rationale": f"mock: {t} on a post with heat {_heat(s):.1f}",
        })
    return {"actions": actions}


def _resolve(raw: dict, targets: dict[str, Signal]) -> Signal | None:
    return targets.get(raw.get("target_id")) or next(
        (t for t in targets.values() if raw.get("target_url") and t.url == raw.get("target_url")), None
    )


def _to_action(name: str, raw: dict, targets: dict[str, Signal], mem: dict) -> Action | None:
    kind = str(raw.get("type", "")).lower()
    if kind not in LANE_TYPES[LANE]:
        return None
    s = _resolve(raw, targets)
    handle = (raw.get("handle") or raw.get("target_handle") or "").lstrip("@") or (_handle(s) if s else None)
    if kind in {"reply", "quote"} and not s:
        return None
    if not handle:
        return None
    acct = _watch(handle)
    if kind in {"follow", "mention"}:
        if config.watchlist() and not acct:
            return None  # follows and mentions only go to watchlist accounts
        if handle.lower() in mem["engaged_handles"]:
            return None
        if acct:
            handle = acct["handle"]
    text = (raw.get("text") or "").strip()
    if kind == "follow":
        text = ""
    if kind == "mention" and f"@{handle}".lower() not in text.lower():
        text = f"@{handle} {text}"
    context = asdict(s) if s else {"author": f"@{handle}", "text": ""}
    if acct:
        context["watchlist"] = {k: acct.get(k) for k in ("name", "relationship", "priority", "notes")}
    return Action(
        type=kind,
        text=text,
        mind=name,
        narrative_id=f"sig:{s.id or raw.get('target_id')}" if s else f"acct:{handle.lower()}",
        rationale=raw.get("rationale", ""),
        target_url=s.url if s and kind in {"reply", "quote"} else None,
        target_handle=handle,
        context=context,
    )


def _declines(ctx: LaneContext, targets: dict[str, Signal], proposals: dict, engaged_ids: set[str]) -> list[Action]:
    """One wait/no_response row per target that no mind chose to engage with."""
    out = []
    for tid, s in targets.items():
        if tid in engaged_ids:
            continue
        votes = [(name, r) for name, raws in proposals.items() for r in raws
                 if _resolve(r, targets) is s and str(r.get("type", "")).lower() in DECLINE_TYPES | {"skip"}]
        kinds = Counter("wait" if str(r.get("type")).lower() == "wait" else "no_response" for _, r in votes)
        reasons = Counter(norm_reason(r.get("reason")) for _, r in votes)
        kind = "wait" if kinds["wait"] > kinds["no_response"] else "no_response"
        reason = reasons.most_common(1)[0][0] if reasons else "nothing_to_add"
        why = "; ".join(f"{name}: {r.get('rationale', '').strip()}" for name, r in votes if r.get("rationale"))
        acct = _watch(_handle(s))
        context = asdict(s)
        if acct:
            context["watchlist"] = {k: acct.get(k) for k in ("name", "relationship", "priority", "notes")}
        out.append(Action(
            type=kind, text="", mind=",".join(sorted({n for n, _ in votes})) or "chorus",
            narrative_id=f"sig:{s.id or tid}", rationale=why or "no mind proposed an action",
            target_url=s.url, target_handle=_handle(s), context=context, reason=reason,
            lane=LANE, run_id=ctx.run_id,
        ))
    return out


def run(ctx: LaneContext, signals: list[Signal]) -> list[Action]:
    mem = memory.recall(LANE, context_text=" ".join(s.text for s in signals))
    targets = observe(signals, mem, ctx.limit)
    if not targets:
        return []
    payload = json.dumps(
        [{"target_id": tid, "author": s.author, "text": s.text, "url": s.url, "created_at": s.created_at,
          "age_hours": _age_hours(s), "metrics": {k: v for k, v in s.metrics.items() if k in
                                                  ("likes", "reposts", "replies", "quotes", "views")},
          "watchlist": ({k: a.get(k) for k in ("name", "relationship", "priority", "notes")}
                        if (a := _watch(_handle(s))) else None)}
         for tid, s in targets.items()],
        indent=1,
    )
    cands = candidates(targets, mem)
    minds = load_minds(LANE)
    focus = _focus(list(targets), list(minds))

    def prompt(name: str) -> str:
        return PROMPT.format(
            mission=ctx.mission, memory=memory.format_recall(mem), targets=payload,
            candidates=json.dumps(cands, indent=1) if cands else "(no watchlist; follow/mention target authors only)",
            reasons=", ".join(REASONS), n=ctx.per_mind, focus=", ".join(focus[name]) or "(none)",
        )

    proposals = ask(ctx.llm, minds, prompt, lambda name: _mock(name, targets, ctx.per_mind), tag="reply:act")

    actions, followed, engaged_ids = [], set(), set()
    for name, raws in proposals.items():
        kept = 0
        for raw in raws:  # declines and invalid drafts don't count toward the per-mind cap
            a = _to_action(name, raw, targets, mem)
            if not a or (a.type == "follow" and a.target_handle in followed) or kept >= ctx.per_mind:
                continue
            if a.type == "follow":
                followed.add(a.target_handle)
            if (s := _resolve(raw, targets)) is not None:
                engaged_ids |= {tid for tid, t in targets.items() if t is s}
            actions.append(a)
            kept += 1

    declines = _declines(ctx, targets, proposals, engaged_ids)
    if declines:
        memory.save_actions(declines)
        print(f"[reply:act] declined {len(declines)} targets: "
              + ", ".join(f"{a.type}({a.reason}) @{a.target_handle}" for a in declines))
    return evaluate(ctx, LANE, actions, mem)
