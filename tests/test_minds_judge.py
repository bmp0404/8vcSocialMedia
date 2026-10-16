import json

import pytest

from harness.llm import AnthropicLLM, LLM, MockLLM
from harness.minds import corpus, judge


class OrderRanker:
    """Ranks by a fixed preference list, so the result doesn't depend on input order."""

    name = "order"

    def __init__(self, prefer, verdicts=None, confidence="High"):
        self.prefer, self.verdicts, self.confidence = prefer, verdicts or {}, confidence
        self.calls = []

    def rank(self, texts, **kw):
        self.calls.append(list(texts))
        order = sorted(texts, key=self.prefer.index)
        return [judge.Ranked(t, 90 - i, self.verdicts.get(t, "Strong"), self.confidence) for i, t in enumerate(order)]


KW = dict(lane="reply", mission="m", context="c", references=[])


def test_select_ranks_in_both_orders_and_ships_a_stable_strong_pick():
    r = OrderRanker(["b", "a", "c"])
    sel = judge.select(r, ["a", "b", "c"], **KW)
    assert sel.ships and sel.best.text == "b" and [x.text for x in sel.ranked] == ["b", "a", "c"]
    assert r.calls == [["a", "b", "c"], ["c", "b", "a"]]


def test_select_dedupes_and_drops_blank_candidates():
    r = OrderRanker(["a", "b"])
    judge.select(r, ["a", "a", " ", "b"], **KW)
    assert r.calls[0] == ["a", "b"]


def test_select_refuses_when_the_top_pick_depends_on_order():
    class FirstWins:
        name = "first"

        def rank(self, texts, **kw):
            return [judge.Ranked(t, 90 - i, "Strong", "High") for i, t in enumerate(texts)]

    sel = judge.select(FirstWins(), ["a", "b"], **KW)
    assert not sel.ships and sel.why_not == "top pick changed when the order was reversed"


@pytest.mark.parametrize("verdict,confidence", [("Revise", "High"), ("Weak", "High"), ("Strong", "Low")])
def test_select_needs_strong_with_medium_or_high_confidence(verdict, confidence):
    sel = judge.select(OrderRanker(["a", "b"], {"a": verdict}, confidence), ["a", "b"], **KW)
    assert not sel.ships and verdict in sel.why_not and confidence in sel.why_not


def test_single_candidate_is_ranked_once():
    r = OrderRanker(["a"])
    assert judge.select(r, ["a"], **KW).ships and len(r.calls) == 1


def test_ranker_error_means_no_pick():
    class Broken:
        name = "broken"

        def rank(self, texts, **kw):
            raise judge.RankerError("judge timed out")

    sel = judge.select(Broken(), ["a", "b"], **KW)
    assert (sel.best, sel.ships, sel.why_not, sel.ranker) == (None, False, "judge timed out", "broken")


def test_no_candidates():
    assert judge.select(OrderRanker([]), [], **KW).why_not == "no candidates"


def test_mock_ranker_ignores_input_order():
    m = judge.MockRanker()
    assert [r.text for r in m.rank(["ccc", "a", "bb"], **KW)] == [r.text for r in m.rank(["bb", "ccc", "a"], **KW)]


class Canned(LLM):
    def __init__(self, out):
        self.out = out

    def complete_json(self, system, prompt, mock=None):
        return self.out


def test_llm_ranker_maps_ids_back_to_texts():
    out = {"ranked": [{"candidate_id": "c2", "score": 80, "verdict": "Strong", "confidence": "High", "reason": "r"},
                      {"candidate_id": "c1", "score": 20, "verdict": "Weak", "confidence": "Medium", "reason": "r"}]}
    ranked = judge.LLMRanker(Canned(out)).rank(["first", "second"], **KW)
    assert [(r.text, r.verdict) for r in ranked] == [("second", "Strong"), ("first", "Weak")]


