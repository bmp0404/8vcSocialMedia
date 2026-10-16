"""Folding the watcher's threads into the rolling benchmark: mature threads join under their root's week,
and nothing rooted inside a frozen or fitted window ever does."""

import gzip
import json
from datetime import datetime, timedelta, timezone

from harness.corpus import load
from harness.corpus.bench import Pair, week_of
from harness.corpus.load import TWITTER_EPOCH_MS
from harness.improve import fold

FROZEN_LAST = datetime(2026, 10, 16, 6, tzinfo=timezone.utc)
FROZEN = [Pair("r0:a:b", "replies", "r0", "a", "b", 9, 1, "test", 4, FROZEN_LAST.isoformat(), 1.0)]


def sid(t: datetime, n: int = 0) -> str:
    return str(((int(t.timestamp() * 1000) - TWITTER_EPOCH_MS) << 22) + n)


def row(status_id: str, author: str, read_at: datetime, likes=0, **extra) -> dict:
    return {"id": status_id, "author_handle": author, "text": extra.pop("text", f"words from {author} here"),
            "likes": likes, "reposts": 0, "replies": 0, "quotes": 0, "bookmarks": 0, "views": 0,
            "fetched_at": read_at.isoformat(), "author_followers": 10, "author_verified": False, **extra}


