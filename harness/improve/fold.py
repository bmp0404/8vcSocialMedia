"""Fold the watcher's mature threads into the rolling reply benchmark, without leaking into fitted splits.

The watcher (harness/watcher) reads every watched post's conversation at +1h, +24h and +48h and appends
kept threads to data/threads/ in the corpus loader's format. This module turns the threads whose +48h
read has landed into reply pairs under the frozen benchmark's rule (harness/corpus/bench.py: winner
has 5 or more likes and 3 times the loser's, both direct replies to the root), and files each under
the week its root was posted.

**Maturity.** The frozen benchmark counts a reply only when it was read 48 hours or more after the
reply itself was posted. Under a +48h read of the root nearly every reply would fail that, because
replies arrive minutes to hours after the root. Here a thread counts once its root was read 48 hours
or more after posting, and a reply counts if it came within the root's first 24 hours, so every
counted reply had at least 24 hours of likes at that read. The 24 hours is policy; no measurement
picked it. Rolling pairs are reported apart from the frozen ones for that reason.

**No leakage.** A folded thread must be rooted after every root in the frozen benchmark and no earlier
than every fitted ranker's training cutoff (`horizon`). A thread rooted earlier is refused with the
reason, never filed into a week a ranker trained on. A thread already in the frozen benchmark is refused.

The same reads also give post pairs within an account (`post_pairs`) for the post rankers, and lineup
threads for the action instrument (`lineup_threads`).
"""

import gzip
import json
import random
import shutil
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from harness import config
from harness.corpus import Corpus
from harness.corpus.bench import (BENCH_DIR, MAX_PAIRS_PER_GROUP, POST_MIN_ENGAGEMENT, POST_RATIO, SEED, Pair,
                                  reply_body, reply_pairs, week_of)
from harness.corpus.load import MATURE_AFTER, Item
from harness.rank.post import clean_text

THREADS_DIR = config.ROOT / "data" / "threads"
ROLLING_DIR = BENCH_DIR / "rolling"
REPLY_WINDOW = timedelta(hours=24)  # policy: see the module docstring
WATCH_FILES = ("author_timelines.jsonl.gz", "reply_samples.jsonl.gz", "x_metrics.jsonl.gz", "boost_flags.csv.gz",
               "timeline_posts.csv.gz")


def thread_settled(reply: Item, root: Item) -> bool:
    """The rolling rule: root and reply both read 48 hours or more after the root went out, and the reply
    posted within the root's first 24 hours."""
    return (root.mature and reply.fetched_at - root.created_at >= MATURE_AFTER
            and reply.created_at - root.created_at <= REPLY_WINDOW)


def snapshot(threads_dir: Path, dest: Path) -> Path:
    """Copy the watcher's files before reading them, so an append in progress can't tear a read."""
    dest.mkdir(parents=True, exist_ok=True)
    missing = [n for n in WATCH_FILES if not (Path(threads_dir) / n).is_file()]
    if missing:
        raise FileNotFoundError(f"{threads_dir} lacks {missing}; is the watcher writing there?")
    for name in WATCH_FILES:
        shutil.copyfile(Path(threads_dir) / name, dest / name)
    return dest


def digest(threads_dir: Path) -> str:
    """Size and modification time of each file: changes whenever the watcher appends a thread."""
    parts = []
    for name in WATCH_FILES:
        st = (Path(threads_dir) / name).stat()
        parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(parts)


def horizon(frozen: list[Pair], fitted_before: list[datetime]) -> tuple[datetime, datetime | None]:
    """(latest root in the frozen benchmark, latest training cutoff of any fitted ranker)."""
    if not frozen:
        raise ValueError("the frozen reply benchmark is empty; build it with `python -m harness.corpus bench`")
    latest = max(datetime.fromisoformat(p.root_created_at) for p in frozen)
    return latest, max(fitted_before) if fitted_before else None


@dataclass
class Fold:
    pairs: list[Pair]
    refused: dict[str, str]  # root status ID -> why it can't join
    latest_frozen_root: datetime
    fitted_before: datetime | None
    threads_seen: int  # threads with at least one settled reply
    weeks: dict[int, dict] = field(default_factory=dict)

    def manifest(self) -> dict:
        reasons = defaultdict(int)
        for why in self.refused.values():
            reasons[why.split(":")[0]] += 1
        return {"rule": "frozen benchmark pair rule; root read 48h+ after posting, replies within its first 24h",
                "latest_frozen_root": self.latest_frozen_root.isoformat(),
                "fitted_before": self.fitted_before.isoformat() if self.fitted_before else None,
                "threads_seen": self.threads_seen, "pairs": len(self.pairs),
                "threads": len({p.group for p in self.pairs}), "refused": dict(reasons),
                "weeks": {str(w): v for w, v in sorted(self.weeks.items())}}


