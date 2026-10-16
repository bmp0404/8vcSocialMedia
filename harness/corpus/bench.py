"""Benchmarks whose labels come from the data: which reply in a thread got more likes, and which
of an author's posts got more engagement.

Both compare items that share the thing that predicts engagement most (the thread, or the author),
so a scorer has to rank content and timing rather than recognize a big account. `build()` writes
the pairs and a manifest to `data/bench/`; `load_pairs()` reads them back. DESIGN.md explains each
threshold.
"""

import json
import random
import re
import zlib
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import config
from harness.corpus.load import Corpus, Item

BENCH_DIR = config.ROOT / "data" / "bench"
SEED = 20261016

# Weeks start at 00:00 UTC on the dataset's first day (Tuesday 2026-09-15), so every cutoff below
# is a week boundary: week 0 is Sep 15 to 21, and test weeks 3 and 4 start at the two test cutoffs.
WEEK0 = datetime(2026, 9, 15, tzinfo=timezone.utc)
WEEK = timedelta(days=7)
# Threads whose root post is older than this are dev; the rest are test. Every design choice
# (margins, features, regularization, label fields) was made looking at dev only.
DEV_BEFORE = datetime(2026, 10, 6, tzinfo=timezone.utc)
# Rolling cutoffs: fit on threads rooted before the cutoff, evaluate on the week after it.
DEV_CUTOFFS = (datetime(2026, 9, 29, tzinfo=timezone.utc),)
TEST_CUTOFFS = (datetime(2026, 10, 6, tzinfo=timezone.utc), datetime(2026, 10, 13, tzinfo=timezone.utc))

# A reply "clearly outdid" another when it has at least 5 likes and at least 3 times as many.
# Under the null that two replies are equally likeable and each like lands on either one with
# probability 1/2, 5-0 has one-sided p = 0.031, 6-2 has 0.14 and 12-4 has 0.038; at larger
# counts the ratio rule is far stricter than any p-value cut. DESIGN.md shows results at 2x and 5x.
REPLY_MIN_LIKES = 5
REPLY_RATIO = 3.0
# Posts have more engagement than replies; 10 is the dataset's own Deadweight ceiling.
POST_MIN_ENGAGEMENT = 10
POST_RATIO = 3.0
# A thread or author can supply thousands of pairs (one thread has 1,197). Keeping at most 50,
# drawn with SEED, stops a few threads from deciding the score.
MAX_PAIRS_PER_GROUP = 50
HIGH_COVERAGE = 0.8

_LEADING_MENTIONS = re.compile(r"^(?:\s*@\w+)+\s*")


def reply_body(text: str) -> str:
    """Reply text without the @handles X prepends to every reply."""
    return _LEADING_MENTIONS.sub("", text).strip()


def week_of(t: datetime) -> int:
    return (t - WEEK0) // WEEK


@dataclass(frozen=True)
class Pair:
    pair_id: str
    benchmark: str  # "replies" or "posts"
    group: str  # thread root status ID for replies, author handle for posts
    winner: str  # status IDs
    loser: str
    winner_count: int  # likes for replies, engagement for posts
    loser_count: int
    split: str  # "dev" or "test"
    week: int | None = None  # replies: week of the thread's root post
    root_created_at: str | None = None
    coverage: float | None = None  # replies: share of the root's replies that were sampled

    def to_dict(self) -> dict:
        return asdict(self)


def _cap(pairs: list, rng: random.Random) -> list:
    if len(pairs) <= MAX_PAIRS_PER_GROUP:
        return pairs
    return sorted(rng.sample(pairs, MAX_PAIRS_PER_GROUP), key=lambda p: p.pair_id)


def eligible_replies(corpus: Corpus) -> dict[str, list[Item]]:
    """Root status ID -> sampled direct replies that a pair may use."""
    by_root = defaultdict(list)
    for item in corpus.items:
        if item.thread_root is None or item.parent_id != item.thread_root:
            continue  # not sampled, or a reply to another reply
        root = corpus.get(item.thread_root)
        if root is None:
            raise ValueError(f"reply {item.status_id} points at root {item.thread_root}, which is not in the corpus")
        if item.author.lower() == root.author.lower() or not reply_body(item.text):
            continue  # the root's own author, or a reply with no words of its own
        if not (item.mature and root.mature):
            continue
        by_root[item.thread_root].append(item)
    return by_root


def reply_pairs(corpus: Corpus, min_likes: int = REPLY_MIN_LIKES, ratio: float = REPLY_RATIO,
                cap: bool = True) -> list[Pair]:
    rng = random.Random(SEED)
    out = []
    for root_id, replies in sorted(eligible_replies(corpus).items()):
        root = corpus.get(root_id)
        coverage = next((r.thread_coverage for r in replies if r.thread_coverage is not None), None)
        split = "dev" if root.created_at < DEV_BEFORE else "test"
        pairs = [
            Pair(f"{root_id}:{w.status_id}:{l.status_id}", "replies", root_id, w.status_id, l.status_id,
                 w.metrics["likes"], l.metrics["likes"], split, week_of(root.created_at),
                 root.created_at.isoformat(), coverage)
            for w in replies for l in replies
            if w is not l and w.metrics["likes"] >= min_likes and w.metrics["likes"] >= ratio * l.metrics["likes"]
        ]
        out.extend(_cap(pairs, rng) if cap else pairs)
    return out


