from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from harness.integration import handoff, measure
from harness.llm import MockLLM

URL = "https://x.com/valkyries/status/111"
NEWS = "https://www.espn.com/wnba/story/_/id/1"


def rec(**kw):
    base = {
        "narratives": [{"id": "n1", "title": "Finals", "summary": "Dream vs Valkyries", "signal_urls": [NEWS],
                        "reply_targets": [{"url": URL, "author": "valkyries", "text": "Ballhalla awaits."}]}],
        "signals": [{"source": "espn_news", "url": NEWS, "text": "Game 1 is Saturday at Chase Center"}],
        "qualify_calls": [{"in": ["a1", "a2"], "kept": ["a1"]}],
        "decide_calls": [{}],
        "recommendations_file": None,
    }
    return base | kw


def gate(rule):
    return [{"rule": rule, "detail": "x", "start": 0, "end": 1}]


DECISIONS = [
    {"lane": "reply", "opportunity": "https://x.com/a/status/9", "tool": "abstain", "reason": "stale_target",
     "detail": "81 min old", "question": "", "action_id": None, "candidates": []},
    {"lane": "reply", "opportunity": URL, "tool": "reply", "reason": "", "detail": "", "question": "", "action_id": "a1",
     "candidates": [
         {"text": "good one", "gate": [], "veto": {"passed": True, "why": ""}},
         {"text": "bad — one", "gate": gate("em_dash")},
         {"text": "vetoed one", "gate": [], "veto": {"passed": False, "why": "reads as AI"}},
         {"text": "wrong target", "schema_error": "reply.target_url not offered"},
     ]},
    {"lane": "post", "opportunity": NEWS, "tool": "post", "reason": "", "detail": "caption of a real source",
     "question": "", "action_id": "a2", "candidates": [{"text": "short caption", "gate": [], "veto": {"passed": True}}]},
    {"lane": "post", "opportunity": "valkyries", "tool": "follow", "reason": "", "detail": "", "question": "",
     "action_id": "a3", "candidates": []},
    {"lane": "post", "opportunity": "n1", "tool": "ask_human", "reason": "", "detail": "", "question": "Got a take?",
     "action_id": None, "candidates": [{"mind": "meme_lord", "text": "angle", "gate": []}]},
]
ACTIONS = {
    "a1": {"id": "a1", "type": "reply", "text": "good one", "target_url": URL, "narrative_id": "n1", "critique": ""},
    "a2": {"id": "a2", "type": "post", "text": "short caption (via espn.com) " + NEWS, "target_url": None,
           "narrative_id": "n1", "critique": "too thin", "draft_meta": {"source_url": NEWS}},
    "a3": {"id": "a3", "type": "follow", "text": "", "target_url": None, "narrative_id": "", "critique": ""},
}


def test_funnel_counts_each_gate():
    f = measure.funnel(rec(), DECISIONS, ACTIONS)
    assert f["reply_lane"] == {"targets_offered": 2, "stale": 1, "planner_abstained": 0, "targets_drafted": 1,
                               "drafts": 4, "schema_rejected": 1, "gate_passed": 2, "veto_passed": 1, "shipped": 1}
    p = f["post_lane"]
    assert (p["caption_drafts"], p["caption_shipped"], p["follows_shipped"]) == (1, 1, 1)
    assert (p["chorus_angles_for_a_person"], p["questions_for_a_person"]) == (1, 1)
    assert (f["qualify"]["in"], f["qualify"]["critic_vetoed"], f["qualify"]["kept"]) == (2, 1, 1)
    assert f["qualify"]["vetoes"][0]["id"] == "a2"


def test_drafts_link_shipped_text_and_skip_angles():
    ds = measure.drafts(rec(), DECISIONS, ACTIONS)
    assert [d["text"] for d in ds] == ["good one", "bad — one", "vetoed one", "wrong target", "short caption"]
    by = {d["text"]: d for d in ds}
    assert by["good one"]["action_id"] == "a1" and by["good one"]["target_text"] == "Ballhalla awaits."
    assert by["short caption"]["action_id"] == "a2"  # matched despite the appended "(via ...)" credit
    assert by["bad — one"]["gate_rules"] == ["em_dash"] and not by["bad — one"]["gate_passed"]
    assert not by["wrong target"]["gate_passed"]
    assert by["vetoed one"]["veto_passed"] is False


def test_abstentions_count_by_lane_and_reason():
    a = measure.abstentions(DECISIONS)
    assert a["counts"] == {"reply:stale_target": 1}
    assert a["questions"] == ["Got a take?"]


