"""Dataset eligibility, sampling, predictors and the headline metric, on small synthetic inputs."""

import math
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.evals import backtest, leakage
from harness.evals.dataset import (
    Post,
    apply_enriched,
    attach_targets,
    attach_timing,
    created_at,
    is_eligible,
    is_scorable,
    is_thread_continuation,
)
from harness.evals.predictors import (
    JudgeRankPredictor,
    MediaPredictor,
    PanelPredictor,
    PostJudgePredictor,
    Prediction,
    TfidfPredictor,
    orders,
)
from harness.llm import MockLLM
from harness.models import Action
from harness.qualify import panel_scores_two_orders

LONG = "We shipped a new release with faster builds and a cleaner config format today."
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def post(i, account="@a", engagement=0, category=None, day=10, text=LONG, media="none", minutes=None, age=100.0):
    category = category or ("Medium Viral" if engagement >= 100 else "Deadweight")
    return Post(id=f"https://x.com/{account}/status/{i}", account=account, text=text, media=media, likes=engagement,
                replies=0, retweets=0, engagement=engagement, date=date(2026, 10, day), category=category,
                segment="Mid-size", created_at=T0 + timedelta(minutes=i if minutes is None else minutes),
                age_hours=age)


def test_created_at_decodes_the_status_id():
    when = datetime(2026, 9, 22, 16, 37, 5, 123000, tzinfo=timezone.utc)
    ms = int(when.timestamp() * 1000)
    status = ((ms - 1288834974657) << 22) | 12345  # the low 22 bits are worker and sequence, not time
    assert created_at(f"https://x.com/x/status/{status}") == when
    with pytest.raises(ValueError):
        created_at("https://x.com/x/status/abc")


def test_timing_marks_thread_continuations_and_age():
    posts = [post(1, minutes=0), post(2, minutes=2), post(3, minutes=10), post(4, "@b", minutes=11)]
    attach_timing(posts)
    assert [is_thread_continuation(p) for p in posts] == [False, True, False, False]
    assert posts[0].age_hours == pytest.approx(11 / 60)
    assert posts[3].age_hours == 0.0


def test_eligibility_needs_text_no_thread_and_48_hours():
    assert is_eligible(post(1, age=48.0))
    assert not is_eligible(post(1, age=47.9))
    p = post(2)
    p.minutes_after_previous = 1.0
    assert not is_eligible(p)
    assert not is_eligible(post(3, text="See the rest of the series here: https://t.co/x"))


def test_targets_are_relative_to_the_account_median():
    posts = [post(1, engagement=0), post(2, engagement=2), post(3, engagement=10), post(4, "@b", engagement=500)]
    attach_targets(posts)
    assert [p.target for p in posts[:3]] == pytest.approx([-math.log(3), 0.0, math.log(11 / 3)])
    assert posts[3].target == 0.0


def test_clean_text_and_scorable_filter():
    assert post(1, text="Ship it &amp; relax https://t.co/abc123").clean_text == "Ship it & relax"
    assert not is_scorable(post(3, text="@someone thanks for the kind words about our launch this week!"))
    assert is_scorable(post(4))


def test_enriched_file_must_cover_every_post(tmp_path):
    f = tmp_path / "enriched.csv"
    f.write_text("Post URL,eng_ratio_author_median,boost_suspect\nhttps://x.com/@a/status/1,2.0,true\n",
                 encoding="utf-8")
    posts = [post(1)]
    apply_enriched(posts, f)
    assert posts[0].target == pytest.approx(math.log(2.0)) and posts[0].boost_suspect is True
    with pytest.raises(ValueError, match="no row"):
        apply_enriched([post(9)], f)


def test_sample_takes_both_tiers_from_accounts_that_have_both():
    pool = ([post(i, engagement=500) for i in range(6)] + [post(10 + i, engagement=1) for i in range(2)]
            + [post(20 + i, "@onlyflops", engagement=1) for i in range(5)])
    picked = backtest.sample(pool, per_tier=4, seed=0)
    assert {p.account for p in picked} == {"@a"}
    assert sum(p.viral for p in picked) == 4 and sum(not p.viral for p in picked) == 2
    assert backtest.sample(pool, 4, seed=0) == picked  # seeded


