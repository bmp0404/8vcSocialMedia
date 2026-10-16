"""Choose the 1,000-item pilot: 500 replies and 500 posts, stratified by engagement.

Items come from dev-split benchmark pairs, both members of each pair, so a label field's
predictive validity can be measured on the pilot alone. Test-split threads and authors are left
out entirely: the label schema is designed on dev and must not see test.
"""

import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from harness.corpus import Corpus
from harness.corpus.bench import author_split, load_pairs
from harness.label.schema import LABELS_DIR

PILOT_PATH = LABELS_DIR / "pilot.json"
SEED = 20261016
PER_KIND = 500
REPLY_TIERS = ((5, 9), (10, 29), (30, 10**9))  # winner likes
POST_TIERS = ((0, 10), (11, 99), (100, 749), (750, 10**9))  # engagement, the dataset's own bands
MAX_PAIRS_PER_GROUP = 4


def _tier(value: int, tiers) -> int:
    return next(i for i, (lo, hi) in enumerate(tiers) if lo <= value <= hi)


def _from_pairs(pairs, tiers, limit: int, rng: random.Random) -> tuple[list[str], list]:
    """Round-robin over winner tiers, at most MAX_PAIRS_PER_GROUP pairs per thread or author."""
    by_tier = defaultdict(list)
    for p in pairs:
        by_tier[_tier(p.winner_count, tiers)].append(p)
    for v in by_tier.values():
        rng.shuffle(v)
    chosen, items, per_group = [], [], Counter()
    seen = set()
    while len(items) < limit and any(by_tier.values()):
        for t in sorted(by_tier):
            while by_tier[t]:
                p = by_tier[t].pop()
                if per_group[p.group] >= MAX_PAIRS_PER_GROUP:
                    continue
                new = [s for s in (p.winner, p.loser) if s not in seen]
                if len(items) + len(new) > limit:
                    continue
                per_group[p.group] += 1
                chosen.append(p)
                for s in new:
                    seen.add(s)
                    items.append(s)
                break
    return items, chosen


def select(corpus: Corpus) -> dict:
    rng = random.Random(SEED)
    reply_pairs = [p for p in load_pairs("replies") if p.split == "dev"]
    post_pairs = [p for p in load_pairs("posts") if p.split == "dev"]
    replies, reply_chosen = _from_pairs(reply_pairs, REPLY_TIERS, PER_KIND, rng)
    posts, post_chosen = _from_pairs(post_pairs, POST_TIERS, 350, rng)
    # Fill the post half with dev-author or non-dataset posts outside the pairs, one engagement band at a time.
    taken = set(posts)
    accounts = {i.author.lower() for i in corpus.items if i.in_dataset}
    pool = defaultdict(list)
    for item in corpus.items:
        if item.kind != "post" or item.status_id in taken or not item.mature:
            continue
        if item.author.lower() in accounts and author_split(item.author) == "test":
            continue
        pool[_tier(item.engagement, POST_TIERS)].append(item.status_id)
    for v in pool.values():
        rng.shuffle(v)
    while len(posts) < PER_KIND and any(pool.values()):
        for t in sorted(pool):
            if pool[t] and len(posts) < PER_KIND:
                posts.append(pool[t].pop())
    return {
        "seed": SEED,
        "items": [f"x:{s}" for s in replies + posts],
        "reply_pairs": [p.pair_id for p in reply_chosen],
        "post_pairs": [p.pair_id for p in post_chosen],
        "counts": {
            "replies": len(replies), "posts": len(posts),
            "reply_likes_tiers": dict(Counter(_tier(corpus.get(s).metrics["likes"], ((0, 0), (1, 4), (5, 9), (10, 29), (30, 10**9)))
                                              for s in replies)),
            "post_engagement_tiers": dict(Counter(_tier(corpus.get(s).engagement, POST_TIERS) for s in posts)),
        },
    }


def write(selection: dict, path: Path = PILOT_PATH) -> None:
    path.write_text(json.dumps(selection, indent=1) + "\n", encoding="utf-8")


def load(path: Path = PILOT_PATH) -> list[str]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["items"]
