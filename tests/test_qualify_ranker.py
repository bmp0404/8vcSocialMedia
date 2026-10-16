"""qualify's ranker: the post ridge orders only the post lane's posts and quotes, replies keep decide's order,
and the panel comes back only through the qualify.ranker knob."""

import argparse

import pytest

from harness import qualify as q
from harness.iterate import knobs
from harness.llm import LLM
from harness.models import Action
from harness.rank import post_ranker

HIGH = "Claude Opus 5.5 is out today, and it works with your MCP servers"
LOW = "Come see us at our booth during tech week"
CRITIC = q.CRITIC_SYSTEM.split("\n")[0]
PANEL = q.PANEL_SYSTEM.split("\n")[0]


class Recorder(LLM):
    """Counts calls by prompt, vetoes the ids in `veto`, and gives panel scores from `panel_scores`."""

    def __init__(self, veto=(), panel_scores=None):
        self.calls, self.veto, self.panel_scores = [], set(veto), panel_scores or {}

    def complete_json(self, system, prompt, mock=None):
        head = system.split("\n")[0]
        self.calls.append(head)
        out = mock()
        if head == CRITIC:
            for aid in self.veto:
                out["verdicts"][aid] = {"veto": True, "critique": "no"}
        if head == PANEL:
            out = {"scores": {aid: {p: s for p in ("fan",)} for aid, s in self.panel_scores.items()}}
        return out


@pytest.fixture(autouse=True)
def default_ranker(monkeypatch):
    monkeypatch.delenv(q.RANKER_ENV, raising=False)


def reply(text, aid):
    return Action(type="reply", text=text, mind="reply_lane", narrative_id="n", lane="reply", id=aid,
                  target_url=f"https://x.com/a/status/{aid}")


def post(text, aid, kind="post"):
    return Action(type=kind, text=text, mind="caption", narrative_id="n", lane="post", id=aid)


def follow(aid):
    return Action(type="follow", text="", mind="chorus", narrative_id="n", lane="post", id=aid, target_handle="WNBA")


def test_the_ridge_is_the_default_and_spends_one_critic_call_and_no_panel_call():
    llm = Recorder()
    actions = [reply(HIGH, "r1"), post(LOW, "p1"), post(HIGH, "q1", kind="quote"), follow("f1")]
    kept = q.qualify(llm, "MISSION", actions)
    assert llm.calls == [CRITIC]
    model = post_ranker.load()
    by_id = {a.id: a for a in kept}
    for aid, text in (("p1", LOW), ("q1", HIGH)):
        meta = by_id[aid].draft_meta["ranker"]
        assert meta == {"name": "post_ridge", "sha256": model.sha256, "score": round(model.score(text), 4),
                        "percentile": model.percentile(model.score(text))}
        assert by_id[aid].predicted_score == meta["percentile"] and by_id[aid].panel == {}
    for aid in ("r1", "f1"):  # a reply with the highest-scoring text still gets no score
        assert "ranker" not in by_id[aid].draft_meta and by_id[aid].predicted_score == 0.0 and by_id[aid].panel == {}


def test_posts_reorder_among_their_own_places_and_everything_else_keeps_its_place():
    actions = [reply(LOW, "r1"), post(LOW, "p_low"), reply(HIGH, "r2"), post(HIGH, "p_high"), follow("f1"),
               reply("third reply", "r3")]
    kept = q.qualify(Recorder(), "MISSION", actions)
    assert [a.id for a in kept] == ["r1", "p_high", "r2", "p_low", "f1", "r3"]


def test_vetoed_drafts_drop_out_and_the_rest_keep_their_order():
    actions = [reply("one", "r1"), reply("two", "r2"), post(LOW, "p1"), post(HIGH, "p2"), reply("three", "r3")]
    kept = q.qualify(Recorder(veto={"r2", "p2"}), "MISSION", actions)
    assert [a.id for a in kept] == ["r1", "p1", "r3"]
    assert {a.id for a in actions if a.vetoed} == {"r2", "p2"}


