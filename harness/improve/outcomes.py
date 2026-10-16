"""What the instruments are scored against, and the two statistics they are scored with.

Two sources, kept apart in the instrument ledger:
- `sgu`: `_sgu_`'s real replies, from the outcomes ledger (data/outcomes.db, written by `harness outcomes
  sync`), each with its rank percentile in its thread (harness/outcomes/rank.py). Scored by Spearman
  correlation between the instrument's score for the reply and that percentile.
- `threads`: reply pairs and within-account post pairs from threads rooted after the instrument's cutoff,
  from the frozen corpus and from the watcher's folded threads. Scored by pairwise accuracy.

Both statistics come with a 95% percentile bootstrap interval. Pairs are resampled by thread (or account),
because pairs that share one are not independent; `_sgu_` replies are resampled one by one, since each
sits in its own thread.
"""

import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from harness.corpus import Corpus
from harness.corpus.bench import Pair
from harness.corpus.stats import pair_credit

SCORED_OUTCOMES = ("success", "failure", "unranked")  # insufficient_thread (under 5 replies) says nothing


@dataclass(frozen=True)
class SguReply:
    action_id: str
    status_id: str
    parent_status: str
    text: str
    parent_text: str  # from the hot file when there is one, else ""
    minutes_after: float | None
    followers: int | None
    percentile: float  # 1.0 is the top reply; off the first page, the best it could be
    checkpoint: int
    profile: str | None
    arm: str | None

    @property
    def posted_at(self) -> datetime:
        from harness.corpus.load import created_at

        return created_at(self.status_id)


def sgu_replies(ledger_path: Path, hot_dir: Path) -> list[SguReply]:
    """Every matched `_sgu_` reply with a usable rank, at its latest checkpoint. Read-only."""
    ledger_path = Path(ledger_path)
    if not ledger_path.is_file():
        return []
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT rm.action_id, rm.status_id, rm.parent_status, rm.arm, rm.minutes_after_parent,
                      rm.followers_at_match, p.text, h.profile
               FROM reply_matches rm JOIN posts p ON p.status_id = rm.status_id
               LEFT JOIN hot_targets h ON h.action_id = rm.action_id ORDER BY rm.matched_at""").fetchall()
        out = []
        for r in rows:
            marks = ",".join("?" * len(SCORED_OUTCOMES))
            rank = conn.execute(f"SELECT * FROM ranks WHERE action_id = ? AND outcome IN ({marks}) "
                                "ORDER BY checkpoint DESC LIMIT 1", (r["action_id"], *SCORED_OUTCOMES)).fetchone()
            if rank is None:
                continue
            pct = rank["percentile"] if rank["percentile"] is not None else rank["percentile_max"]
            if pct is None:
                continue
            hot = Path(hot_dir) / f"{r['parent_status']}.json"
            parent = json.loads(hot.read_text(encoding="utf-8"))["target"]["text"] if hot.is_file() else ""
            out.append(SguReply(r["action_id"], r["status_id"], r["parent_status"], r["text"] or "", parent,
                                r["minutes_after_parent"], r["followers_at_match"], float(pct), rank["checkpoint"],
                                r["profile"], r["arm"]))
        return out
    finally:
        conn.close()


def root_time(p: Pair) -> datetime:
    return datetime.fromisoformat(p.root_created_at)


@dataclass
class Evidence:
    """Everything one cycle scores the instruments against."""

    frozen: Corpus  # data/corpus/raw: the corpus the benchmarks and fitted rankers came from
    frozen_pairs: list[Pair]  # data/bench/replies.jsonl
    watch: Corpus | None = None  # a snapshot of the watcher's data/threads
    rolling: list[Pair] = field(default_factory=list)  # reply pairs folded from the watcher (fold.py)
    post_pairs: list[Pair] = field(default_factory=list)  # within-account pairs among watched posts
    profiles: dict[str, str] = field(default_factory=dict)  # watched root -> profile
    sgu: list[SguReply] = field(default_factory=list)

    def item(self, status_id: str):
        it = self.watch.get(status_id) if self.watch is not None else None
        return it or self.frozen.get(status_id)

    def profile(self, root_id: str) -> str:
        """The frozen corpus is the dev-tool dataset; watched roots carry their own profile."""
        return self.profiles.get(root_id, "devtools")

    def sgu_after(self, cutoff: datetime) -> list[SguReply]:
        """`_sgu_` replies posted at or after `cutoff`: a new instrument version starts its evidence over."""
        return [r for r in self.sgu if r.posted_at >= cutoff]

    def settled_replies(self, root_id: str) -> list:
        """The root's direct replies whose likes count under the benchmark's rule (fold.thread_settled for
        watched roots, both read 48 hours after posting for the frozen corpus), oldest first."""
        if not hasattr(self, "_settled"):
            from harness.corpus.bench import eligible_replies
            from harness.improve.fold import thread_settled

            self._settled = dict(eligible_replies(self.frozen))
            if self.watch is not None:
                self._settled.update(eligible_replies(self.watch, thread_settled))
        return sorted(self._settled.get(root_id, []), key=lambda it: it.created_at)

    def reply_pairs(self, cutoff: datetime, profiles: tuple[str, ...] | None = None) -> list[tuple[Pair, str]]:
        """(pair, origin) for every reply pair whose root went out at or after `cutoff`."""
        out = [(p, "corpus") for p in self.frozen_pairs if root_time(p) >= cutoff]
        out += [(p, "watch") for p in self.rolling if root_time(p) >= cutoff]
        return [(p, o) for p, o in out if profiles is None or self.profile(p.group) in profiles]

    def posts_after(self, cutoff: datetime, profiles: tuple[str, ...] | None = None) -> list[Pair]:
        """Within-account post pairs whose two posts both went out at or after `cutoff`."""
        out = []
        for p in self.post_pairs:
            w, l = self.item(p.winner), self.item(p.loser)
            if min(w.created_at, l.created_at) < cutoff:
                continue
            if profiles is None or self.profile(p.winner) in profiles:
                out.append(p)
        return out


# ---------------------------------------------------------------- statistics


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation of average ranks; None when either side is constant or n < 3."""
    if len(xs) != len(ys):
        raise ValueError("spearman: the two lists differ in length")
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx, syy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def spearman_interval(xs: list[float], ys: list[float], b: int = 2000, seed: int = 0) -> tuple[float, float] | None:
    """95% percentile bootstrap interval, resampling outcomes; None when fewer than half the draws are defined."""
    rng = random.Random(seed)
    n = len(xs)
    draws = []
    for _ in range(b):
        idx = [rng.randrange(n) for _ in range(n)]
        r = spearman([xs[i] for i in idx], [ys[i] for i in idx])
        if r is not None:
            draws.append(r)
    if len(draws) < b / 2:
        return None
    draws.sort()
    lo, hi = draws[int(0.025 * (len(draws) - 1))], draws[int(0.975 * (len(draws) - 1))]
    return lo, hi


def pair_credits(pairs: list[Pair], score: dict[str, float | None]) -> list[float]:
    """Credit per pair. An item the instrument abstained on (None) scores as the worst rank."""
    worst = float("-inf")
    return [pair_credit(worst if score.get(p.winner) is None else score[p.winner],
                        worst if score.get(p.loser) is None else score[p.loser]) for p in pairs]
