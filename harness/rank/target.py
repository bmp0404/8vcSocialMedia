"""How open an account's threads are to small repliers: a targeting signal for where a reply can land.

For each thread an account started, take its three most-liked direct replies by other people. Across
the account's threads, `openness` reports the share of those top-3 replies written by accounts under
1,000 followers, and the median minutes after the root post they arrived.

Source: `data/context/reply_samples.jsonl` (fxtwitter's likes-ranked first page merged with the newest
page, so the top 3 by likes are seen even where coverage is low) and the root posts in
`data/context/x_metrics.jsonl`. Both hold individuals' replies, handles and bios, so they stay local and
gitignored; copy them from the context-dynamics worktree. `build` commits only per-account aggregates,
and only for the brand accounts listed in `data/context/accounts.csv`, to `data/context/openness.json`.
`openness(handle)` reads that file, so the watcher and the lanes need no local data.

`validate` asks whether an account's openness on its earlier threads predicts its later threads, and
writes `eval/experiments/reports/openness-validation.json`. Results are in `eval/experiments/closed.md`.

    uv run python -m harness.rank.target build
    uv run python -m harness.rank.target validate
"""

import argparse
import csv
import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from harness import config
from harness.evals.metrics import cluster_bootstrap, pairwise_accuracy, spearman

SMALL_FOLLOWERS = 1000
TOP = 3
CONTEXT = config.ROOT / "data" / "context"
REPLY_SAMPLES = CONTEXT / "reply_samples.jsonl"
ROOTS = CONTEXT / "x_metrics.jsonl"
ACCOUNTS = CONTEXT / "accounts.csv"
TABLE = CONTEXT / "openness.json"
REPORT = config.ROOT / "eval" / "experiments" / "reports" / "openness-validation.json"
LEADING_MENTIONS = re.compile(r"^(?:\s*@\w+)+\s*")  # X prepends the parent's handles to every reply
# Weekly test windows for the rolling check. Mondays; the samples run 2026-09-15 to 2026-10-15.
WEEKLY_CUTOFFS = tuple(datetime(2026, m, d, tzinfo=timezone.utc) for m, d in ((9, 22), (9, 29), (10, 6), (10, 13), (10, 20)))
# B2's reply benchmark split dev and test at this date (sam/corpus, harness/corpus/DESIGN.md).
B2_SPLIT = datetime(2026, 10, 6, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Reply:
    id: str
    author: str
    followers: int
    likes: int
    created_at: datetime


@dataclass(frozen=True)
class Thread:
    root_id: str
    author: str  # lowercased handle of the account that started the thread
    created_at: datetime
    author_followers: int
    replies: tuple[Reply, ...]  # direct replies by other people, with words after the leading @handles


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. It holds individuals' replies, so it is never committed; copy it from the "
            "context-dynamics worktree's data/context/ or rebuild it with `python -m harness.context collect replies`.")
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_threads(samples: Path = REPLY_SAMPLES, roots: Path = ROOTS) -> list[Thread]:
    """Every sampled thread, with its direct replies by people other than the root's author.

    A reply counts when its parent is the root, its author isn't the root's author, and it has words
    after the @handles X prepends. A reply that appears twice (both sort orders) counts once."""
    root_rows = {r["id"]: r for r in _read_jsonl(roots) if "id" in r and "error" not in r}
    by_root: dict[str, dict[str, Reply]] = defaultdict(dict)
    for r in _read_jsonl(samples):
        if "id" not in r:  # error and empty-sample markers
            continue
        root_id = str(r["reply_to_post"])
        root = root_rows.get(root_id)
        if root is None:
            raise ValueError(f"reply {r['id']} answers {root_id}, which isn't in {roots}")
        if str(r.get("replying_to_status")) != root_id or r["author_handle"].lower() == root["author_handle"].lower():
            continue
        if not LEADING_MENTIONS.sub("", r.get("text") or "").strip():
            continue
        by_root[root_id][r["id"]] = Reply(r["id"], r["author_handle"], int(r.get("author_followers") or 0),
                                          int(r.get("likes") or 0), _time(r["created_at"]))
    return [Thread(rid, root_rows[rid]["author_handle"].lower(), _time(root_rows[rid]["created_at"]),
                   int(root_rows[rid].get("author_followers") or 0), tuple(reps.values()))
            for rid, reps in sorted(by_root.items())]


