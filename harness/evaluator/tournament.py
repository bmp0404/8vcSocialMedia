"""Pairwise tournament ranker: head-to-head comparisons on the fast tier, aggregated with Bradley-Terry.

Each pair is judged twice with positions swapped. The pair's outcome for i is
    s_ij = (P(i better | i shown as A) + P(i better | i shown as B)) / 2
With per-option probabilities (decision models) that's a soft win; with hard choices (claude) it is
1, 0, or 0.5 when the two orders disagree (a tie: position bias cancelled out). Pairing is a full
round robin for n <= ROUND_ROBIN_MAX, otherwise a Swiss bracket: ceil(log2 n) + 1 rounds, each pairing
neighbours by current score without rematches, so cost is ~n/2 pairs per round instead of n^2/2.
Bradley-Terry is fit by MM (Hunter 2004) with one virtual tie per candidate against a phantom
"average draft" of strength 1, which keeps unbeaten candidates finite and anchors the scale:
win_prob = p / (p + 1) = P(beats an average candidate in this batch).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from harness.evaluator.candidates import Candidate, EvalContext
from harness.evaluator.decide import MAX_WORKERS, Question, decide_batch

ROUND_ROBIN_MAX = 6

SYSTEM = """You predict engagement on X (Twitter).

AUDIENCE:
{audience}