def test_headline_counts_only_viral_vs_deadweight_pairs_within_accounts():
    posts = [post(1, engagement=500), post(2, engagement=300), post(3, engagement=5), post(4, engagement=1),
             post(5, "@b", engagement=900), post(6, "@b", engagement=0)]
    perfect = {p.id: Prediction(math.log1p(p.engagement)) for p in posts}
    r = backtest.evaluate(posts, perfect, n_boot=50)
    assert r["headline_viral_vs_deadweight_auc"]["value"] == 1.0
    assert r["headline_viral_vs_deadweight_auc"]["n_pairs"] == 2 * 2 + 1
    assert r["viral_vs_viral_by_engagement"]["n_pairs"] == 1
    assert r["deadweight_vs_deadweight_noise_check"]["n_pairs"] == 1
    flat = {p.id: Prediction(0.0) for p in posts}
    assert backtest.evaluate(posts, flat, n_boot=50)["headline_viral_vs_deadweight_auc"]["value"] == 0.5
    with pytest.raises(ValueError, match="no score"):
        backtest.evaluate(posts, {})


def test_relative_predictor_only_compares_posts_from_the_same_call():
    posts = [post(1, engagement=500), post(2, engagement=1), post(3, engagement=500), post(4, engagement=1)]
    # Calls x = {1, 2} and y = {3, 4} each rank correctly; across calls the scores disagree.
    preds = {posts[0].id: Prediction(10, {"call": "x"}), posts[1].id: Prediction(5, {"call": "x"}),
             posts[2].id: Prediction(3, {"call": "y"}), posts[3].id: Prediction(1, {"call": "y"})}
    assert backtest.evaluate(posts, preds, relative=False, n_boot=20)["headline_viral_vs_deadweight_auc"][
        "value"] == 0.75
    assert backtest.evaluate(posts, preds, relative=True, n_boot=20)["headline_viral_vs_deadweight_auc"][
        "value"] == 1.0


def test_paired_difference_is_zero_for_identical_predictors():
    posts = [post(i, engagement=500 if i % 2 else 1, account=f"@{i // 4}") for i in range(16)]
    a = {p.id: Prediction(float(p.viral)) for p in posts}
    d = backtest.paired_difference(posts, a, a, n_boot=100, seed=0)
    assert d["value"] == 0 and d["ci95"] == [0, 0]


def test_orders_averages_a_shuffle_and_its_reverse():
    o = orders([1, 2, 3, 4], "s")
    assert sorted(o[0]) == [1, 2, 3, 4] and o[1] == o[0][::-1]
    assert orders([7], "s") == [[7]]


def test_panel_predictor_scores_each_post_alone_by_default(tmp_path):
    pred = PanelPredictor(MockLLM(), {"dev": "a developer", "skeptic": "a skeptic"})
    out = pred.predict([post(i) for i in range(3)] + [post(9, "@b")])
    assert {p.score for p in out.values()} == {0.5}
    assert pred.calls == 4


class PositionBiasedLLM(MockLLM):
    """Scores the first draft in the prompt 1.0 and the rest 0.0."""

    def complete_json(self, system, prompt, mock=None):
        import re

        ids = re.findall(r'"id": "([^"]+)"', prompt)
        return {"scores": {i: {"dev": 1.0 if k == 0 else 0.0} for k, i in enumerate(ids)}}


def test_batched_panel_cancels_position_bias_by_averaging_orders():
    pred = PanelPredictor(PositionBiasedLLM(), {"dev": "a developer"}, batch=2)
    out = pred.predict([post(1), post(2)])
    assert [out[p].score for p in sorted(out)] == [0.5, 0.5]
    assert pred.calls == 2


