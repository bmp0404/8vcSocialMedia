"""Amplifier ranking and the polling replay, on synthetic data with hand-computed answers."""

from datetime import datetime, timedelta, timezone

import pytest

from harness.rank import amplifiers as A

T0 = datetime(2026, 10, 2, tzinfo=timezone.utc)
WINDOW = (T0, T0 + timedelta(days=2))


def test_rank_reposters_counts_other_brands_inside_the_follower_band():
    brand_of = {"p1": "acme", "p2": "bolt", "p3": "acme", "p4": "cobra"}
    reposts = [
        {"post_id": "p1", "handle": "Fan", "followers": 5000},
        {"post_id": "p2", "handle": "fan", "followers": 5000},
        {"post_id": "p3", "handle": "fan", "followers": 5000},  # a second acme post: one brand, two reposts
        {"post_id": "p1", "handle": "other", "followers": 2000},
        {"post_id": "p2", "handle": "other", "followers": 2000},
        {"post_id": "p1", "handle": "huge", "followers": 100_001},  # above the band
        {"post_id": "p1", "handle": "tiny", "followers": 999},  # below it
        {"post_id": "p3", "handle": "acme", "followers": 9000},  # a brand reposting itself
        {"post_id": "p9", "handle": "fan", "followers": 5000},  # a post we can't attribute
        {"post_id": "p4", "empty": True},
    ]
    assert A.rank_reposters(reposts, brand_of) == [
        {"handle": "fan", "followers": 5000, "brands": 2, "count": 3},
        {"handle": "other", "followers": 2000, "brands": 2, "count": 2},
    ]


def test_rank_repliers_counts_each_reply_once():
    brand_of = {"1": "acme", "2": "bolt"}
    replies = [
        {"id": "a", "reply_to_post": "1", "author_handle": "Dev", "author_followers": 3000},
        {"id": "a", "reply_to_post": "1", "author_handle": "Dev", "author_followers": 3000},  # both sort orders
        {"id": "b", "reply_to_post": 2, "author_handle": "dev", "author_followers": 3000},
        {"id": "c", "reply_to_post": "1", "author_handle": "acme", "author_followers": 9000},  # the brand itself
        {"reply_to_post": "1", "error": "timeout"},
    ]
    assert A.rank_repliers(replies, brand_of) == [{"handle": "dev", "followers": 3000, "brands": 2, "count": 2}]


def test_original_and_on_topic_posts():
    own = {"author_handle": "Acme", "text": "Our coding agent is out", "lang": "en"}
    assert A.is_original(own, "acme") and A.on_topic(own)
    assert not A.is_original({**own, "is_repost": True}, "acme")
    assert not A.is_original({**own, "replying_to": "bolt"}, "acme")
    assert not A.is_original({**own, "author_handle": "bolt"}, "acme")
    assert not A.on_topic({**own, "lang": "ja"}) and not A.on_topic({**own, "text": "lunch"})


def test_detection_delays_follow_the_round_robin_schedule():
    # Two handles at 0.2 reads/s: a reads at 0 s, 10 s, 20 s...; b at 5 s, 15 s, 25 s...
    posts = {"a": [T0 + timedelta(seconds=3), T0 + timedelta(seconds=10)], "b": [T0 - timedelta(seconds=4)]}
    assert A.detection_delays(posts, ["a", "b"], T0, rate=0.2) == {"a": [7.0, 0.0], "b": [9.0]}


def test_open_rate_counts_threads_with_two_or_more_small_top_replies():
    assert A.open_rate((3, 2, 1, 0)) == 0.5
    assert A.open_rate(()) is None


