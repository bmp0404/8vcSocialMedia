import pytest

from harness import config, memory
from harness.learning.kinds import EditPair, Episode, Outcome, save
from harness.learning.reviews import ReviewSummary, by_narrative, edit_diff, procedures, run_episodes, summarize
from harness.learning.store import FakeStore
from harness.models import Action


def ev(action_id: str, status: str, *, eid: int | None = None, note: str = "", edited: str | None = None,
       original: str = "Fever win tonight", at: str = "", reasons: list[str] | None = None) -> dict:
    """One row in the shape memory.review_history() returns."""
    return {"id": eid, "action_id": action_id, "run_id": 1, "status": status, "original_text": original,
            "edited_text": edited, "note": note, "reviewed_at": at, "reasons": reasons or []}


# summarize: final verdict and flips


@pytest.mark.parametrize("statuses, final, flips", [
    (["approved"], "approved", 0),
    (["approved", "killed"], "killed", 1),
    (["approved", "killed", "approved"], "approved", 2),
    (["killed", "killed"], "killed", 0),
    (["approved", "pending"], "", 0),
    (["approved", "pending", "killed"], "killed", 1),
    (["approved", "pending", "approved"], "approved", 0),
    (["pending"], "", 0),
])
def test_summarize_final_verdict_and_flips(statuses, final, flips):
    s = summarize([ev("a1", st) for st in statuses])["a1"]
    assert (s.final, s.flips) == (final, flips)


def test_summarize_keeps_actions_apart():
    out = summarize([ev("a1", "approved"), ev("a2", "killed"), ev("a1", "killed")])
    assert list(out) == ["a1", "a2"]
    assert (out["a1"].final, out["a1"].flips) == ("killed", 1)
    assert (out["a2"].final, out["a2"].flips) == ("killed", 0)


def test_summarize_dedupes_and_strips_notes_including_pending_ones():
    events = [ev("a1", "approved", note="  too long "), ev("a1", "killed", note="too long"), ev("a1", "killed", note=""),
              ev("a1", "pending", note="recheck the stat"), ev("a1", "approved", note="too long")]
    assert summarize(events)["a1"].notes == ["too long", "recheck the stat"]


def test_summarize_keeps_the_latest_verdicts_reasons_sorted_and_deduped():
    events = [ev("a1", "killed", reasons=["tone", "slop", "tone"])]
    assert summarize(events)["a1"].reasons == ["slop", "tone"]
    assert summarize(events + [ev("a1", "approved")])["a1"].reasons == []
    assert summarize(events + [ev("a1", "pending", reasons=["tone"])])["a1"].reasons == []
    assert summarize(events + [ev("a1", "killed", reasons=["off_message"])])["a1"].reasons == ["off_message"]


# summarize: edits


def test_summarize_records_edits_with_raw_text_and_dedupes_consecutive_repeats():
    events = [
        ev("a1", "approved", eid=1, edited="  Fever win tonight!  ", note="punchier", at="2026-10-01T10:00:00+00:00"),
        ev("a1", "approved", eid=2, edited="Fever  win\ttonight!", at="2026-10-01T11:00:00+00:00"),  # same, normalized
        ev("a1", "killed", eid=3, edited="Fever lose", at="2026-10-01T12:00:00+00:00"),
        ev("a1", "approved", eid=4, edited="Fever win tonight!", at="2026-10-01T13:00:00+00:00"),  # not consecutive
    ]
    assert summarize(events)["a1"].edits == [
        {"event_id": 1, "status": "approved", "original": "Fever win tonight", "edited": "  Fever win tonight!  ",
         "note": "punchier", "at": "2026-10-01T10:00:00+00:00"},
        {"event_id": 3, "status": "killed", "original": "Fever win tonight", "edited": "Fever lose", "note": "",
         "at": "2026-10-01T12:00:00+00:00"},
        {"event_id": 4, "status": "approved", "original": "Fever win tonight", "edited": "Fever win tonight!",
         "note": "", "at": "2026-10-01T13:00:00+00:00"},
    ]


def test_summarize_ignores_an_edit_equal_to_the_original_after_whitespace_normalization():
    s = summarize([ev("a1", "approved", original="Fever  win tonight", edited=" Fever win\n tonight ")])["a1"]
    assert s.edits == []
    assert s.final == "approved"


