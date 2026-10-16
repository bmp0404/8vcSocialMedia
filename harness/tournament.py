"""Best-of-N: a Swiss-style pairwise tournament over the drafts that survived qualify.

Pairwise "which spreads further?" judgments are more reliable than absolute 0-1 scores, so the
panel score seeds the bracket and Elo from head-to-head rounds sets the final order.
"""

import json
import os
import random

from harness import memory, retrieval
from harness.actions import is_public
from harness.llm import LLM
from harness.models import Action

K = 32  # Elo step

# The core is what `tune` evolves against the dataset (stored in kv "judge_core"); the rules never change.
JUDGE_CORE = """You judge pairs of draft X posts for a company's account. For each pair, pick the one more
likely to spread among the target audience (reposts, quotes, replies), weighing the opening line most."""

JUDGE_RULES = """A draft that breaks the mission's fact sheet or reads like an ad always loses.
Some drafts list `similar_real_posts`: real dev-tool posts close to the draft, with how they performed.
Treat them as evidence of what this audience rewards, but judge each draft's own writing, and remember a
big account's numbers say little about what a small account can expect."""

# Off by default: in backtests (90 same-account pairs) evidence scored 82/90 vs. 84/90 without it.
EVIDENCE = os.getenv("HARNESS_JUDGE_EVIDENCE", "0") == "1"


def judge_system(core: str | None = None) -> str:
    return f"{core or memory.kv_get('judge_core') or JUDGE_CORE}\n\n{JUDGE_RULES}"

JUDGE_PROMPT = """MISSION:
{mission}

PAIRS:
{pairs}

Return JSON: {{"winners": {{"<pair id>": "A" or "B"}}}}"""


def _view(a: Action, evidence: bool) -> dict:
    view = {"type": a.type, "text": a.text, "replying_to": a.target_url or a.target_handle}
    if evidence:
        # Backtests judge dataset posts (mind "dataset", narrative_id = account): hide that account's posts.
        exclude = {a.narrative_id} if a.mind == "dataset" else set()
        if similar := retrieval.similar(a.text, k=3, exclude_accounts=exclude):
            view["similar_real_posts"] = similar
    return view


def judge(llm: LLM, mission: str, pairs: list[tuple[Action, Action]], rng: random.Random,
          evidence: bool | None = None, core: str | None = None) -> list[Action | None]:
    """One LLM call judges every pair. Sides are shuffled to cancel position bias.
    `evidence` adds similar real posts per draft (default: HARNESS_JUDGE_EVIDENCE); `core` overrides the
    judge's evolvable instructions (used by `tune` to try a candidate)."""
    if not pairs:
        return []
    evidence = EVIDENCE if evidence is None else evidence
    sides = [(a, b) if rng.random() < 0.5 else (b, a) for a, b in pairs]
    payload = [{"pair": str(i), "A": _view(a, evidence), "B": _view(b, evidence)} for i, (a, b) in enumerate(sides)]
    out = llm.complete_json(
        judge_system(core),
        JUDGE_PROMPT.format(mission=mission, pairs=json.dumps(payload, indent=1)),
        mock=lambda: {"winners": {str(i): "A" if a.predicted_score >= b.predicted_score else "B"
                                  for i, (a, b) in enumerate(sides)}},
    ).get("winners", {})
    picks = []
    for i, (a, b) in enumerate(sides):
        w = str(out.get(str(i), "")).strip().upper()
        picks.append(a if w == "A" else b if w == "B" else None)
    return picks


def _expected(ra: float, rb: float) -> float:
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def tournament(llm: LLM, mission: str, actions: list[Action], rounds: int = 3, field: int = 16,
               rng: random.Random | None = None) -> list[Action]:
    """Rank `actions` (already sorted by score). Only the top `field` public actions play; the rest keep their
    order. Outreach and internal actions aren't judged on spread, so they follow the public ones."""
    rng = rng or random.Random()
    public = [a for a in actions if is_public(a)]
    others = [a for a in actions if not is_public(a)]
    pool, rest = public[:field], public[field:] + others
    if len(pool) < 2 or rounds <= 0:
        ordered = public + others
        for i, a in enumerate(ordered, 1):
            a.rank = i
        return ordered

    elo = {a.id: 1500.0 for a in pool}
    wins = {a.id: 0 for a in pool}
    losses = {a.id: 0 for a in pool}
    played: set[frozenset] = set()

    for r in range(rounds):
        # Swiss pairing: sort by score so far, pair neighbours, avoid rematches where possible.
        order = sorted(pool, key=lambda a: (-wins[a.id], -elo[a.id], -a.predicted_score))
        pairs, waiting = [], order[:]
        while len(waiting) >= 2:
            a = waiting.pop(0)
            j = next((j for j, b in enumerate(waiting) if frozenset((a.id, b.id)) not in played), 0)
            b = waiting.pop(j)
            pairs.append((a, b))
            played.add(frozenset((a.id, b.id)))
        for (a, b), w in zip(pairs, judge(llm, mission, pairs, rng)):
            if w is None:
                continue
            l = b if w is a else a
            e = _expected(elo[w.id], elo[l.id])
            elo[w.id] += K * (1 - e)
            elo[l.id] -= K * (1 - e)
            wins[w.id] += 1
            losses[l.id] += 1
        print(f"[tournament] round {r + 1}: {len(pairs)} matches")

    for a in pool:
        a.elo = round(elo[a.id])
        a.record = f"{wins[a.id]}-{losses[a.id]}"
    ranked = sorted(pool, key=lambda a: elo[a.id], reverse=True) + rest
    for i, a in enumerate(ranked, 1):
        a.rank = i
    return ranked