def top_replies(thread: Thread, k: int = TOP) -> list[Reply] | None:
    """The `k` most-liked replies, ties going to the earlier reply; None when the thread has fewer than `k`."""
    if len(thread.replies) < k:
        return None
    return sorted(thread.replies, key=lambda r: (-r.likes, r.created_at, r.id))[:k]


def small_in_top(thread: Thread) -> int | None:
    """How many of the top 3 replies came from accounts under SMALL_FOLLOWERS; None below 3 replies."""
    top = top_replies(thread)
    return None if top is None else sum(r.followers < SMALL_FOLLOWERS for r in top)


def top_minutes(thread: Thread) -> list[float] | None:
    top = top_replies(thread)
    return None if top is None else [(r.created_at - thread.created_at).total_seconds() / 60 for r in top]


def openness_table(threads: list[Thread]) -> dict[str, dict]:
    """{handle: {share_small_top3, median_top3_minutes, n}} over threads with at least 3 replies.
    `n` counts those threads; the share and the median pool their top-3 replies."""
    small: dict[str, int] = defaultdict(int)
    minutes: dict[str, list[float]] = defaultdict(list)
    n: dict[str, int] = defaultdict(int)
    for t in threads:
        k = small_in_top(t)
        if k is None:
            continue
        n[t.author] += 1
        small[t.author] += k
        minutes[t.author] += top_minutes(t)
    return {h: {"share_small_top3": round(small[h] / (TOP * n[h]), 3),
                "median_top3_minutes": round(statistics.median(minutes[h]), 1), "n": n[h]}
            for h in sorted(n)}


EMPTY = {"share_small_top3": None, "median_top3_minutes": None, "n": 0}


@lru_cache(maxsize=4)
def load_table(path: Path = TABLE) -> dict[str, dict]:
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; rebuild it with `python -m harness.rank.target build`")
    return json.loads(path.read_text(encoding="utf-8"))["accounts"]


def openness(handle: str, table: dict[str, dict] | None = None) -> dict:
    """{share_small_top3, median_top3_minutes, n} for the account's sampled threads; n = 0 and None values
    when it has none. Reads the committed table unless `table` is given."""
    row = (load_table() if table is None else table).get(handle.strip().lstrip("@").lower())
    return dict(row) if row else dict(EMPTY)


# ---------------------------------------------------------------- build


def public_accounts(path: Path = ACCOUNTS) -> set[str]:
    """Lowercased handles of the brand accounts in the dev-tool dataset, the only ones the table may name."""
    with path.open(encoding="utf-8", newline="") as f:
        return {row["account"].lstrip("@").lower() for row in csv.DictReader(f)}