def test_summarize_ignores_edit_text_on_a_pending_event():
    assert summarize([ev("a1", "pending", edited="Something else")])["a1"].edits == []


def test_summarize_treats_missing_original_and_timestamps_as_empty():
    events = [{"id": None, "action_id": "a1", "status": "killed", "original_text": None, "edited_text": "New text",
               "note": None, "reviewed_at": None}]
    assert summarize(events)["a1"] == ReviewSummary(
        action_id="a1", final="killed", flips=0, notes=[], reasons=[],
        edits=[{"event_id": None, "status": "killed", "original": "", "edited": "New text", "note": "", "at": ""}],
    )


# edit_diff


def test_edit_diff_reports_word_tag_mention_and_verify_changes():
    original = "Clark drops 30 tonight #WNBA @CaitlinClark22"
    edited = "Clark drops [VERIFY: points] tonight #Fever @IndianaFever"
    # Matching blocks are "Clark drops" and "tonight": 3 matched words of 6 + 7, ratio 6/13.
    assert edit_diff(original, edited) == {
        "chars_before": 44,
        "chars_after": 57,
        "similarity": 0.462,
        "words_removed": ["30", "#WNBA", "@CaitlinClark22"],
        "words_added": ["[VERIFY:", "points]", "#Fever", "@IndianaFever"],
        "hashtags_removed": ["#WNBA"],
        "hashtags_added": ["#Fever"],
        "mentions_removed": ["@CaitlinClark22"],
        "mentions_added": ["@IndianaFever"],
        "verify_added": True,
        "verify_removed": False,
    }


def test_edit_diff_flags_a_removed_verify_marker():
    d = edit_diff("Clark scored [VERIFY: 30] points", "Clark scored 30 points")
    assert (d["verify_added"], d["verify_removed"]) == (False, True)
    assert d["words_removed"] == ["[VERIFY:", "30]"]
    assert d["words_added"] == ["30"]


def test_edit_diff_of_identical_text():
    d = edit_diff("Fever win #WNBA", "Fever win #WNBA")
    assert (d["similarity"], d["words_removed"], d["words_added"]) == (1.0, [], [])
    assert (d["hashtags_removed"], d["hashtags_added"]) == ([], [])


# procedures

PROC_ROWS = [
    {"action": {"id": "a1", "mind": "Hype Mind", "narrative_id": "n1", "type": "post"}},
    {"action": {"id": "a2", "mind": "Hype Mind", "narrative_id": "n2", "type": "reply"}},
    {"action": {"id": "a3", "mind": "stats", "narrative_id": "n1", "type": "post"}},
]
PROC_EVENTS = [
    ev("a2", "approved", eid=1, original="Reply draft", edited="Reply edited", at="2026-10-02T09:00:00+00:00"),
    ev("a1", "approved", eid=2, original="Post draft", edited="Post edited", note="cut the hype",
       at="2026-10-03T09:00:00+00:00"),
    ev("a3", "killed", eid=3, original="Stat draft", edited="Stat edited", at="2026-10-01T09:00:00+00:00"),
    ev("a9", "approved", eid=4, original="Orphan draft", edited="Orphan edited", at="2026-10-04T09:00:00+00:00"),
    ev("a1", "killed", eid=5, original="Post draft", at="2026-10-05T09:00:00+00:00"),
]


def test_procedures_group_edits_by_mind_with_stable_ids_and_dates():
    procs = procedures(summarize(PROC_EVENTS), PROC_ROWS)
    assert [(p.id, p.mind, p.created, p.updated) for p in procs] == [
        ("procedure-review-edits-hype-mind", "Hype Mind", "2026-10-02", "2026-10-03"),
        ("procedure-review-edits-stats", "stats", "2026-10-01", "2026-10-01"),
        ("procedure-review-edits-unknown", "unknown", "2026-10-04", "2026-10-04"),
    ]
    hype = procs[0]
    assert hype.pairs == [
        EditPair(action_id="a2", original="Reply draft", edited="Reply edited", verdict="approved", note="",
                 narrative_id="n2", action_type="reply", diff=edit_diff("Reply draft", "Reply edited"),
                 at="2026-10-02T09:00:00+00:00"),
        EditPair(action_id="a1", original="Post draft", edited="Post edited", verdict="approved", note="cut the hype",
                 narrative_id="n1", action_type="post", diff=edit_diff("Post draft", "Post edited"),
                 at="2026-10-03T09:00:00+00:00"),
    ]
    assert [(p.action_id, p.verdict, p.narrative_id, p.action_type) for p in procs[1].pairs] == [
        ("a3", "killed", "n1", "post")]
    assert [(p.action_id, p.narrative_id, p.action_type) for p in procs[2].pairs] == [("a9", "", "")]