@pytest.mark.parametrize("bad,match", [
    ({"ranked": [{"candidate_id": "c1", "score": 1, "verdict": "Strong", "confidence": "High"}]}, "exactly once"),
    ({"ranked": [{"candidate_id": "c1", "score": 1, "verdict": "Great", "confidence": "High"},
                 {"candidate_id": "c2", "score": 0, "verdict": "Weak", "confidence": "High"}]}, "unknown verdict 'Great'"),
    ({"nope": []}, "malformed"),
])
def test_llm_ranker_rejects_bad_output(bad, match):
    with pytest.raises(judge.RankerError, match=match):
        judge.LLMRanker(Canned(bad)).rank(["first", "second"], **KW)


def test_get_ranker_uses_mock_for_mock_llm():
    assert isinstance(judge.get_ranker(MockLLM()), judge.MockRanker)


def test_get_ranker_forced_post_judge_is_loud_when_missing(monkeypatch):
    monkeypatch.setenv("HARNESS_RANKER", "post_judge")
    monkeypatch.setattr(judge.PostJudgeRanker, "__init__", lambda self: (_ for _ in ()).throw(judge.RankerError("post_judge unavailable")))
    with pytest.raises(judge.RankerError, match="post_judge unavailable"):
        judge.get_ranker(Canned({}))


def test_get_ranker_falls_back_to_a_gpt_model_for_anthropic(monkeypatch):
    monkeypatch.delenv("HARNESS_RANKER", raising=False)
    monkeypatch.delenv("HARNESS_SELECT_MODEL", raising=False)
    monkeypatch.setattr(judge.PostJudgeRanker, "__init__", lambda self: (_ for _ in ()).throw(judge.RankerError("missing")))
    monkeypatch.setattr(AnthropicLLM, "__init__", lambda self, model: setattr(self, "model", model))
    r = judge.get_ranker(AnthropicLLM("claude-sonnet-5-5"))
    assert isinstance(r, judge.LLMRanker) and r.llm.model == "gpt-6.1-sol"


# ---- corpus


@pytest.fixture
def tiny_corpus(tmp_path, monkeypatch):
    def reply(text, likes, followers=100):
        return {"text": text, "likes": likes, "replier_followers": followers, "minutes_after_root": 5}

    threads = [
        {"url": "u1", "direct_replies": [reply("now fire the coach", 300), reply("ok", 0), reply("big fan here", 50, 90000),
                                          reply("zero likes one", 0), reply("zero likes two", 0)]},
        {"url": "u2", "direct_replies": [reply("soft launching that sponsorship", 2000), reply("zero likes three", 0),
                                          reply("go team lol", 40)]},
    ]
    path = tmp_path / "threads.json"
    path.write_text(json.dumps({"threads": threads}), encoding="utf-8")
    monkeypatch.setenv("HARNESS_REPLY_CORPUS", str(path))
    corpus._load.cache_clear()
    yield path
    corpus._load.cache_clear()


def test_exemplars_skip_big_accounts_short_noise_and_the_answered_thread(tiny_corpus):
    assert [r["text"] for r in corpus.exemplars()] == ["soft launching that sponsorship", "now fire the coach", "go team lol",
                                                       "zero likes one", "zero likes two", "zero likes three"]
    assert "soft launching that sponsorship" not in [r["text"] for r in corpus.exemplars(exclude_url="u2")]


def test_references_have_both_classes_or_none(tiny_corpus):
    refs = corpus.references(per_class=2)
    assert [r["performance"] for r in refs] == ["Successful", "Successful", "Poor", "Poor"]
    assert all(r["engagement"] == 0 for r in refs if r["performance"] == "Poor")
    assert corpus.references(per_class=5) == []


def test_missing_corpus_gives_no_exemplars(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_REPLY_CORPUS", str(tmp_path / "missing.json"))
    corpus._load.cache_clear()
    assert corpus.exemplars() == [] and corpus.references() == []
    corpus._load.cache_clear()