def author_split(author: str) -> str:
    """Stable, seeded 50/50 assignment, so dev and test never share an author."""
    return "dev" if zlib.crc32(f"{SEED}:{author.lower()}".encode()) % 2 == 0 else "test"


def eligible_posts(corpus: Corpus) -> dict[str, list[Item]]:
    """Author -> their own original posts that are settled, not thread tails and not flagged as boosted.

    Only the dataset's accounts qualify (`in_dataset` or found on their own timeline); reposted posts by
    other authors are skipped because their author contributes one or two posts at most.
    """
    accounts = {i.author.lower() for i in corpus.items if i.in_dataset}
    by_author = defaultdict(list)
    for item in corpus.items:
        if item.kind != "post" or item.author.lower() not in accounts:
            continue
        if item.is_thread_continuation or item.boost_suspect is True or not item.mature:
            continue
        if not (WEEK0 <= item.created_at < datetime(2026, 10, 17, tzinfo=timezone.utc)):
            continue  # pinned posts from years ago sit on some timelines
        by_author[item.author.lower()].append(item)
    return by_author


def post_pairs(corpus: Corpus, min_engagement: int = POST_MIN_ENGAGEMENT, ratio: float = POST_RATIO,
               cap: bool = True) -> list[Pair]:
    rng = random.Random(SEED)
    out = []
    for author, posts in sorted(eligible_posts(corpus).items()):
        pairs = [
            Pair(f"{author}:{w.status_id}:{l.status_id}", "posts", author, w.status_id, l.status_id,
                 w.engagement, l.engagement, author_split(author))
            for w in posts for l in posts
            if w is not l and w.engagement >= min_engagement and w.engagement >= ratio * l.engagement
        ]
        out.extend(_cap(pairs, rng) if cap else pairs)
    return out


def build(corpus: Corpus, out_dir: Path = BENCH_DIR) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    replies, posts = reply_pairs(corpus), post_pairs(corpus)
    for name, pairs in (("replies", replies), ("posts", posts)):
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for p in pairs:
                f.write(json.dumps(p.to_dict()) + "\n")
    raw_manifest = json.loads((config.ROOT / "data" / "corpus" / "raw" / "MANIFEST.json").read_text(encoding="utf-8"))
    threads = defaultdict(lambda: {"pairs": 0})
    for p in replies:
        t = threads[p.group]
        t.update(split=p.split, week=p.week, root_created_at=p.root_created_at, coverage=p.coverage,
                 author=corpus.get(p.group).author)
        t["pairs"] += 1
    authors = defaultdict(lambda: {"pairs": 0})
    for p in posts:
        authors[p.group]["split"] = p.split
        authors[p.group]["pairs"] += 1
    manifest = {
        "seed": SEED,
        "corpus_snapshot_sha256": {k: v["sha256"] for k, v in raw_manifest["files"].items()},
        "replies": {
            "rule": f"direct replies to the same root, mature, not by the root's author, with words; winner has "
                    f">= {REPLY_MIN_LIKES} likes and >= {REPLY_RATIO}x the loser's likes",
            "split": f"dev: root before {DEV_BEFORE.date()}; test: from {DEV_BEFORE.date()}",
            "week0": WEEK0.isoformat(),
            "dev_cutoffs": [c.isoformat() for c in DEV_CUTOFFS],
            "test_cutoffs": [c.isoformat() for c in TEST_CUTOFFS],
            "max_pairs_per_thread": MAX_PAIRS_PER_GROUP,
            "pairs": len(replies),
            "threads": dict(sorted(threads.items())),
        },
        "posts": {
            "rule": f"an account's own mature originals in the window, no thread tails, boost_suspect excluded; "
                    f"winner has >= {POST_MIN_ENGAGEMENT} engagement and >= {POST_RATIO}x the loser's",
            "split": "seeded 50/50 by author (author_split)",
            "max_pairs_per_author": MAX_PAIRS_PER_GROUP,
            "pairs": len(posts),
            "authors": dict(sorted(authors.items())),
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def load_pairs(name: str, bench_dir: Path = BENCH_DIR) -> list[Pair]:
    path = Path(bench_dir) / f"{name}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing; build it with `python -m harness.corpus bench`")
    with path.open(encoding="utf-8") as f:
        return [Pair(**json.loads(line)) for line in f if line.strip()]


def window(pairs: list[Pair], start: datetime | None = None, end: datetime | None = None) -> list[Pair]:
    """Reply pairs whose root was posted in [start, end)."""
    out = []
    for p in pairs:
        t = datetime.fromisoformat(p.root_created_at)
        if (start is None or t >= start) and (end is None or t < end):
            out.append(p)
    return out
