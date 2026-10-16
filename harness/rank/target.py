"""How open an account's threads are to small repliers: a targeting signal for where a reply can land.

For each thread the account started, take its three most-liked direct replies by other people. Across
the account's threads, report the share of those top-3 replies written by accounts under 1,000
followers, and the median minutes after the root post they arrived. A thread whose top replies come
from big accounts, or only from the first few minutes, leaves little room for a small brand account.

The reply samples came from fxtwitter's likes-ranked first page merged with the newest page, so the
top 3 by likes are seen even in threads with low coverage.
"""

import statistics
from collections import defaultdict

from harness.corpus import Corpus, load
from harness.corpus.bench import reply_body

SMALL_FOLLOWERS = 1000
TOP = 3


def top_replies(corpus: Corpus) -> dict[str, list]:
    """Root status ID -> its TOP most-liked direct replies by others (ties go to the earlier reply)."""
    by_root = defaultdict(list)
    for it in corpus.items:
        if it.thread_root and it.parent_id == it.thread_root and reply_body(it.text):
            root = corpus.get(it.thread_root)
            if root and it.author.lower() != root.author.lower():
                by_root[it.thread_root].append(it)
    return {r: sorted(rs, key=lambda i: (-i.metrics["likes"], i.created_at))[:TOP]
            for r, rs in by_root.items() if len(rs) >= TOP}


def openness_table(corpus: Corpus | None = None) -> dict[str, dict]:
    corpus = corpus or load()
    per_author = defaultdict(lambda: {"small": 0, "total": 0, "minutes": [], "n": 0})
    for root_id, tops in top_replies(corpus).items():
        root = corpus.get(root_id)
        a = per_author[root.author.lower()]
        a["n"] += 1
        for it in tops:
            a["total"] += 1
            a["small"] += (it.author_followers or 0) < SMALL_FOLLOWERS
            a["minutes"].append((it.created_at - root.created_at).total_seconds() / 60)
    return {h: {"share_small_top3": round(a["small"] / a["total"], 3),
                "median_top3_minutes": round(statistics.median(a["minutes"]), 1), "n": a["n"]}
            for h, a in per_author.items()}


def openness(handle: str, corpus: Corpus | None = None) -> dict:
    """{share_small_top3, median_top3_minutes, n} for the account's sampled threads; n = 0 when none."""
    row = openness_table(corpus).get(handle.lstrip("@").lower())
    return row or {"share_small_top3": None, "median_top3_minutes": None, "n": 0}
