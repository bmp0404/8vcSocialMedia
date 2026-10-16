"""Openness aggregates and the date-split check, on synthetic threads with hand-computed answers."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from harness.rank import target as T

T0 = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def reply(rid, followers, likes, minutes, author=None):
    return T.Reply(rid, author or f"u{rid}", followers, likes, T0 + timedelta(minutes=minutes))


def thread(root, author, replies, day=0, author_followers=50_000):
    start = T0 + timedelta(days=day)
    shifted = tuple(T.Reply(r.id, r.author, r.followers, r.likes, start + (r.created_at - T0)) for r in replies)
    return T.Thread(root, author, start, author_followers, shifted)


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def sample_row(rid, root, author, text, parent=None, followers=10, likes=1, minutes=5):
    return {"id": rid, "reply_to_post": root, "replying_to_status": parent or root, "author_handle": author,
            "text": text, "author_followers": followers, "likes": likes,
            "created_at": (T0 + timedelta(minutes=minutes)).isoformat()}


def test_load_threads_keeps_direct_replies_by_others_with_words(tmp_path):
    roots, samples = tmp_path / "roots.jsonl", tmp_path / "samples.jsonl"
    write_jsonl(roots, [{"id": "100", "author_handle": "Acme", "created_at": T0.isoformat(), "author_followers": 9000},
                        {"id": "999", "error": "HTTP 404"}])
    write_jsonl(samples, [
        sample_row("1", "100", "fan_a", "@Acme love it"),
        sample_row("1", "100", "fan_a", "@Acme love it"),  # seen in both sort orders
        sample_row("2", "100", "acme", "@Acme thanks all"),  # the root's own author
        sample_row("3", "100", "fan_c", "@Acme @fan_a "),  # only handles
        sample_row("4", "100", "fan_d", "@fan_a agreed", parent="1"),  # a reply to a reply
        {"reply_to_post": "100", "error": "timeout"},
        {"reply_to_post": "100", "empty": True},
    ])
    [t] = T.load_threads(samples, roots)
    assert (t.root_id, t.author, t.author_followers, t.created_at) == ("100", "acme", 9000, T0)
    assert [r.id for r in t.replies] == ["1"]


def test_load_threads_refuses_a_reply_to_an_unknown_root(tmp_path):
    roots, samples = tmp_path / "roots.jsonl", tmp_path / "samples.jsonl"
    write_jsonl(roots, [])
    write_jsonl(samples, [sample_row("1", "100", "fan", "hi")])
    with pytest.raises(ValueError, match="100"):
        T.load_threads(samples, roots)


def test_missing_samples_say_why_and_where(tmp_path):
    with pytest.raises(FileNotFoundError, match="never committed"):
        T.load_threads(tmp_path / "nope.jsonl", tmp_path / "roots.jsonl")


def test_top_replies_rank_by_likes_then_earlier():
    t = thread("r", "acme", [reply("a", 10, 5, 30), reply("b", 10, 9, 40), reply("c", 10, 5, 10), reply("d", 10, 1, 1)])
    assert [r.id for r in T.top_replies(t)] == ["b", "c", "a"]
    assert T.top_replies(thread("r", "acme", [reply("a", 1, 1, 1), reply("b", 1, 1, 2)])) is None


def test_openness_table_pools_top3_replies_per_author():
    t1 = thread("1", "acme", [reply("a", 500, 9, 4), reply("b", 5000, 8, 6), reply("c", 999, 7, 20), reply("d", 10, 0, 1)])
    t2 = thread("2", "acme", [reply("e", 1000, 3, 30), reply("f", 2000, 2, 50), reply("g", 20, 1, 70)], day=1)
    t3 = thread("3", "acme", [reply("h", 1, 1, 1), reply("i", 1, 1, 1)], day=2)  # under 3 replies: not scored
    t4 = thread("4", "other", [reply("j", 1, 3, 2), reply("k", 1, 2, 3), reply("l", 1, 1, 4)])
    table = T.openness_table([t1, t2, t3, t4])
    # acme: small top-3 replies a, c (t1) and g (t2) = 3 of 6; minutes 4, 6, 20, 30, 50, 70 -> median 25.
    assert table == {"acme": {"share_small_top3": 0.5, "median_top3_minutes": 25.0, "n": 2},
                     "other": {"share_small_top3": 1.0, "median_top3_minutes": 3.0, "n": 1}}


def test_openness_normalises_the_handle_and_reports_no_data_as_n_zero():
    table = {"acme": {"share_small_top3": 0.5, "median_top3_minutes": 25.0, "n": 2}}
    assert T.openness("@ACME ", table) == table["acme"]
    assert T.openness("nobody", table) == {"share_small_top3": None, "median_top3_minutes": None, "n": 0}
    T.openness("acme", table)["n"] = 99  # callers get a copy
    assert table["acme"]["n"] == 2


def test_committed_table_names_only_dataset_brand_accounts():
    allowed = T.public_accounts()
    rows = T.load_table()
    assert rows and set(rows) <= allowed
    assert all(set(r) == {"share_small_top3", "median_top3_minutes", "n"} for r in rows.values())


def test_build_writes_only_allowed_accounts(tmp_path):
    t1 = thread("1", "acme", [reply("a", 1, 3, 1), reply("b", 1, 2, 2), reply("c", 1, 1, 3)])
    t2 = thread("2", "somebody", [reply("d", 1, 3, 1), reply("e", 1, 2, 2), reply("f", 1, 1, 3)])
    out = T.build([t1, t2], {"acme"}, tmp_path / "openness.json")
    assert set(out["accounts"]) == {"acme"}
    assert set(json.loads((tmp_path / "openness.json").read_text(encoding="utf-8"))["accounts"]) == {"acme"}


def open3(root, author, day, small):
    """A scored thread whose top 3 hold `small` accounts under 1,000 followers."""
    reps = [reply(f"{root}{i}", 10 if i < small else 50_000, 10 - i, 5 + i) for i in range(3)]
    return thread(root, author, reps, day=day)


def test_rolling_predictions_use_only_threads_before_the_window():
    c0, c1, c2 = (T0 + timedelta(days=d) for d in (3, 6, 9))
    threads = [
        open3("a1", "acme", 0, 3), open3("a2", "acme", 1, 0),  # history before c0: share 0.5
        open3("a3", "acme", 3, 3),  # starts exactly at c0, so it is tested against the two above
        open3("a4", "acme", 7, 0),  # window [c1, c2): history a1-a3, share 6/9
        open3("b1", "solo", 0, 3), open3("b2", "solo", 4, 3),  # one earlier thread: below min_history
        open3("z9", "acme", 10, 1),  # after the last cutoff: never tested
    ]
    preds = T.rolling_predictions(threads, (c0, c1, c2), min_history=2)
    got = {(p.thread.root_id, p.window): (p.prior["share_small_top3"], p.prior["n"]) for p in preds}
    assert got == {("a3", c0.date().isoformat()): (0.5, 2), ("a4", c1.date().isoformat()): (0.667, 3)}
    assert {p.thread.root_id for p in T.rolling_predictions(threads, (c0, c1, c2), min_history=1)} == {"a3", "a4", "b2"}


def test_score_predictions_auc_counts_ties_as_half():
    def pred(root, author, prior_share, small):
        return T.Prediction(open3(root, author, 0, small), {"share_small_top3": prior_share, "median_top3_minutes": 6.0, "n": 2}, "w")
    preds = [pred("1", "a", 0.9, 2), pred("2", "b", 0.5, 1), pred("3", "c", 0.5, 0), pred("4", "d", 0.1, 0)]
    res = T.score_predictions(preds, n_boot=50)
    # Positives (any small in top 3): 0.9, 0.5. Negatives: 0.5, 0.1. Pairs: (0.9>0.5)=1, (0.9>0.1)=1,
    # (0.5=0.5)=0.5, (0.5>0.1)=1 -> 3.5 / 4.
    assert res["any_small_by_prior_share"]["auc"] == 0.875
    assert (res["any_small_by_prior_share"]["positives"], res["any_small_by_prior_share"]["negatives"]) == (2, 2)
    assert res["n_threads"] == 4 and res["n_authors"] == 4 and res["any_small_base_rate"] == 0.5
