"""Dataset loading, sampling, predictors and the leakage text measures, on small synthetic inputs."""

import math
from datetime import date
from types import SimpleNamespace

import pytest

from harness.evals import backtest, leakage
from harness.evals.dataset import Post, apply_enriched, attach_targets, is_scorable
from harness.evals.predictors import (
    JudgeRankPredictor,
    MediaPredictor,
    PanelPredictor,
    PostJudgePredictor,
    Prediction,
)
from harness.llm import MockLLM

LONG = "We shipped a new release with faster builds and a cleaner config format today."


def post(i, account="@a", engagement=0, category="Deadweight", day=10, text=LONG, media="none"):
    return Post(id=f"https://x.com/{account}/status/{i}", account=account, text=text, media=media, likes=engagement,
                replies=0, retweets=0, engagement=engagement, date=date(2026, 10, day), category=category,
                segment="Mid-size")


def test_targets_are_relative_to_the_account_median():
    posts = [post(1, engagement=0), post(2, engagement=2), post(3, engagement=10), post(4, "@b", engagement=500)]
    attach_targets(posts)
    assert [p.target for p in posts[:3]] == pytest.approx([-math.log(3), 0.0, math.log(11 / 3)])
    assert posts[3].target == 0.0


def test_clean_text_and_scorable_filter():
    p = post(1, text="Ship it &amp; relax https://t.co/abc123")
    assert p.clean_text == "Ship it & relax"
    assert not is_scorable(post(2, text="See the rest of the series here: https://t.co/x"))
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
    bad = tmp_path / "bad.csv"
    bad.write_text("Post URL,ratio\nx,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must have columns"):
        apply_enriched(posts, bad)


def test_split_keeps_train_strictly_before_the_cutoff():
    posts = [post(1, day=5), post(2, day=6), post(3, day=7, text="#tag")]
    s = backtest.split(posts, date(2026, 10, 6))
    assert [p.id for p in s.train] == [posts[0].id]
    assert [p.id for p in s.holdout] == [posts[1].id]
    assert s.dropped_unscorable == 1


def test_sample_rotates_categories_within_an_account():
    posts = [post(i, category="Deadweight") for i in range(10)] + [post(99, category="Super Viral")]
    picked = backtest.sample(posts, per_account=3, max_accounts=None, seed=0)
    assert len(picked) == 3
    assert sum(p.category == "Super Viral" for p in picked) == 1


def test_evaluate_scores_a_perfect_within_account_predictor():
    posts = [post(i, account=a, engagement=e) for i, (a, e) in
             enumerate([("@a", 0), ("@a", 5), ("@a", 50), ("@b", 1000), ("@b", 3000)])]
    attach_targets(posts)
    preds = {p.id: Prediction(p.target) for p in posts}
    r = backtest.evaluate(posts, preds, probabilistic=False, n_boot=50)
    assert r["within_account"]["pairwise_accuracy"]["value"] == 1.0
    assert r["within_account"]["pairwise_accuracy"]["n_pairs"] == 4
    assert r["within_account"]["top_pick_beats_median"]["value"] == 1.0
    with pytest.raises(ValueError, match="no score"):
        backtest.evaluate(posts, {}, probabilistic=False)


def test_media_predictor_refuses_unknown_media():
    assert MediaPredictor().predict([post(1, media="video")])[post(1).id].score == 2.0
    with pytest.raises(ValueError, match="no rank"):
        MediaPredictor().predict([post(1, media="hologram")])


def test_panel_predictor_on_mock_gives_every_post_the_same_score(tmp_path):
    panel = tmp_path / "panel.json"
    panel.write_text('{"dev": "a developer", "skeptic": "a skeptic"}', encoding="utf-8")
    pred = PanelPredictor(MockLLM(), panel, batch=2)
    posts = [post(i) for i in range(3)] + [post(9, "@b")]
    out = pred.predict(posts)
    assert {p.score for p in out.values()} == {0.5}
    assert pred.calls == 3  # @a needs two batches of at most 2, @b one


def test_panel_predictor_rejects_missing_panelists(tmp_path):
    panel = tmp_path / "panel.json"
    panel.write_text('{"dev": "a developer", "skeptic": "a skeptic"}', encoding="utf-8")

    class Partial(MockLLM):
        def complete_json(self, system, prompt, mock=None):
            return {"scores": {"p0": {"dev": 0.3}}}

    with pytest.raises(ValueError, match="expected"):
        PanelPredictor(Partial(), panel).predict([post(1)])


def judge_stub(score=68, ref_day=1, ref_id="https://x.com/@z/status/7"):
    ref = SimpleNamespace(id=ref_id, date=f"2026-10-{ref_day:02d}")
    return lambda p: SimpleNamespace(score_100=score, grade=SimpleNamespace(confidence="Medium"), references=[ref])


def test_post_judge_adapter_reads_score_and_confidence():
    p = post(1)
    out = PostJudgePredictor(judge_stub(), holdout_from=date(2026, 10, 6)).predict([p])
    assert out[p.id].score == 0.68
    assert out[p.id].detail["confidence"] == "Medium"
    assert out[p.id].detail["references"] == ["https://x.com/@z/status/7"]


def test_post_judge_adapter_refuses_holdout_references():
    p = post(1)
    with pytest.raises(ValueError, match="holdout"):
        PostJudgePredictor(judge_stub(ref_day=6), holdout_from=date(2026, 10, 6)).predict([p])
    with pytest.raises(ValueError, match="holdout"):
        PostJudgePredictor(judge_stub(ref_id=p.id), holdout_from=date(2026, 10, 6)).predict([p])
    with pytest.raises(ValueError, match="0-100"):
        PostJudgePredictor(judge_stub(score=140), holdout_from=date(2026, 10, 6)).predict([p])


def test_leakage_text_measures():
    assert leakage.lcs_len(["a", "b", "c", "d"], ["a", "c", "d", "x"]) == 3
    assert leakage.overlap("Faster builds, cleaner config.", "faster builds and a cleaner config") == 1.0
    assert leakage.overlap("one two three four", "nothing alike") == 0.0
    assert leakage.verbatim("a b c d e f g h i", "A b c d e f g h, then more")
    assert not leakage.verbatim("a b c", "a b c")  # too short to call verbatim
    assert leakage.halves("one two three four five") == ("one two", "three four five")


def test_rank_predictor_splits_accounts_into_even_calls():
    calls = []

    def rank(chunk):
        calls.append(len(chunk))
        return {p.id: 100 - i for i, p in enumerate(chunk)}

    posts = [post(i) for i in range(7)] + [post(50, "@solo")]
    out = JudgeRankPredictor(rank).predict(posts)
    assert sorted(calls) == [3, 4]  # 7 posts -> 4 + 3, never 6 + 1
    assert out[post(50, "@solo").id].score == 50.0 and out[post(50, "@solo").id].detail == {"unranked": True}


def test_rank_predictor_refuses_a_ranking_with_wrong_ids():
    with pytest.raises(ValueError, match="expected"):
        JudgeRankPredictor(lambda chunk: {"other": 1.0, "ids": 2.0}).predict([post(1), post(2)])