def test_procedures_save_with_edit_evidence():
    store = FakeStore()
    hype = procedures(summarize(PROC_EVENTS), PROC_ROWS)[0]
    meta = store.items[save(store, hype)].meta
    assert meta["evidence"] == ["ep-a2", "ep-a1"]
    assert (meta["title"], meta["aliases"], meta["created"], meta["updated"]) == (
        "How reviewers edit Hype Mind drafts", ["Hype Mind"], "2026-10-02", "2026-10-03")


def test_procedures_skip_actions_without_edits():
    assert procedures(summarize([ev("a1", "approved"), ev("a2", "killed")]), PROC_ROWS) == []


# run_episodes


def run_row(aid: str, *, vetoed: bool = False, predicted_score: float = 0.5, critique: str = "",
            metrics: dict | None = None, recorded_at: str | None = None, narrative_id: str = "n1",
            run_id: int = 3) -> dict:
    """One row in the shape memory.run_log() returns."""
    return {
        "run_id": run_id,
        "started_at": "2026-10-01T12:00:00+00:00",
        "provider": "mock",
        "action": {"id": aid, "type": "post", "text": f"draft {aid}", "mind": "hype", "narrative_id": narrative_id,
                   "rationale": "why", "target_url": None, "target_handle": None,
                   "predicted_score": predicted_score, "vetoed": vetoed, "critique": critique},
        "review": None,
        "metrics": metrics,
        "recorded_at": recorded_at,
    }


RUN_ROWS = [
    run_row("a1"),
    run_row("a2"),
    run_row("a3"),
    run_row("a4", vetoed=True, predicted_score=0.2, critique="invents a stat"),
    run_row("a5", metrics={"likes": 3}, recorded_at="2026-10-04T00:00:00+00:00"),
    run_row("a6", metrics={"likes": 50, "outcome": "success"}, recorded_at="2026-10-05T00:00:00+00:00"),
    run_row("a7", metrics={"likes": 1, "outcome": "meh"}, recorded_at="2026-10-06T00:00:00+00:00"),
]
RUN_EVENTS = [
    ev("a1", "approved", original="draft a1"),
    ev("a2", "approved", original="draft a2", edited="draft a2, better"),
    ev("a3", "killed", original="draft a3", note="off brand", reasons=["tone"]),
    ev("a6", "killed", original="draft a6"),
]


def episodes_by_id() -> dict[str, Episode]:
    return {ep.id: ep for ep in run_episodes(RUN_ROWS, summarize(RUN_EVENTS), brand="fanacct")}


def test_run_episodes_builds_the_full_episode_for_an_approved_draft():
    rows = [run_row("a1"), run_row("b1", narrative_id="n2")]
    events = [ev("a1", "approved", original="draft a1")]
    ep = run_episodes(rows, summarize(events), brand="fanacct")[0]
    assert ep == Episode(
        id="ep-a1",
        source="run",
        author_username="fanacct",
        text="draft a1",
        action_type="post",
        created_at="2026-10-01T12:00:00+00:00",
        loop_id="run-3",
        url=None,
        features={"mind": "hype", "predicted_score": 0.5, "vetoed": False, "edited": False, "flips": 0,
                  "media": "none"},
        observation={"narrative_id": "n1", "target_url": None, "target_handle": None, "rationale": "why"},
        decision={"mind": "hype", "provider": "mock", "draft_meta": {}, "siblings": [], "chosen_by_bot": None},
        outcomes=[Outcome(label="", source="eval", raw="0.5"),
                  Outcome(label="success", source="review", raw="approved")],
    )


