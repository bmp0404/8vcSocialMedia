"""Folding the watcher's threads into the rolling benchmark: mature threads join under their root's week,
and nothing rooted inside a frozen or fitted window ever does."""

import json
from datetime import datetime, timedelta, timezone

from watch_threads import Threads

from harness.corpus import load
from harness.corpus.bench import Pair, week_of
from harness.improve import fold

FROZEN_LAST = datetime(2026, 10, 16, 6, tzinfo=timezone.utc)
FROZEN = [Pair("r0:a:b", "replies", "r0", "a", "b", 9, 1, "test", 4, FROZEN_LAST.isoformat(), 1.0)]


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


def test_a_reply_from_the_roots_first_hours_counts_though_it_is_under_48h_old_at_the_read(tmp_path):
    # Root read at +49h; replies at +2h and +2h05m are 47 hours old at that read. The frozen rule (each
    # reply read 48h after itself) would drop them; the rolling rule keeps them.
    from harness.corpus.bench import both_mature

    t = Threads(tmp_path / "threads")
    root = t.thread(FROZEN_LAST + timedelta(days=1), timedelta(hours=49), [12, 0], reply_delay=timedelta(hours=2))
    corpus = load(t.write())
    assert {p.group for p in fold.fold(corpus, FROZEN, []).pairs} == {root}
    from harness.corpus.bench import reply_pairs
    assert reply_pairs(corpus, settled=both_mature) == []


def test_corpus_threads_keep_watched_roots_at_the_coverage_line_with_direct_replies_only(tmp_path):
    from harness.watcher.watchlist import Watched, Watchlist

    wl = Watchlist("devtools", (Watched("devtools", "supabase", False), Watched("devtools", "vercel", True)),
                   ("agents",))
    t = Threads(tmp_path / "corpus")
    day = FROZEN_LAST - timedelta(days=5)
    kept = t.thread(day, timedelta(days=3), [5, 3], author="supabase", coverage=0.8)
    t.thread(day + timedelta(hours=1), timedelta(days=3), [5, 3], author="supabase", coverage=0.79)  # thin sample
    t.thread(day + timedelta(hours=2), timedelta(days=3), [5, 3], author="someone")  # not watched
    t.thread(day + timedelta(hours=3), timedelta(days=3), [5, 3], author="vercel", root_text="a launch post")
    broad = t.thread(day + timedelta(hours=4), timedelta(days=3), [5, 3], author="vercel",
                     root_text="coding agents in your repo")  # a broad account counts only with a keyword
    threads = fold.corpus_threads(load(t.write()), wl.covers, 0.8)
    assert [x["id"] for x in threads] == [kept, broad]
    assert sorted(r["likes"] for r in threads[0]["direct_replies"]) == [3, 5]
    assert set(threads[0]) == {"id", "author", "url", "text", "direct_replies"}