def test_lineup_positions_are_distinct_and_repeatable():
    for seed in ("a", "b", "run22:text"):
        first = measure.lineup_positions(seed)
        assert first == measure.lineup_positions(seed)
        assert first[0] != first[1] and all(0 <= p < 5 for p in first)


def test_lineup_scores_a_catch_only_when_the_judge_names_the_draft_slot():
    class Picks(MockLLM):
        def __init__(self, picks):
            self.picks = list(picks)

        def complete_json(self, system, prompt, mock=None):
            return {"ai_reply": self.picks.pop(0), "why": ""}

        complete = None

    slots = measure.lineup_positions("s")
    draft = {"kind": "reply", "text": "draft", "target_text": "post", "target_author": "a"}
    hit = measure.lineup(Picks([slots[0] + 1, slots[1] + 1]), draft, ["h1", "h2", "h3", "h4"], "s", "thread replies")
    assert hit["caught_in"] == 2
    miss = measure.lineup(Picks([(slots[0] + 1) % 5 + 1, (slots[1] + 1) % 5 + 1]), draft, ["h1", "h2", "h3", "h4"], "s",
                          "thread replies")
    assert miss["caught_in"] == 0
    assert measure.lineup(MockLLM(), draft, ["h1"], "s", "thread replies")["ran"] is False


def test_resolve_flags_missing_wrong_author_and_matches_text():
    now = datetime.now(timezone.utc)

    class Reader:
        def status(self, sid):
            if sid == "404":
                raise RuntimeError("HTTP 404")
            author = "someoneelse" if sid == "222" else "valkyries"
            return SimpleNamespace(text="Ballhalla awaits. ⚔️", author_handle=author, created_at=now - timedelta(minutes=10),
                                   replying_to=None, likes=1, replies=0, reposts=0, quotes=0, views=5, author_followers=9)

    got = measure.resolve([URL, "https://x.com/valkyries/status/222", "https://x.com/a/status/404", NEWS],
                          {URL: "Ballhalla awaits."}, Reader())
    assert got[URL]["resolves"] and got[URL]["author_matches_url"] and got[URL]["text_matches_target"]
    assert got["https://x.com/valkyries/status/222"]["author_matches_url"] is False
    assert got["https://x.com/a/status/404"]["resolves"] is False
    assert got[NEWS]["resolves"] is False


def audited(**kw):
    a = {"p_ai": 10, "targeted_insult": False, "engagement_bait": False, "claims": [], "unsupported_claims": [],
         "deterministic_ungrounded": []}
    return {"audit": a | kw, "lineup": {"ran": True, "caught_in": 0}}


def test_hold_reasons():
    reply = {"type": "reply", "vetoed": False}
    ok_target = {"resolves": True, "author_matches_url": True}
    assert handoff.hold_reasons(reply, audited(), ok_target) == []
    assert handoff.hold_reasons(reply, audited(), None) == ["target not checked"]
    assert handoff.hold_reasons(reply, audited(), {"resolves": False, "why": "404"}) == ["target doesn't resolve: 404"]
    assert handoff.hold_reasons(reply, audited(unsupported_claims=["x"]), ok_target) == ["unsupported claims: x"]
    assert handoff.hold_reasons(reply, audited(targeted_insult=True), ok_target)[0].startswith("targeted insult")
    caught = audited()
    caught["lineup"]["caught_in"] = 2
    assert handoff.hold_reasons(reply, caught, ok_target) == ["AI-register lineup picked it in both orders"]
    assert handoff.hold_reasons({"type": "follow", "vetoed": False}, None, None) == []


def test_screen_holds_unscreened_and_drops_failures():
    h = {"ready": [{"action_id": "a"}, {"action_id": "b"}, {"action_id": "c"}], "held": [{"action_id": "d", "held_because": ["x"]}]}
    out = handoff.apply_screen(h, {"a": {"pass": True, "reason": "on Clark's side"},
                                   "b": {"pass": False, "reason": "cheers the rival"},
                                   "d": {"pass": False, "reason": "thin"}})
    assert [r["action_id"] for r in out["ready"]] == ["a"] and out["ready"][0]["screened"] == "on Clark's side"
    assert [r["action_id"] for r in out["dropped"]] == ["b", "d"]  # a held item that fails the screen is dropped too
    assert [r["action_id"] for r in out["held"]] == ["c"]
    assert out["held"][0]["held_because"][0].startswith("not screened")