def test_the_panel_knob_scores_every_draft_in_two_orders_and_sorts_them_all(monkeypatch):
    monkeypatch.setenv(q.RANKER_ENV, "panel")
    llm = Recorder(panel_scores={"r1": 0.2, "p1": 0.9, "r2": 0.6})
    kept = q.qualify(llm, "MISSION", [reply("one", "r1"), post(LOW, "p1"), reply("two", "r2")], panel={"fan": "a fan"})
    assert sorted(llm.calls) == sorted([PANEL, PANEL, CRITIC])
    assert [(a.id, a.predicted_score) for a in kept] == [("p1", 0.9), ("r2", 0.6), ("r1", 0.2)]
    assert all("ranker" not in a.draft_meta for a in kept)


@pytest.mark.parametrize("how", ["env", "argument"])
def test_an_unknown_ranker_is_refused_before_any_call(monkeypatch, how):
    llm = Recorder()
    if how == "env":
        monkeypatch.setenv(q.RANKER_ENV, "pannel")
        with pytest.raises(ValueError, match=r"HARNESS_QUALIFY_RANKER='pannel'; expected one of \['post_ridge', 'panel'\]"):
            q.qualify(llm, "MISSION", [post(LOW, "p1")])
    else:
        with pytest.raises(ValueError, match="ranker 'ridge'"):
            q.qualify(llm, "MISSION", [post(LOW, "p1")], ranker="ridge")
    assert llm.calls == []


def test_a_model_that_fails_its_hash_check_stops_qualify_before_the_critic_call(monkeypatch):
    def broken():
        raise post_ranker.RankerError("post_ridge.json.gz hashes to x, but the manifest records y")

    monkeypatch.setattr(post_ranker, "load", broken)
    llm = Recorder()
    with pytest.raises(post_ranker.RankerError, match="hashes to"):
        q.qualify(llm, "MISSION", [post(LOW, "p1")])
    assert llm.calls == []


def test_the_knob_is_registered_with_the_production_default():
    knob = knobs.BY_PATH["qualify.ranker"]
    assert knob.default == q.RANKERS[0] == q.ranker_choice() == "post_ridge"
    assert knob.choices == q.RANKERS and knob.stage == "qualify" and "HARNESS_QUALIFY_RANKER" in knob.reader


RUN = argparse.Namespace(provider="mock", sources="sample", narratives=2, per_mind=1, top=10, retries=0)


def test_a_run_with_an_unknown_ranker_stops_before_observe(monkeypatch):
    from harness import cli
    monkeypatch.setenv(q.RANKER_ENV, "pannel")
    monkeypatch.setattr(cli, "observe", lambda *a, **k: pytest.fail("observe ran before the ranker was checked"))
    with pytest.raises(ValueError, match="pannel"):
        cli.run(RUN)


def test_a_run_whose_model_fails_its_hash_check_stops_before_observe(monkeypatch):
    from harness import cli

    def broken():
        raise post_ranker.RankerError("post_ridge.json.gz hashes to x, but the manifest records y")

    monkeypatch.setattr(post_ranker, "load", broken)
    monkeypatch.setattr(cli, "observe", lambda *a, **k: pytest.fail("observe ran before the ranker was checked"))
    with pytest.raises(post_ranker.RankerError, match="hashes to"):
        cli.run(RUN)
    monkeypatch.setenv(q.RANKER_ENV, "panel")  # the panel needs no model file
    assert q.ready() == "panel"


def test_the_review_list_keeps_unscored_replies_in_the_order_the_run_saved_them(tmp_path, monkeypatch):
    from harness import config, memory
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    actions = [reply("one", "zz"), reply("two", "aa"), post(LOW, "p1"), reply("three", "mm"), follow("f1")]
    q.qualify(Recorder(), "MISSION", actions)
    run_id = memory.save_run(actions)
    assert [a["id"] for a in memory.run_actions(run_id)] == ["p1", "zz", "aa", "mm", "f1"]