def test_run_episodes_lists_siblings_from_the_same_run_and_narrative():
    rows = [run_row("a1"), run_row("a2", predicted_score=0.7), run_row("a3", vetoed=True, predicted_score=0.1),
            run_row("b1", narrative_id="n2"), run_row("c1", run_id=4)]
    eps = {ep.id: ep for ep in run_episodes(rows, {})}
    assert eps["ep-a1"].decision["siblings"] == [{"id": "a2", "predicted_score": 0.7, "vetoed": False},
                                                 {"id": "a3", "predicted_score": 0.1, "vetoed": True}]
    assert eps["ep-a3"].decision["siblings"] == [{"id": "a1", "predicted_score": 0.5, "vetoed": False},
                                                 {"id": "a2", "predicted_score": 0.7, "vetoed": False}]
    assert eps["ep-b1"].decision["siblings"] == []
    assert eps["ep-c1"].decision["siblings"] == []


def test_run_episodes_labels_review_verdicts():
    eps = episodes_by_id()
    assert {eid: eps[eid].label() for eid in ("ep-a1", "ep-a2", "ep-a3")} == {
        "ep-a1": "success", "ep-a2": "success", "ep-a3": "failure"}
    assert eps["ep-a2"].outcomes[1] == Outcome(label="success", source="review", raw="approved (edited)")
    assert eps["ep-a2"].features["edited"] is True
    assert eps["ep-a3"].outcomes[1] == Outcome(label="failure", source="review", raw="killed", note="off brand",
                                               reasons=["tone"])


def test_run_episodes_gives_a_vetoed_draft_an_eval_failure():
    ep = episodes_by_id()["ep-a4"]
    assert ep.outcomes == [Outcome(label="failure", source="eval", raw="0.2", note="invents a stat")]
    assert ep.label() == "failure"
    assert ep.features["vetoed"] is True


def test_run_episodes_labels_metrics_only_when_they_name_an_outcome():
    eps = episodes_by_id()
    assert eps["ep-a5"].outcomes[-1] == Outcome(label="", source="metrics", metrics={"likes": 3},
                                                at="2026-10-04T00:00:00+00:00")
    assert eps["ep-a5"].label() == ""
    assert eps["ep-a6"].outcomes[-1] == Outcome(label="success", source="metrics", metrics={"likes": 50},
                                                at="2026-10-05T00:00:00+00:00")
    assert eps["ep-a7"].outcomes[-1] == Outcome(label="", source="metrics", metrics={"likes": 1},
                                                at="2026-10-06T00:00:00+00:00")
    assert eps["ep-a7"].label() == ""


def test_run_episodes_metrics_outrank_a_review_verdict():
    ep = episodes_by_id()["ep-a6"]
    assert [o.label for o in ep.outcomes] == ["", "failure", "success"]
    assert ep.outcome().source == "metrics"
    assert ep.label() == "success"


def test_run_episodes_leaves_the_input_metrics_alone():
    run_episodes(RUN_ROWS, summarize(RUN_EVENTS))
    assert RUN_ROWS[5]["metrics"] == {"likes": 50, "outcome": "success"}


def test_run_episodes_default_brand_and_save():
    ep = run_episodes([run_row("a1")], summarize([ev("a1", "killed")]))[0]
    assert ep.author_username == "brand"
    store = FakeStore()
    meta = store.items[save(store, ep)].meta
    assert meta["tags"] == ["run", "failure"]
    assert meta["aliases"] == ["brand", "a1", "none"]


# by_narrative


def test_by_narrative_uses_the_narrative_then_the_loop():
    with_narrative, without = run_episodes([run_row("a1", narrative_id="n7"), run_row("a2", narrative_id="")], {})
    assert by_narrative(with_narrative) == "n7"
    assert by_narrative(without) == "run-3"


# memory.py review_events


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    return tmp_path / "h.db"


def save_two_actions() -> int:
    return memory.save_run([
        Action(type="post", text="Fever win tonight", mind="hype", narrative_id="n1", predicted_score=0.9, id="a1"),
        Action(type="reply", text="Second draft", mind="stats", narrative_id="n2", predicted_score=0.5, id="a2"),
    ])