def test_qualify_averages_two_orders():
    actions = [Action(type="post", text=t, mind="m", narrative_id="n", id=t) for t in ("a", "b", "c")]
    scores = panel_scores_two_orders(PositionBiasedLLM(), actions, {"dev": "a developer"})
    # Each draft is first in at most one of the two orders, so no draft can average above 0.5.
    assert all(s["dev"] <= 0.5 for s in scores.values())
    assert sum(s["dev"] for s in scores.values()) == pytest.approx(1.0)


def test_panel_predictor_drops_posts_it_cannot_score():
    class Partial(MockLLM):
        def complete_json(self, system, prompt, mock=None):
            return {"scores": {"p0": {"dev": 0.3}}}

    pred = PanelPredictor(Partial(), {"dev": "a developer", "skeptic": "a skeptic"})
    assert pred.predict([post(1)]) == {}
    assert pred.failures == [post(1).id]


def test_media_predictor_refuses_unknown_media():
    assert MediaPredictor().predict([post(1, media="video")])[post(1).id].score == 2.0
    with pytest.raises(ValueError, match="no rank"):
        MediaPredictor().predict([post(1, media="hologram")])


def test_tfidf_is_cross_fitted_and_learns_a_separable_word():
    pool = []
    for k in range(10):
        pool += [post(100 * k + i, f"@acct{k}", engagement=500, text=f"introducing turbo mode number {i} today")
                 for i in range(3)]
        pool += [post(100 * k + 50 + i, f"@acct{k}", engagement=1, text=f"podcast episode {i} is out now listen")
                 for i in range(3)]
    pred = TfidfPredictor(pool, folds=5, epochs=200)
    out = pred.predict(pool)
    assert backtest.evaluate(pool, out, n_boot=20)["headline_viral_vs_deadweight_auc"]["value"] == 1.0
    for p in pool:  # no post was scored by a model that trained on its account
        assert out[p.id].detail["fold"] == pred.fold(p.account)


def judge_stub(score=68, ref_day=1, ref_id="https://x.com/@z/status/7"):
    ref = SimpleNamespace(id=ref_id, date=f"2026-10-{ref_day:02d}")
    return lambda p: SimpleNamespace(score_100=score, grade=SimpleNamespace(confidence="Medium"), references=[ref])


def test_post_judge_adapter_reads_score_and_confidence():
    p = post(1)
    out = PostJudgePredictor(judge_stub()).predict([p])
    assert out[p.id].score == 0.68
    assert out[p.id].detail["confidence"] == "Medium"
    assert out[p.id].detail["references"] == ["https://x.com/@z/status/7"]


def test_post_judge_adapter_refuses_references_from_the_candidates_day_or_later():
    p = post(1, day=10)
    with pytest.raises(ValueError, match="own day or later"):
        PostJudgePredictor(judge_stub(ref_day=10)).predict([p])
    with pytest.raises(ValueError, match="own day or later"):
        PostJudgePredictor(judge_stub(ref_id=p.id)).predict([p])
    with pytest.raises(ValueError, match="0-100"):
        PostJudgePredictor(judge_stub(score=140)).predict([p])


def test_post_judge_failures_are_recorded_not_fatal():
    def boom(p):
        raise RuntimeError("max tokens")

    pred = PostJudgePredictor(boom)
    assert pred.predict([post(1)]) == {}
    assert pred.failures == [post(1).id]


def test_rank_predictor_splits_accounts_into_even_calls_in_two_orders():
    calls = []

    def rank(chunk):
        calls.append(len(chunk))
        return {p.id: 100 - i for i, p in enumerate(chunk)}

    posts = [post(i) for i in range(7)] + [post(50, "@solo")]
    out = JudgeRankPredictor(rank).predict(posts)
    assert sorted(calls) == [3, 3, 4, 4]  # 7 posts -> 4 + 3, each ranked forward and reversed
    assert out[post(50, "@solo").id].detail == {"call": post(50, "@solo").id, "unranked": True}


def test_leakage_text_measures():
    assert leakage.verbatim("a b c d e f g h i", "A b c d e f g h, then more")
    assert not leakage.verbatim("a b c", "a b c")  # too short to call verbatim
    assert leakage.halves("one two three four five") == ("one two", "three four five")