def build(threads: list[Thread], allowed: set[str], path: Path = TABLE) -> dict:
    """Write the aggregates for `allowed` accounts to `path` and return what was written."""
    table = {h: row for h, row in openness_table(threads).items() if h in allowed}
    dates = sorted(t.created_at for t in threads)
    out = {"source": "data/context/reply_samples.jsonl (local) via harness.rank.target build",
           "small_followers": SMALL_FOLLOWERS, "top": TOP,
           "roots_from": dates[0].date().isoformat() if dates else None,
           "roots_to": dates[-1].date().isoformat() if dates else None, "accounts": table}
    path.write_text(json.dumps(out, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    load_table.cache_clear()
    return out


# ---------------------------------------------------------------- validation


@dataclass(frozen=True)
class Prediction:
    thread: Thread
    prior: dict  # the author's openness row over threads before the test window opened
    window: str  # ISO date the test window opened


def rolling_predictions(threads: list[Thread], cutoffs: tuple[datetime, ...], min_history: int = 2) -> list[Prediction]:
    """Each thread in [cutoffs[i], cutoffs[i+1]) paired with its author's openness over the threads that
    started before cutoffs[i]. Threads whose author has fewer than `min_history` earlier scored threads, or
    that have fewer than 3 replies themselves, are left out."""
    out = []
    for start, end in zip(cutoffs, cutoffs[1:]):
        history = openness_table([t for t in threads if t.created_at < start])
        for t in threads:
            if start <= t.created_at < end and small_in_top(t) is not None:
                prior = history.get(t.author)
                if prior and prior["n"] >= min_history:
                    out.append(Prediction(t, prior, start.date().isoformat()))
    return out


def _auc_ci(scores: list[float], labels: list[bool], clusters: list[str], n_boot: int, seed: int) -> dict:
    auc, pairs = pairwise_accuracy(scores, [float(y) for y in labels])
    stat = lambda idx, _ids: pairwise_accuracy([scores[i] for i in idx], [float(labels[i]) for i in idx])[0]  # noqa: E731
    ci = cluster_bootstrap(stat, clusters, n_boot=n_boot, seed=seed)
    return {"auc": None if auc is None else round(auc, 3), "ci95": None if ci is None else [round(c, 3) for c in ci],
            "positives": sum(labels), "negatives": len(labels) - sum(labels), "pairs": pairs}


def _rho_ci(xs: list[float], ys: list[float], clusters: list[str], n_boot: int, seed: int) -> dict:
    rho = spearman(xs, ys)
    stat = lambda idx, _ids: spearman([xs[i] for i in idx], [ys[i] for i in idx])  # noqa: E731
    ci = cluster_bootstrap(stat, clusters, n_boot=n_boot, seed=seed)
    return {"rho": None if rho is None else round(rho, 3), "ci95": None if ci is None else [round(c, 3) for c in ci]}


def score_predictions(preds: list[Prediction], n_boot: int = 2000, seed: int = 0) -> dict:
    """Does prior openness predict the test thread? Intervals resample authors, since one author's threads
    share an audience. The outcomes, per test thread:
    - any_small: at least one top-3 reply came from an account under 1,000 followers (the brief's question);
    - share_small: the share of the top 3 that did;
    - top3_minutes: the median minutes-after of the top 3, against the prior median_top3_minutes.
    Baseline: the author's follower count, which needs no reply data at all."""
    if not preds:
        return {"n_threads": 0}
    authors = [p.thread.author for p in preds]
    prior_share = [p.prior["share_small_top3"] for p in preds]
    small = [small_in_top(p.thread) for p in preds]
    any_small = [k > 0 for k in small]
    share = [k / TOP for k in small]
    smaller_author = [-math.log10(max(p.thread.author_followers, 1)) for p in preds]
    return {
        "n_threads": len(preds), "n_authors": len(set(authors)),
        "any_small_base_rate": round(sum(any_small) / len(preds), 3),
        "any_small_by_prior_share": _auc_ci(prior_share, any_small, authors, n_boot, seed),
        "any_small_by_smaller_author": _auc_ci(smaller_author, any_small, authors, n_boot, seed),
        "share_small_by_prior_share": _rho_ci(prior_share, share, authors, n_boot, seed),
        "share_small_by_smaller_author": _rho_ci(smaller_author, share, authors, n_boot, seed),
        "top3_minutes_by_prior_median": _rho_ci([p.prior["median_top3_minutes"] for p in preds],
                                                [statistics.median(top_minutes(p.thread)) for p in preds],
                                                authors, n_boot, seed),
    }


def validate(threads: list[Thread], min_history: int = 2, n_boot: int = 2000, seed: int = 0) -> dict:
    rolling = rolling_predictions(threads, WEEKLY_CUTOFFS, min_history)
    single = rolling_predictions(threads, (B2_SPLIT, B2_SPLIT + timedelta(days=365)), min_history)
    return {
        "question": "Does an author's openness on earlier threads predict whether a small account's reply "
                    "reaches the top 3 of a later thread?",
        "min_history_threads": min_history, "bootstrap": {"draws": n_boot, "seed": seed, "cluster": "author"},
        "threads_scored": sum(small_in_top(t) is not None for t in threads),
        "rolling_weekly": {"cutoffs": [c.date().isoformat() for c in WEEKLY_CUTOFFS], **score_predictions(rolling, n_boot, seed)},
        "single_split_2026_10_06": score_predictions(single, n_boot, seed),
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m harness.rank.target")
    p.add_argument("command", choices=["build", "validate"])
    p.add_argument("--min-history", type=int, default=2)
    args = p.parse_args()
    threads = load_threads()
    if args.command == "build":
        out = build(threads, public_accounts())
        print(f"wrote {TABLE} with {len(out['accounts'])} accounts ({out['roots_from']} to {out['roots_to']})")
        return
    report = validate(threads, args.min_history)
    REPORT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