def without_time(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "reviewed_at"} for r in rows]


def test_review_history_returns_every_event_in_order_with_the_original_text(db):
    run_id = save_two_actions()
    memory.set_review("a1", "approved")
    memory.set_review("a1", "killed", note="off brand", reasons=["tone", "off_message", "tone"])
    memory.set_review("a2", "approved", text="Second draft, edited")
    memory.set_review("a1", "approved", text="Fever win tonight!", note="tightened")
    history = memory.review_history()
    assert without_time(history) == [
        {"id": 1, "action_id": "a1", "run_id": run_id, "status": "approved", "original_text": "Fever win tonight",
         "edited_text": None, "note": "", "reasons": []},
        {"id": 2, "action_id": "a1", "run_id": run_id, "status": "killed", "original_text": "Fever win tonight",
         "edited_text": None, "note": "off brand", "reasons": ["off_message", "tone"]},
        {"id": 3, "action_id": "a2", "run_id": run_id, "status": "approved", "original_text": "Second draft",
         "edited_text": "Second draft, edited", "note": "", "reasons": []},
        {"id": 4, "action_id": "a1", "run_id": run_id, "status": "approved", "original_text": "Fever win tonight",
         "edited_text": "Fever win tonight!", "note": "tightened", "reasons": []},
    ]
    assert all(isinstance(r["reviewed_at"], str) and r["reviewed_at"] for r in history)

    reviews = {a["id"]: a["review"] for a in memory.run_actions(run_id)}
    assert {aid: (r["status"], r["text"], r["note"]) for aid, r in reviews.items()} == {
        "a1": ("approved", "Fever win tonight!", "tightened"),
        "a2": ("approved", "Second draft, edited", ""),
    }


def test_pending_clears_the_latest_verdict_but_stays_in_history(db):
    run_id = save_two_actions()
    memory.set_review("a1", "killed", note="off brand")
    memory.set_review("a1", "pending")
    assert [(r["status"], r["note"]) for r in memory.review_history()] == [("killed", "off brand"), ("pending", "")]
    a1 = next(a for a in memory.run_actions(run_id) if a["id"] == "a1")
    assert a1["review"] == {"status": "pending", "text": None, "note": "", "reviewed_at": None}
    assert summarize(memory.review_history())["a1"].final == ""


def test_review_history_includes_legacy_verdicts_without_events(db):
    run_id = save_two_actions()
    with memory.connect() as conn:
        conn.execute("INSERT INTO reviews VALUES (?, ?, ?, ?, ?)",
                     ("a2", "killed", "Old edit", "old note", "2026-09-01T00:00:00+00:00"))
    memory.set_review("a1", "approved")
    history = memory.review_history()
    assert history[0] == {"id": None, "action_id": "a2", "run_id": run_id, "status": "killed",
                          "original_text": "Second draft", "edited_text": "Old edit", "note": "old note",
                          "reviewed_at": "2026-09-01T00:00:00+00:00", "reasons": []}
    assert [(r["id"], r["action_id"]) for r in history] == [(None, "a2"), (1, "a1")]


def test_set_review_rejects_an_unknown_status_and_action(db):
    save_two_actions()
    with pytest.raises(ValueError, match="status must be one of"):
        memory.set_review("a1", "maybe")
    with pytest.raises(ValueError, match=r"unknown review reasons \['boring'\]"):
        memory.set_review("a1", "killed", reasons=["tone", "boring"])
    with pytest.raises(KeyError):
        memory.set_review("nope", "approved")
    assert memory.review_history() == []


def test_a_draft_nothing_scored_has_no_eval_number_in_its_episode():
    rows = [run_row("a1", predicted_score=None), run_row("a2", predicted_score=0.7)]
    eps = {e.id: e for e in run_episodes(rows, {}, brand="_sgu_")}
    assert eps["ep-a1"].outcomes[0].raw == "" and eps["ep-a1"].features["predicted_score"] is None
    assert eps["ep-a2"].outcomes[0].raw == "0.7"
    assert eps["ep-a2"].decision["siblings"] == [{"id": "a1", "predicted_score": None, "vetoed": False}]
