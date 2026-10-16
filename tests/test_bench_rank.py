"""Benchmark pair rules, bootstrap statistics, the pairwise ranker and the openness signal, on synthetic data."""

import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from harness.corpus import Corpus, Item
from harness.corpus import bench
from harness.corpus.stats import accuracy, pair_credit, paired_difference
from harness.rank import pairwise, reply
from harness.rank.target import openness_table

T0 = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
FETCH = "2026-10-16T02:00:00+00:00"


def mk(sid, author, kind="post", likes=0, minutes=0, root=None, text="words here", followers=100, fetched=FETCH):
    return Item(status_id=str(sid), kind=kind, author=author, text=text, created_at=T0 + timedelta(minutes=minutes),
                author_followers=followers, author_verified=False, author_description=None,
                parent_id=str(root) if root else None, parent_author="acme" if root else None, quoted_id=None,
                media_types=[], n_links=0, n_mentions=0, lang="en",
                metrics={"likes": likes, "reposts": 0, "replies": 0, "quotes": 0, "bookmarks": 0, "views": 0,
                         "fetched_at": fetched},
                sources=["reply_samples"] if root else ["x_metrics"], thread_root=str(root) if root else None,
                thread_coverage=0.9 if root else None, in_dataset=root is None)


def thread_corpus():
    items = [
        mk(1, "acme"),
        mk(2, "fan_a", "reply", likes=6, minutes=5, root=1),   # beats 3 (6 >= 3*2) and 4 (6 >= 3*0)
        mk(3, "fan_b", "reply", likes=2, minutes=50, root=1),
        mk(4, "fan_c", "reply", likes=0, minutes=90, root=1),
        mk(5, "acme", "reply", likes=40, minutes=1, root=1),   # the root's own author: excluded
        mk(6, "fan_d", "reply", likes=30, minutes=2, root=1, text="@acme"),  # no words of its own: excluded
        mk(7, "fan_e", "reply", likes=4, minutes=3, root=1),   # under 5 likes, so never a winner
    ]
    nested = mk(8, "fan_f", "reply", likes=50, minutes=10, root=1)
    nested.parent_id = "2"  # a reply to a reply
    return Corpus(items + [nested], Counter(), Counter())


def test_reply_pairs_follow_the_margin_and_exclusions():
    pairs = bench.reply_pairs(thread_corpus())
    assert {(p.winner, p.loser) for p in pairs} == {("2", "3"), ("2", "4")}
    assert all(p.split == "dev" and p.week == 0 and p.group == "1" and p.coverage == 0.9 for p in pairs)
    looser = bench.reply_pairs(thread_corpus(), min_likes=3, ratio=2.0)
    assert ("7", "4") in {(p.winner, p.loser) for p in looser} and ("2", "7") not in {(p.winner, p.loser) for p in looser}


def test_immature_replies_are_left_out():
    c = thread_corpus()
    c.get("4").metrics["fetched_at"] = (c.get("4").created_at + timedelta(hours=10)).isoformat()
    assert {(p.winner, p.loser) for p in bench.reply_pairs(c)} == {("2", "3")}


def test_cap_and_week_and_author_split_are_deterministic():
    many = [bench.Pair(f"p{i}", "replies", "g", str(i), str(i + 1000), 9, 1, "dev") for i in range(80)]
    a, b = bench._cap(list(many), random.Random(1)), bench._cap(list(many), random.Random(1))
    assert len(a) == bench.MAX_PAIRS_PER_GROUP and a == b
    assert bench.week_of(bench.WEEK0 + timedelta(days=7, seconds=1)) == 1
    assert bench.author_split("Supabase") == bench.author_split("supabase")
    assert Counter(bench.author_split(f"acct{i}") for i in range(400))["dev"] in range(160, 241)


def test_reply_body_strips_only_the_leading_mentions():
    assert bench.reply_body("@a @b_c  thanks @d!") == "thanks @d!"
    assert bench.reply_body("@a") == ""


def test_accuracy_and_paired_difference():
    assert pair_credit(2, 1) == 1.0 and pair_credit(1, 1) == 0.5 and pair_credit(0, 1) == 0.0
    credits, groups = [1.0] * 30 + [0.0] * 10, [f"g{i % 8}" for i in range(40)]
    iv = accuracy(credits, groups, b=500)
    assert iv.estimate == 0.75 and 0.0 <= iv.low <= 0.75 <= iv.high <= 1.0 and iv.n_clusters == 8
    same = paired_difference(credits, credits, groups, b=200)
    assert same.estimate == 0.0 and same.low == 0.0 and same.high == 0.0
    with pytest.raises(ValueError):
        accuracy([], [])