def fold(corpus: Corpus, frozen: list[Pair], fitted_before: list[datetime]) -> Fold:
    """Rolling pairs from a corpus of watcher threads. Pure: reads nothing but its arguments."""
    latest, fit_cut = horizon(frozen, fitted_before)
    frozen_roots = {p.group for p in frozen}
    candidates = reply_pairs(corpus, settled=thread_settled)
    by_root = defaultdict(list)
    for p in candidates:
        by_root[p.group].append(p)
    out, refused = [], {}
    for root_id, pairs in sorted(by_root.items()):
        root_at = datetime.fromisoformat(pairs[0].root_created_at)
        if root_id in frozen_roots:
            refused[root_id] = "in the frozen benchmark"
        elif root_at <= latest:
            refused[root_id] = f"rooted before the frozen benchmark's last root: {root_at.isoformat()}"
        elif fit_cut is not None and root_at < fit_cut:
            refused[root_id] = f"rooted before a fitted ranker's training cutoff: {root_at.isoformat()}"
        else:
            out.extend(replace(p, split="rolling") for p in pairs)
    weeks: dict[int, dict] = defaultdict(lambda: {"threads": 0, "pairs": 0})
    for root_id in sorted({p.group for p in out}):
        weeks[next(p.week for p in out if p.group == root_id)]["threads"] += 1
    for p in out:
        weeks[p.week]["pairs"] += 1
    return Fold(out, refused, latest, fit_cut, len(by_root), dict(weeks))


def write(result: Fold, out_dir: Path = ROLLING_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "replies.jsonl.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        for p in result.pairs:
            f.write(json.dumps(p.to_dict()) + "\n")
    tmp.replace(out_dir / "replies.jsonl")
    (out_dir / "manifest.json").write_text(json.dumps(result.manifest(), indent=1) + "\n", encoding="utf-8")
    return out_dir / "replies.jsonl"


def load_rolling(out_dir: Path = ROLLING_DIR) -> list[Pair]:
    path = out_dir / "replies.jsonl"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return [Pair(**json.loads(line)) for line in f if line.strip()]


# ---------------------------------------------------------------- other views of the same reads


def profiles(threads_dir: Path) -> dict[str, str]:
    """Root status ID -> the watch profile that read it."""
    out = {}
    with gzip.open(Path(threads_dir) / "author_timelines.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                out[str(row["id"])] = row.get("watch_profile") or ""
    return out


def post_pairs(corpus: Corpus, min_engagement: int = POST_MIN_ENGAGEMENT, ratio: float = POST_RATIO) -> list[Pair]:
    """Within-account pairs among the watched accounts' own posts read 48 hours or more after posting,
    under the frozen post benchmark's margin. Every pair's split is "rolling"."""
    by_author = defaultdict(list)
    for it in corpus.items:
        if it.kind == "post" and it.mature and not it.is_thread_continuation and len(clean_text(it.text)) >= 10:
            by_author[it.author.lower()].append(it)
    rng = random.Random(SEED)
    out = []
    for author, posts in sorted(by_author.items()):
        pairs = [Pair(f"{author}:{w.status_id}:{l.status_id}", "posts", author, w.status_id, l.status_id,
                      w.engagement, l.engagement, "rolling", week_of(w.created_at))
                 for w in posts for l in posts
                 if w is not l and w.engagement >= min_engagement and w.engagement >= ratio * l.engagement]
        if len(pairs) > MAX_PAIRS_PER_GROUP:
            pairs = sorted(rng.sample(pairs, MAX_PAIRS_PER_GROUP), key=lambda p: p.pair_id)
        out.extend(pairs)
    return out


def lineup_threads(corpus: Corpus, root_profiles: dict[str, str], profile: str) -> list[dict]:
    """Watched posts of one profile in the action fixture's shape, with each direct reply's text and likes."""
    replies = defaultdict(list)
    for it in corpus.items:
        if it.thread_root and it.parent_id == it.thread_root and reply_body(it.text):
            replies[it.thread_root].append({"text": it.text, "likes": it.metrics["likes"]})
    out = []
    for it in corpus.items:
        if it.kind == "post" and root_profiles.get(it.status_id) == profile and replies.get(it.status_id):
            out.append({"id": it.status_id, "author": it.author, "url": it.url, "text": it.text,
                        "direct_replies": replies[it.status_id]})
    return out