def test_yield_counts_fresh_posts_per_covered_day_times_open_rate():
    a = A.Account("a", (T0, T0 + timedelta(hours=1), T0 + timedelta(days=1)), 2.0, (2, 2, 0, 0), quiet=4)  # 1.5/day, open 0.5
    b = A.Account("b", (T0,), 1.0, (), fallback=(3, 0, 0, 0))  # no scored threads: takes its fallback's rate
    out = A.yield_per_day([a, b], rate=0.2, window=WINDOW)
    assert out == {"handles": 2, "read_every_s": 10.0, "quiet_posts_per_day": 2.0, "posts_per_day": 2.5,
                   "fresh_per_day": 2.5, "open_fresh_per_day": round(1.5 * 0.5 + 1.0 * 0.25, 2)}


def test_a_long_list_misses_the_freshness_bar():
    # 150 handles at 0.2 reads/s: each read every 750 s, so a post just after a read waits past 10 minutes.
    accounts = [A.Account(f"h{i}", (T0 + timedelta(seconds=5 * i + 1),), 1.0, (3,)) for i in range(150)]
    out = A.yield_per_day(accounts, rate=0.2, window=WINDOW)
    assert out["read_every_s"] == 750.0 and out["posts_per_day"] == 150.0 and out["fresh_per_day"] == 0.0


def test_brand_accounts_use_busy_originals_inside_the_window():
    timelines = [
        {"timeline_of": "Acme", "author_handle": "acme", "replies": 9, "created_at": (T0 - timedelta(days=3)).isoformat()},
        {"timeline_of": "Acme", "author_handle": "acme", "replies": 3, "created_at": (T0 + timedelta(hours=2)).isoformat()},
        {"timeline_of": "Acme", "author_handle": "acme", "replies": 2, "created_at": (T0 + timedelta(hours=4)).isoformat()},
        {"timeline_of": "Acme", "author_handle": "bolt", "is_repost": True, "replies": 50,
         "created_at": (T0 + timedelta(hours=3)).isoformat()},
        {"timeline_of": "Acme", "error": "HTTP 404"},
    ]
    [acc] = A.brand_accounts(["acme"], {"acme": [2, 1]}, timelines, WINDOW)
    assert acc == A.Account("acme", (T0 + timedelta(hours=2),), 2.0, (2, 1), quiet=1)


def test_candidate_accounts_cover_only_what_the_page_reaches():
    row = {"handle": "fan", "fetched_at": (T0 + timedelta(days=5)).isoformat(), "threads": [{"small_in_top3": 3}],
           "timeline": [
               {"author_handle": "fan", "created_at": (T0 + timedelta(days=1)).isoformat(), "lang": "en",
                "text": "launch day", "replies": 4},
               {"author_handle": "fan", "created_at": (T0 + timedelta(days=1, hours=12)).isoformat(), "lang": "en",
                "text": "lunch", "replies": 7},
               {"author_handle": "fan", "created_at": (T0 + timedelta(days=1, hours=13)).isoformat(), "lang": "en",
                "text": "lunch again", "replies": 0},
           ]}
    [topical] = A.candidate_accounts([row, {"handle": "gone", "error": "HTTP 404"}], WINDOW)
    assert topical.covered_days == pytest.approx(1.0) and topical.posts == (T0 + timedelta(days=1),)
    [everything] = A.candidate_accounts([row], WINDOW, topical=False)
    assert len(everything.posts) == 2 and everything.quiet == 1 and everything.threads == (3,)


def test_a_repost_of_an_old_post_doesnt_stretch_coverage():
    row = {"handle": "fan", "fetched_at": (T0 + timedelta(days=5)).isoformat(), "threads": [], "timeline": [
        {"author_handle": "fan", "created_at": (T0 + timedelta(days=1, hours=12)).isoformat(), "lang": "en", "text": "x"},
        {"author_handle": "big", "is_repost": True, "created_at": (T0 - timedelta(days=20)).isoformat(), "text": "y"},
    ]}
    [acc] = A.candidate_accounts([row], WINDOW, topical=False)
    assert acc.covered_days == pytest.approx(0.5)
    [none] = A.candidate_accounts([{**row, "timeline": row["timeline"][1:]}], WINDOW, topical=False)
    assert none.covered_days == 0.0 and none.posts == ()