def test_pairwise_logistic_recovers_the_direction_that_wins():
    rng = random.Random(3)
    diffs = []
    for _ in range(300):
        a, b = {"good": rng.random(), "noise": rng.random()}, {"good": rng.random(), "noise": rng.random()}
        w, l = (a, b) if a["good"] > b["good"] else (b, a)
        diffs.append(pairwise.diff(w, l))
    weights = pairwise.fit(diffs, l2=1e-3)
    assert weights["good"] > 1.0 and abs(weights.get("noise", 0.0)) < weights["good"] / 3
    best, scores = pairwise.choose_l2(diffs, [str(i % 10) for i in range(300)])
    assert best in scores


def test_text_features_and_ngram_hashing():
    f = reply.text_features("We shipped Postgres 18 support", "@acme I love the postgres support?! 🎉", media=True)
    assert f["x:question"] == 1.0 and f["x:first_person"] == 1.0 and f["x:media"] == 1.0
    assert f["x:log_emoji"] > 0 and f["x:overlap"] > 0 and not any(k.startswith("h:") for k in f)
    g = reply.ngram_features("@acme love the postgres support")
    assert g == reply.ngram_features("@other love the postgres support") and len(g) == 7  # 4 words + 3 bigrams


def test_benchmark_refuses_a_model_trained_past_the_cutoff():
    model = reply.ReplyRanker(("timing",), datetime(2026, 10, 10, tzinfo=timezone.utc), {}, {}, {}, 0.0, 0, 0)
    with pytest.raises(ValueError, match="after the cutoff"):
        reply.benchmark(model, datetime(2026, 10, 6, tzinfo=timezone.utc), corpus=thread_corpus(), pairs=[])


def test_ranker_score_matches_its_weights():
    model = reply.ReplyRanker(("timing",), T0, {"t:log_minutes": -1.0, "t:log_followers": 0.5}, {}, {}, 0.0, 0, 0)
    early = model.score("root", "hi there", minutes_after=1, followers=100)
    late = model.score("root", "hi there", minutes_after=600, followers=100)
    assert early > late


def test_openness_counts_small_accounts_among_top_replies():
    c = thread_corpus()
    c.get("3").author_followers = 5000
    table = openness_table(c)
    # Top 3 by likes among fan_a (6), fan_e (4), fan_b (2): two of three under 1,000 followers.
    assert table["acme"] == {"share_small_top3": 0.667, "median_top3_minutes": 5.0, "n": 1}


def test_char_wb_and_word_grams_match_scikit_learn_conventions():
    from harness.rank.post import _char_wb, _word_grams
    # TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 3)) on "ab cd" gives " ab", "ab ", " cd", "cd ".
    assert _char_wb("ab cd", 3, 3) == [" ab", "ab ", " cd", "cd "]
    assert _char_wb("a", 3, 5) == [" a "]
    # The default token pattern drops one-letter tokens.
    assert _word_grams("I ship a Fast API") == ["ship", "fast", "api", "ship fast", "fast api"]


def test_tfidf_uses_smoothed_idf_sublinear_tf_and_unit_rows():
    import math as m
    from harness.rank.post import Tfidf
    t = Tfidf(lambda s: s.split(), min_df=1, max_features=10, prefix="w:").fit(["a a b", "b c"])
    assert t.idf["a"] == pytest.approx(m.log(3 / 2) + 1) and t.idf["b"] == pytest.approx(1.0)
    row = t.transform("a a b")
    raw_a, raw_b = (1 + m.log(2)) * t.idf["a"], 1.0
    norm = m.sqrt(raw_a ** 2 + raw_b ** 2)
    assert row == pytest.approx({"w:a": raw_a / norm, "w:b": raw_b / norm})
    assert Tfidf(lambda s: s.split(), min_df=2, max_features=10, prefix="w:").fit(["a b", "b c"]).idf.keys() == {"b"}


def test_ridge_matches_the_closed_form_on_a_tiny_problem(monkeypatch):
    import harness.rank.post as post
    # Two one-word documents, characters and shape switched off: X = I (unit rows),
    # so (X'X + alpha I) w = X'(y - mean) gives w_i = (y_i - mean) / (1 + alpha).
    monkeypatch.setattr(post, "_shape", lambda text: [0.0])
    model = post.ridge_fit(["alpha", "beta"], [3.0, 1.0], alpha=1.0, word_min_df=1, char_min_df=10**9)
    assert model.intercept == 2.0
    assert model.weights["w:alpha"] == pytest.approx(0.5) and model.weights["w:beta"] == pytest.approx(-0.5)
    assert model.predict("alpha") == pytest.approx(2.5) and model.predict("gamma") == pytest.approx(2.0)
