import json
from collections import Counter

import pytest

from harness import config
from harness.llm import LLM, MockLLM
from harness.minds import decide, judge, lanes
from harness.models import Action, Narrative

ESPN = "https://www.espn.com/wnba/story/_/id/1/boston-surgery"
X_URL = "https://x.com/AtlantaDream/status/2110808408366493714"
X_URL2 = "https://x.com/WNBA/status/2110808211347456506"


@pytest.fixture(autouse=True)
def tmp_out(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    return tmp_path


def narrative(targets=None, signal_urls=None, nid="finals") -> Narrative:
    n = Narrative(id=nid, title="Finals tip off Saturday", summary="Dream at Valkyries, Game 1 Oct 17",
                  relevance=0.9, velocity=0.8, signal_urls=signal_urls or [])
    if targets is not None:
        n.reply_targets = targets  # Observe's field, set duck-typed until sam/observe merges
    return n


def target(url=X_URL, author="AtlantaDream", age=20.0, eph=500.0, text="ATL, show up for Game 3"):
    return {"url": url, "author": author, "text": text, "age_minutes": age, "engagement_per_hour": eph}


class ScriptedLLM(LLM):
    """complete_json returns the next scripted object, whatever the prompt."""

    def __init__(self, replies):
        self.replies = list(replies)

    def complete_json(self, system, prompt, mock=None):
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FixedRanker:
    name = "fixed"

    def __init__(self, verdict="Strong", confidence="High", stable=True):
        self.verdict, self.confidence, self.stable = verdict, confidence, stable

    def rank(self, texts, **kw):
        order = texts if not self.stable else sorted(texts)
        return [judge.Ranked(t, 90 - i, self.verdict, self.confidence, "fixed") for i, t in enumerate(order)]


def run(llm, narratives, ranker=None, chorus=(), budget=None, monkeypatch=None):
    if ranker is not None:
        monkeypatch.setattr(judge, "get_ranker", lambda _llm: ranker)
    return lanes.run(llm, "mission", "lessons", narratives, list(chorus), budget=budget or lanes.Budget())


def last_log(tmp_out) -> list[dict]:
    path = sorted(tmp_out.glob("decisions-*.json"))[-1]
    return json.loads(path.read_text(encoding="utf-8"))["decisions"]


# ---- the bug PR #6 left on main: replies aimed at news links


def test_mock_decide_never_replies_to_signal_urls(tmp_out):
    actions = decide(MockLLM(), "mission", "lessons", [narrative(signal_urls=[ESPN, X_URL])], per_mind=1)
    assert not [a for a in actions if a.type in ("reply", "quote")]
    assert {"lane": "reply", "tool": "abstain", "reason": "no_target"}.items() <= last_log(tmp_out)[0].items()


def test_every_reply_targets_an_offered_url(tmp_out):
    actions = run(MockLLM(), [narrative([target()], signal_urls=[ESPN])])
    replies = [a for a in actions if a.type in ("reply", "quote")]
    assert replies and all(a.target_url == X_URL for a in replies)


def test_reply_to_an_unoffered_url_is_rejected(tmp_out, monkeypatch):
    llm = ScriptedLLM([{"tool": "reply", "target_url": ESPN, "text": "dream in 4"}])
    actions = run(llm, [narrative([target()])], FixedRanker(), monkeypatch=monkeypatch)
    assert actions == []
    d = last_log(tmp_out)[0]
    assert (d["tool"], d["reason"]) == ("abstain", "gate_rejected")
    assert "was not offered" in d["candidates"][0]["schema_error"]


# ---- reply lane


def test_reply_lane_ships_one_reply_with_lane_fields(tmp_out):
    actions = run(MockLLM(), [narrative([target()])])
    (a,) = actions
    assert (a.type, a.lane, a.mind, a.target_url, a.target_handle) == ("reply", "reply", "reply_lane", X_URL, "AtlantaDream")
    assert a.target_text == "ATL, show up for Game 3"
    assert a.draft_meta["ranker"] == "mock" and a.draft_meta["samples"] == lanes.REPLY_SAMPLES
    assert last_log(tmp_out)[0]["action_id"] == a.id


def test_targets_from_every_narrative_are_considered(tmp_out):
    # The chorus sees only the top narratives; the reply lane must still find targets elsewhere.
    narratives = [narrative([], nid="top"), narrative([target()], nid="reply-lane")]
    (a,) = run(MockLLM(), narratives)
    assert a.narrative_id == "reply-lane"


def test_stale_target_abstains(tmp_out):
    assert run(MockLLM(), [narrative([target(age=lanes.STALE_MINUTES + 1)])]) == []
    assert last_log(tmp_out)[0]["reason"] == "stale_target"


def test_one_reply_per_conversation_root(tmp_out):
    n = narrative([target(), target(eph=900.0)])
    assert len(lanes.reply_targets([n])) == 1


def test_fastest_target_first():
    n = narrative([target(url=X_URL, eph=100.0), target(url=X_URL2, author="WNBA", eph=900.0)])
    assert [t["url"] for t in lanes.reply_targets([n])] == [X_URL2, X_URL]


def test_reply_budget_caps_the_run(tmp_out):
    n = narrative([target(url=X_URL, eph=900.0), target(url=X_URL2, author="WNBA", eph=100.0)])
    actions = run(MockLLM(), [n], budget=lanes.Budget(run_caps={"reply": 1}))
    assert [a.target_url for a in actions] == [X_URL]
    assert [d["reason"] for d in last_log(tmp_out)[:2]] == ["", "budget_exhausted"]


def test_daily_budget_counts_earlier_logs(tmp_out):
    (tmp_out / "decisions-20261015-100000.json").write_text(json.dumps({"decisions": [
        {"tool": "reply", "action_id": "a1"}, {"tool": "reply", "action_id": "a2"}, {"tool": "abstain", "action_id": None},
    ]}), encoding="utf-8")
    b = lanes.Budget.from_logs(today="20261015")
    assert b.used_today == Counter({"reply": 2})
    assert b.left("reply") == min(lanes.RUN_CAPS["reply"], lanes.DAILY_CAPS["reply"] - 2)


def test_slop_drafts_abstain_with_gate_reasons(tmp_out, monkeypatch):
    llm = ScriptedLLM([{"tool": "reply", "target_url": X_URL, "text": "Ten straight wins — a testament to the work"}])
    assert run(llm, [narrative([target()])], FixedRanker(), monkeypatch=monkeypatch) == []
    d = last_log(tmp_out)[0]
    assert d["reason"] == "gate_rejected"
    assert any(r.startswith("em_dash") for r in d["candidates"][0]["gate"])


def test_model_abstaining_means_no_angle(tmp_out, monkeypatch):
    llm = ScriptedLLM([{"tool": "abstain", "reason": "no_angle", "detail": "nothing to add"}])
    assert run(llm, [narrative([target()])], FixedRanker(), monkeypatch=monkeypatch) == []
    assert last_log(tmp_out)[0]["reason"] == "no_angle"


def test_judge_below_strong_abstains_and_keeps_candidates(tmp_out, monkeypatch):
    assert run(MockLLM(), [narrative([target()])], FixedRanker(verdict="Revise"), monkeypatch=monkeypatch) == []
    d = last_log(tmp_out)[0]
    assert d["reason"] == "judge_rejected" and "Revise" in d["detail"]
    assert all("verdict" in c for c in d["candidates"])


def test_judge_that_flips_with_order_abstains(tmp_out, monkeypatch):
    assert run(MockLLM(), [narrative([target()])], FixedRanker(stable=False), monkeypatch=monkeypatch) == []
    assert "reversed" in last_log(tmp_out)[0]["detail"]


# ---- post lane


def chorus_action(type_, text="", handle=None, url=None, mind="meme_lord"):
    return Action(type=type_, text=text, mind=mind, narrative_id="finals", target_handle=handle, target_url=url)


def test_post_lane_rejects_replies_and_unoffered_handles(tmp_out):
    n = narrative([], signal_urls=["https://x.com/IndianaFever/status/1"])
    chorus = [chorus_action("reply", "dream in 4", url=X_URL), chorus_action("follow", handle="randomfan"),
              chorus_action("follow", handle="@IndianaFever")]
    actions = run(MockLLM(), [n], chorus=chorus)
    assert [(a.type, a.target_handle) for a in actions] == [("follow", "indianafever")]
    rejected = next(d for d in last_log(tmp_out) if d["opportunity"] == "chorus")["candidates"]
    assert "not allowed in the post lane" in rejected[0]["schema_error"]
    assert "'randomfan' was not offered" in rejected[1]["schema_error"]


def test_post_lane_ships_at_most_one_post(tmp_out):
    chorus = [chorus_action("post", "the finals start saturday"), chorus_action("post", "game 1 on nbc", mind="stat_nerd")]
    actions = run(MockLLM(), [narrative([])], chorus=chorus)
    assert [a.type for a in actions] == ["post"] and actions[0].lane == "post"


def test_no_post_means_a_question_for_a_person(tmp_out):
    chorus = [chorus_action("post", "A testament to resilience — truly")]
    assert run(MockLLM(), [narrative([])], chorus=chorus) == []
    ask = [d for d in last_log(tmp_out) if d["tool"] == "ask_human"]
    assert len(ask) == 1 and ask[0]["question"].startswith("Finals tip off Saturday.")