Genuine engagement means replies, reposts, quotes, likes and follows from real people in this audience,
not bots or pity likes. Predict what actually happens on X, not which post you like better or which fits
a brand's taste: product news is content too, so judge it by how much people would care, not by whether
it sounds like marketing. Judge only the text: assume both are posted by the same account at the same
time. Most posts get ignored; pick the one more likely to break through."""

# The brand's own audience panel (mission) encodes taste ("hates marketing-speak"); as an engagement
# predictor it made the judge pick deadweight over viral launch posts (47% on 70 real pairs). The
# default pairwise audience is therefore neutral; pass EvalContext(pair_audience="mission") to use the panel.
NEUTRAL_AUDIENCE = ("Developers and engineering leads who follow developer-tool and AI-coding accounts on X: "
                    "they care about tools they use, real workflow pain, launches that change their day, and "
                    "sharp or funny takes; they scroll past filler.")

QUESTION = Question(
    "better",
    "Which post would get more genuine engagement from this audience on X, and why?",
    {"A": "Post A would get more genuine engagement", "B": "Post B would get more genuine engagement"},
)


@dataclass
class PairResult:
    i: str
    j: str
    s: float  # outcome for i in [0, 1]
    consistent: bool  # both orders picked the same post
    soft: bool  # backed by probabilities
    why: str = ""


@dataclass
class Tournament:
    strength: dict[str, float] = field(default_factory=dict)  # Bradley-Terry p (phantom = 1)
    win_prob: dict[str, float] = field(default_factory=dict)  # p / (p + 1)
    results: list[PairResult] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return 2 * len(self.results)

    def rating(self, cid: str) -> float:
        """Elo-style rating (1500 = batch average)."""
        return 1500 + 400 * math.log10(self.strength.get(cid, 1.0))


def _pair_context(a: Candidate, b: Candidate) -> str:
    if a.responding_to and a.responding_to == b.responding_to:
        head = f"BOTH ARE {a.kind.upper()}S TO THIS POST:\n{a.responding_to}\n\n"
        return f"{head}POST A:\n{a.text}\n\nPOST B:\n{b.text}"
    a_t = f"(replying to: {a.responding_to[:300]})\n" if a.responding_to else ""
    b_t = f"(replying to: {b.responding_to[:300]})\n" if b.responding_to else ""
    return f"POST A:\n{a_t}{a.text}\n\nPOST B:\n{b_t}{b.text}"


def compare_pairs(pairs: list[tuple[Candidate, Candidate]], ectx: EvalContext,
                  backend: str | None = None) -> list[PairResult]:
    """Judge each pair in both orders (2 decisions per pair, run concurrently)."""
    if not pairs:
        return []
    contexts = []
    for a, b in pairs:
        contexts += [_pair_context(a, b), _pair_context(b, a)]
    system = SYSTEM.format(audience=ectx.audience if ectx.pair_audience == "mission" else
                           ectx.pair_audience if ectx.pair_audience not in ("", "neutral") else NEUTRAL_AUDIENCE)
    out_rows: list = [None] * len(contexts)

    def one(k: int):
        out_rows[k] = decide_batch([contexts[k]], [QUESTION], backend or ectx.decider, llm=ectx.llm,
                                   system=system, tier=ectx.pair_tier)[0]["better"]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(one, range(len(contexts))))
    results = []
    for n, (a, b) in enumerate(pairs):
        d1, d2 = out_rows[2 * n], out_rows[2 * n + 1]  # d1: a is A; d2: a is B
        s = (d1.p("A") + d2.p("B")) / 2
        consistent = d1.choice is not None and d2.choice is not None and (d1.choice == "A") == (d2.choice == "B")
        why = d1.why if s >= 0.5 else d2.why
        results.append(PairResult(a.id, b.id, round(s, 4), consistent, bool(d1.probs and d2.probs), why))
    return results


def bradley_terry(ids: list[str], results: list[PairResult], prior: float = 1.0, iters: int = 500) -> dict[str, float]:
    games = defaultdict(list)  # id -> [(opponent, score for id)]
    for r in results:
        games[r.i].append((r.j, r.s))
        games[r.j].append((r.i, 1 - r.s))
    p = {i: 1.0 for i in ids}
    for _ in range(iters):
        new = {}
        for i in ids:
            w = prior * 0.5 + sum(s for _, s in games[i])
            d = prior / (p[i] + 1.0) + sum(1.0 / (p[i] + p[j]) for j, _ in games[i])
            new[i] = max(w / d, 1e-6)
        delta = max(abs(math.log(new[i] / p[i])) for i in ids)
        p = new
        if delta < 1e-7:
            break
    return p


def swiss_pairs(ids: list[str], score: dict[str, float], played: set[frozenset], rng: random.Random) -> list[tuple[str, str]]:
    order = sorted(ids, key=lambda i: (-score[i], rng.random()))
    pairs, used = [], set()
    for k, i in enumerate(order):
        if i in used:
            continue
        rest = [j for j in order[k + 1:] if j not in used]
        if not rest:
            break  # odd one out gets a bye
        j = next((j for j in rest if frozenset((i, j)) not in played), rest[0])
        pairs.append((i, j))
        used |= {i, j}
    return pairs


def run_tournament(cands: list[Candidate], ectx: EvalContext, backend: str | None = None,
                   rounds: int | None = None) -> Tournament:
    """Rank one pool of candidates. Use `run_grouped` to keep comparisons within groups."""
    by_id = {c.id: c for c in cands}
    ids = list(by_id)
    t = Tournament()
    if len(ids) < 2:
        t.strength = {i: 1.0 for i in ids}
        t.win_prob = {i: 0.5 for i in ids}
        return t
    rng = random.Random(ectx.seed)
    if len(ids) <= ROUND_ROBIN_MAX:
        schedule = [[(a, b) for n, a in enumerate(ids) for b in ids[n + 1:]]]
    else:
        schedule = None
        rounds = rounds or ectx.rounds or math.ceil(math.log2(len(ids))) + 1
    score = {i: 0.0 for i in ids}
    played: set[frozenset] = set()
    for rnd in range(len(schedule) if schedule else rounds):
        pairs = schedule[rnd] if schedule else swiss_pairs(ids, score, played, rng)
        if not schedule:
            pairs = [(a, b) for a, b in pairs if frozenset((a, b)) not in played] or pairs
        res = compare_pairs([(by_id[a], by_id[b]) for a, b in pairs], ectx, backend)
        for r in res:
            score[r.i] += r.s
            score[r.j] += 1 - r.s
            played.add(frozenset((r.i, r.j)))
        t.results += res
    t.strength = bradley_terry(ids, t.results)
    t.win_prob = {i: round(p / (p + 1.0), 4) for i, p in t.strength.items()}
    return t


def run_grouped(cands: list[Candidate], groups: dict[str, str], ectx: EvalContext,
                backend: str | None = None, rounds: int | None = None) -> Tournament:
    """One tournament per group (e.g. per account in a backtest); ratings are relative to each group."""
    pools = defaultdict(list)
    for c in cands:
        pools[groups.get(c.id, "")].append(c)
    out = Tournament()
    with ThreadPoolExecutor(max_workers=max(1, min(2, len(pools)))) as pool:
        for t in pool.map(lambda cs: run_tournament(cs, ectx, backend, rounds), pools.values()):
            out.strength.update(t.strength)
            out.win_prob.update(t.win_prob)
            out.results += t.results
    return out
