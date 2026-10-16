"""qualify's ranker: the profile's post ranker orders only the post lane's posts and quotes, replies keep decide's
order, wnba names no ranker, and the panel comes back only through the qualify.ranker knob."""

import argparse
import json

import pytest

from harness import config, qualify as q
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
def devtools_with_the_default_ranker(monkeypatch):
    """devtools names post_ridge; the wnba tests switch back with use_profile."""
    monkeypatch.delenv(q.RANKER_ENV, raising=False)
    monkeypatch.setenv("HARNESS_PROFILE", "devtools")


def refuse_to_load(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a wnba round loaded a post ranker")

    monkeypatch.setattr(post_ranker, "load", refuse)
    monkeypatch.setattr(post_ranker, "load_named", refuse)


def reply(text, aid):
    return Action(type="reply", text=text, mind="reply_lane", narrative_id="n", lane="reply", id=aid,
                  target_url=f"https://x.com/a/status/{aid}")


def post(text, aid, kind="post"):
    return Action(type=kind, text=text, mind="caption", narrative_id="n", lane="post", id=aid)


def follow(aid):
    return Action(type="follow", text="", mind="chorus", narrative_id="n", lane="post", id=aid, target_handle="WNBA")


def test_devtools_ranks_with_post_ridge_by_default_and_spends_one_critic_call_and_no_panel_call():
    llm = Recorder()
    actions = [reply(HIGH, "r1"), post(LOW, "p1"), post(HIGH, "q1", kind="quote"), follow("f1")]
    kept = q.qualify(llm, "MISSION", actions)
    assert llm.calls == [CRITIC]
    model = post_ranker.load_named("post_ridge")
    by_id = {a.id: a for a in kept}
    for aid, text in (("p1", LOW), ("q1", HIGH)):
        meta = by_id[aid].draft_meta["ranker"]
        assert meta == {"name": "post_ridge", "sha256": model.sha256, "score": round(model.score(text), 4),
                        "percentile": model.percentile(model.score(text))}
        assert by_id[aid].predicted_score == meta["percentile"] and by_id[aid].panel == {}
    for aid in ("r1", "f1"):  # a reply with the highest-scoring text still gets no score
        assert "ranker" not in by_id[aid].draft_meta and by_id[aid].predicted_score is None and by_id[aid].panel == {}


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
        with pytest.raises(ValueError, match=r"HARNESS_QUALIFY_RANKER='pannel'; expected one of \['learned', 'panel'\]"):
            q.qualify(llm, "MISSION", [post(LOW, "p1")])
    else:
        with pytest.raises(ValueError, match="ranker 'ridge'"):
            q.qualify(llm, "MISSION", [post(LOW, "p1")], ranker="ridge")
    assert llm.calls == []


def test_a_model_that_fails_its_hash_check_stops_qualify_before_the_critic_call(monkeypatch):
    def broken():
        raise post_ranker.RankerError("post_ridge.json.gz hashes to x, but the manifest records y")

    monkeypatch.setattr(post_ranker, "load_named", lambda name: broken())
    llm = Recorder()
    with pytest.raises(post_ranker.RankerError, match="hashes to"):
        q.qualify(llm, "MISSION", [post(LOW, "p1")])
    assert llm.calls == []


def test_the_knob_is_registered_with_the_production_default():
    knob = knobs.BY_PATH["qualify.ranker"]
    assert knob.default == q.RANKERS[0] == q.ranker_choice() == "learned"
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

    monkeypatch.setattr(post_ranker, "load_named", lambda name: broken())
    monkeypatch.setattr(cli, "observe", lambda *a, **k: pytest.fail("observe ran before the ranker was checked"))
    with pytest.raises(post_ranker.RankerError, match="hashes to"):
        cli.run(RUN)
    monkeypatch.setenv(q.RANKER_ENV, "panel")  # the panel needs no model file
    assert q.ready() == "the audience panel"


def test_unscored_means_none_not_zero():
    assert Action(type="reply", text="x", mind="m", narrative_id="n").predicted_score is None
    stale = post(LOW, "p1")
    stale.predicted_score = 0.4  # a panel score from before; a wnba round must clear it, not keep it
    with config.use_profile("wnba"):
        (kept,) = q.qualify(Recorder(), "MISSION", [stale])
    assert kept.predicted_score is None


def test_a_round_is_saved_in_qualifys_order_and_the_review_list_keeps_it(tmp_path, monkeypatch):
    """The run log, the hand-off and the review queue read memory in saved order, so that order must be qualify's."""
    from harness import cli, memory
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setenv("MEMSTORE_PATH", str(tmp_path / "store"))
    batch = [reply("one", "zz"), post(LOW, "p_low"), reply("two", "aa"), post(HIGH, "p_high"), reply("vetoed", "vv"),
             follow("f1")]
    monkeypatch.setattr(cli, "observe", lambda *a, **k: ["a signal"])
    monkeypatch.setattr(cli, "orient", lambda *a, **k: [])
    monkeypatch.setattr(cli, "decide", lambda *a, **k: batch)
    monkeypatch.setattr(cli, "qualify", lambda llm, mission, actions, ranker=None: q.qualify(
        Recorder(veto={"vv"}), mission, actions, ranker=ranker))
    cli.run(RUN)
    (run,) = memory.list_runs()
    saved = [a["id"] for a in memory.run_actions(run["id"])]
    assert saved == ["zz", "p_high", "aa", "p_low", "f1", "vv"]  # posts swapped places; replies and the follow stayed
    assert [a["predicted_score"] is None for a in memory.run_actions(run["id"])] == [True, False, True, False, True, True]


def test_each_profile_names_its_post_ranker_in_qualify_toml():
    assert q.profile_post_ranker() == "post_ridge" and q.ready() == "post_ridge"
    with config.use_profile("wnba"):
        assert q.profile_post_ranker() == "none" and q.profile_ranker() is None
        assert q.ready() == "decide's order: profile wnba names no post ranker"


def test_a_wnba_round_never_loads_the_ridge_and_scores_nothing(monkeypatch):
    refuse_to_load(monkeypatch)
    actions = [reply(HIGH, "r1"), post(LOW, "p_low"), post(HIGH, "p_high")]
    with config.use_profile("wnba"):
        kept = q.qualify(Recorder(), "MISSION", actions)
    assert [a.id for a in kept] == ["r1", "p_low", "p_high"]  # decide's order
    assert all("ranker" not in a.draft_meta and not a.panel for a in kept)


def test_a_wnba_mock_run_never_loads_the_ridge_and_prints_no_score(monkeypatch, tmp_path, capsys):
    from harness import cli
    refuse_to_load(monkeypatch)
    monkeypatch.setenv("HARNESS_PROFILE", "wnba")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setenv("MEMSTORE_PATH", str(tmp_path / "store"))
    cli.run(RUN)
    out = capsys.readouterr().out
    assert "qualify orders the post lane by decide's order: profile wnba names no post ranker" in out
    assert " 1. [ -- ] quote" in out
    (path,) = (tmp_path / "out").glob("recommendations-*.json")
    assert all("ranker" not in a["draft_meta"] for a in json.loads(path.read_text(encoding="utf-8"))["recommendations"])


@pytest.mark.parametrize("body, match", [
    ('post_ranker = ""', "exactly one key"), ('ranker = "post_ridge"', "exactly one key"),
    ('post_ranker = "post_ridge"\nextra = 1', "exactly one key"),
])
def test_a_malformed_qualify_toml_is_refused(monkeypatch, tmp_path, body, match):
    profile = tmp_path / "brand"
    profile.mkdir()
    (profile / "qualify.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path)
    monkeypatch.setenv("HARNESS_PROFILE", "brand")
    with pytest.raises(ValueError, match=match):
        q.profile_post_ranker()


def test_an_unknown_model_name_lists_the_ones_there():
    with pytest.raises(post_ranker.RankerError, match=r"no post ranker named 'wnba_ridge' .*\['post_ridge'\]"):
        post_ranker.load_named("wnba_ridge")
    assert post_ranker.load_named("post_ridge").name == "post_ridge"