class Threads:
    """A data/threads directory in the watcher's format, built one thread at a time."""

    def __init__(self, path):
        self.path, self.roots, self.replies = path, [], []

    def thread(self, root_at: datetime, read_after: timedelta, likes: list[int], author="brand", profile="devtools",
               reply_delay=timedelta(minutes=5), root_likes=0):
        root = sid(root_at)
        read = root_at + read_after
        self.roots.append(row(root, author, read, likes=root_likes, watch_profile=profile, replies=len(likes)))
        for i, n in enumerate(likes):
            rid = sid(root_at + reply_delay * (i + 1), i)
            self.replies.append(row(rid, f"fan{i}", read, likes=n, replying_to=author, replying_to_status=root,
                                    reply_to_post=root, coverage=1.0, watch_profile=profile))
        return root

    def write(self):
        self.path.mkdir(exist_ok=True)
        files = {"author_timelines.jsonl.gz": self.roots, "reply_samples.jsonl.gz": self.replies,
                 "x_metrics.jsonl.gz": []}
        for name, rows in files.items():
            with gzip.open(self.path / name, "wt", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
        for name, header in (("boost_flags.csv.gz", "post_id,is_thread_continuation,boost_suspect\n"),
                             ("timeline_posts.csv.gz", "post_id,is_thread_continuation\n")):
            with gzip.open(self.path / name, "wt", encoding="utf-8") as f:
                f.write(header)
        return self.path


def test_a_mature_thread_after_the_horizon_joins_under_its_week(tmp_path):
    t = Threads(tmp_path / "threads")
    root_at = FROZEN_LAST + timedelta(days=1)
    root = t.thread(root_at, timedelta(hours=49), [12, 1, 0])
    result = fold.fold(load(t.write()), FROZEN, [datetime(2026, 10, 6, tzinfo=timezone.utc)])
    assert {p.group for p in result.pairs} == {root}
    assert all(p.split == "rolling" and p.week == week_of(root_at) for p in result.pairs)
    assert len(result.pairs) == 2  # 12 beats 1 and 0; 1 vs 0 misses the 5-like floor
    assert result.refused == {} and result.weeks == {week_of(root_at): {"threads": 1, "pairs": 2}}


def test_threads_inside_a_frozen_or_fitted_window_are_refused_with_the_reason(tmp_path):
    t = Threads(tmp_path / "threads")
    before_frozen = t.thread(FROZEN_LAST - timedelta(hours=1), timedelta(hours=50), [12, 0])
    fit_cut = FROZEN_LAST + timedelta(days=2)
    before_fit = t.thread(FROZEN_LAST + timedelta(days=1), timedelta(hours=50), [12, 0], author="b2")
    after_both = t.thread(fit_cut + timedelta(hours=1), timedelta(hours=50), [12, 0], author="b3")
    result = fold.fold(load(t.write()), FROZEN, [fit_cut])
    assert {p.group for p in result.pairs} == {after_both}
    assert result.refused[before_frozen].startswith("rooted before the frozen benchmark's last root")
    assert result.refused[before_fit].startswith("rooted before a fitted ranker's training cutoff")
    assert result.manifest()["refused"] == {"rooted before the frozen benchmark's last root": 1,
                                            "rooted before a fitted ranker's training cutoff": 1}


def test_a_thread_already_in_the_frozen_benchmark_is_refused(tmp_path):
    t = Threads(tmp_path / "threads")
    root = t.thread(FROZEN_LAST + timedelta(days=1), timedelta(hours=50), [12, 0])
    frozen = FROZEN + [Pair(f"{root}:x:y", "replies", root, "x", "y", 9, 1, "test", 4,
                            (FROZEN_LAST - timedelta(days=1)).isoformat(), 1.0)]
    result = fold.fold(load(t.write()), frozen, [])
    assert result.pairs == [] and result.refused == {root: "in the frozen benchmark"}


def test_only_the_48h_read_and_replies_from_the_first_day_count(tmp_path):
    t = Threads(tmp_path / "threads")
    young = t.thread(FROZEN_LAST + timedelta(days=1), timedelta(hours=30), [12, 0])
    late = t.thread(FROZEN_LAST + timedelta(days=1, hours=1), timedelta(hours=60), [12, 0], author="b2",
                    reply_delay=timedelta(hours=13))  # the second reply lands at +26h
    result = fold.fold(load(t.write()), FROZEN, [])
    assert young not in {p.group for p in result.pairs} and young not in result.refused
    assert late not in {p.group for p in result.pairs}  # only one reply inside 24h, so no pair


def test_write_and_load_round_trip(tmp_path):
    t = Threads(tmp_path / "threads")
    t.thread(FROZEN_LAST + timedelta(days=1), timedelta(hours=49), [12, 1, 0])
    result = fold.fold(load(t.write()), FROZEN, [])
    fold.write(result, tmp_path / "rolling")
    assert fold.load_rolling(tmp_path / "rolling") == result.pairs
    manifest = json.loads((tmp_path / "rolling" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pairs"] == 2 and manifest["threads"] == 1
    assert fold.load_rolling(tmp_path / "nothing") == []


def test_snapshot_copies_every_watcher_file_and_refuses_a_partial_directory(tmp_path):
    t = Threads(tmp_path / "threads")
    t.thread(FROZEN_LAST + timedelta(days=1), timedelta(hours=49), [12, 0])
    src = t.write()
    dest = fold.snapshot(src, tmp_path / "copy")
    assert sorted(p.name for p in dest.iterdir()) == sorted(fold.WATCH_FILES)
    (src / "x_metrics.jsonl.gz").unlink()
    try:
        fold.snapshot(src, tmp_path / "copy2")
    except FileNotFoundError as e:
        assert "x_metrics" in str(e)
    else:
        raise AssertionError("snapshot accepted a directory without x_metrics")


def test_post_pairs_compare_mature_posts_within_one_account(tmp_path):
    t = Threads(tmp_path / "threads")
    day = FROZEN_LAST + timedelta(days=1)
    hit = t.thread(day, timedelta(hours=49), [], root_likes=40)
    flop = t.thread(day + timedelta(hours=2), timedelta(hours=49), [], root_likes=2)
    t.thread(day + timedelta(hours=3), timedelta(hours=20), [], root_likes=0)  # not mature: no pair
    t.thread(day + timedelta(hours=4), timedelta(hours=49), [], author="other", root_likes=0)  # another account
    pairs = fold.post_pairs(load(t.write()))
    assert [(p.group, p.winner, p.loser) for p in pairs] == [("brand", hit, flop)]


def test_lineup_threads_keep_one_profile_with_reply_text_and_likes(tmp_path):
    t = Threads(tmp_path / "threads")
    day = FROZEN_LAST + timedelta(days=1)
    wnba = t.thread(day, timedelta(hours=2), [3, 1], profile="wnba")
    t.thread(day + timedelta(hours=1), timedelta(hours=2), [3, 1], author="b2", profile="devtools")
    src = t.write()
    threads = fold.lineup_threads(load(src), fold.profiles(src), "wnba")
    assert [x["id"] for x in threads] == [wnba]
    assert sorted(r["likes"] for r in threads[0]["direct_replies"]) == [1, 3]
