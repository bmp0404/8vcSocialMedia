"""Fact validity windows, the grounding match, the ESPN and post parsers, the store, and the lane wiring."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from harness import config
from harness.facts import espn, ground, posts, store
from harness.learning.kinds import Fact
from harness.learning.store import FakeStore
from harness.llm import MockLLM
from harness.minds import gate, lanes, writer
from harness.minds.settings import ActionConfig
from harness.models import Narrative

AT = datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc)


def fact(answer, subject="entity-aliyah-boston", as_of="2026-10-02", valid_until="2026-11-01", status="current", fid=None):
    return Fact(id=fid or f"fact-{abs(hash((answer, subject))) % 10**8}", subject=subject, question="q?", answer=answer,
                quote=answer, source_url="https://example.test", as_of=as_of, valid_until=valid_until, status=status)


BOSTON = fact("Aliyah Boston (Indiana Fever) against the Las Vegas Aces on 2026-10-01: 19 points, 10 rebounds, "
              "3 assists, 32 minutes, 2 threes, 8-16 from the field, 2-4 from three.")
FEVER = fact("On 2026-10-01 (postseason), the Indiana Fever lost to the Las Vegas Aces 83-94 (94-83). Record after the "
             "game: Indiana Fever 28-16.", subject="entity-indiana-fever")


# ---------------------------------------------------------------- validity windows


@pytest.mark.parametrize("when, expected", [
    (date(2026, 10, 1), False),  # before as_of
    (date(2026, 10, 2), True),  # as_of is inclusive
    (date(2026, 10, 31), True),
    (date(2026, 11, 1), False),  # valid_until is exclusive, as ranking.is_stale treats it
    (datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc), True),
])
def test_live_window_is_as_of_inclusive_valid_until_exclusive(when, expected):
    assert ground.is_live(BOSTON, when) is expected


def test_superseded_or_open_ended_facts_are_never_live():
    assert not ground.is_live(fact("x", status="superseded"), AT)
    assert not ground.is_live(fact("x", valid_until=""), AT)
    assert ground.live([BOSTON, fact("y", valid_until="2026-10-03")], AT) == [BOSTON]


# ---------------------------------------------------------------- the grounding match


def test_names_match_whole_words_ignoring_case_curly_apostrophes_and_possessives():
    idx = ground.FactIndex([fact("A'ja Wilson (Las Vegas Aces) ...", subject="entity-a-ja-wilson"), FEVER])
    assert idx.grounds_name("A’ja") and idx.grounds_name("Fever’s") and idx.grounds_name("VEGAS")
    assert not idx.grounds_name("Indy") and not idx.grounds_name("Feve")


@pytest.mark.parametrize("text, end, unit", [
    ("27 pts", 2, "points"), ("10 boards and", 2, "rebounds"), ("a 27-point night", 4, "points"),
    ("30 pieces", 2, "points"), ("38% shooting", 2, "%"), ("500mb free", 3, "mb"), ("3 models", 1, "model"),
    ("31.", 2, ""), ("500 → 1,000", 3, ""),
])
def test_unit_after_folds_stat_words_and_plurals(text, end, unit):
    assert ground.unit_after(text, end) == unit


def test_number_needs_an_anchor_and_the_same_unit():
    idx = ground.FactIndex([BOSTON, FEVER])
    assert idx.grounds_number("19", "points", {"boston"})
    assert idx.grounds_number("10", "rebounds", {"aliyah"})
    assert not idx.grounds_number("19", "points", {"clark"})  # no fact about Clark
    assert not idx.grounds_number("10", "assists", {"boston"})  # 10 is her rebounds
    assert not idx.grounds_number("19", "tonight", {"boston"})
    assert idx.grounds_number("94-83", "", {"fever"}) and idx.grounds_number("83-94", "", {"fever"})
    assert idx.grounds_number("8-16", "from", {"boston"})
    assert not idx.grounds_number("28-16", "", {"aces"})  # the Fever's record, not the Aces'
    assert not idx.grounds_number("16-28", "", {"fever"})  # a pair matches only as written
    assert not idx.grounds_number("16-8", "", {"boston"})


def test_a_player_line_indexes_exactly_its_stats():
    [ix] = ground.FactIndex([BOSTON])._indexed
    assert ix.anchors == {"aliyah", "boston"}
    assert ix.pairs == {"8-16", "2-4"}
    assert ix.numbers == {("", "2026-10-01"), ("points", "19"), ("rebounds", "10"), ("assists", "3"),
                          ("minutes", "32"), ("threes", "2")}
    # "2-4 3PT" once indexed a "3 points" for every player who played.
    assert not gate.check("Boston had just 3 points tonight", context=POST, facts=ground.FactIndex([BOSTON])).passed


def test_index_anchors_apply_to_every_draft():
    idx = ground.FactIndex([FEVER], ground.anchor_words(ground.split_handle("IndianaFever")))
    assert ground.grounded_numbers(idx, "lost 83-94 again", "") == {5}
    assert ground.grounded_numbers(ground.FactIndex([FEVER]), "lost 83-94 again", "") == set()
    assert ground.grounded_numbers(ground.FactIndex([FEVER]), "lost 83-94 again", "the Fever were in it") == {5}


def test_split_handle_and_anchor_stopwords():
    assert ground.split_handle("@CaitlinClark22") == "Caitlin Clark 22"
    assert ground.split_handle("valkyries") == "valkyries"
    assert ground.subject_anchors(fact("x", subject="entity-golden-state-valkyries")) == {"golden", "valkyries"}


def test_about_picks_facts_naming_the_text_newest_first():
    older = fact("old line", as_of="2026-09-20", fid="fact-old")
    newer = fact("new line", as_of="2026-10-02", fid="fact-new")
    assert [f.id for f in ground.about([FEVER, older, newer], "Boston looked tired", 5)] == ["fact-new", "fact-old"]
    assert ground.about([FEVER, older, newer], "Boston looked tired", 1) == [newer]


# ---------------------------------------------------------------- the gate with facts


POST = "Season's over. Thank you, Indy."


def test_gate_accepts_a_fact_backed_name_and_stat_and_still_flags_invented_ones():
    idx = ground.FactIndex([BOSTON, FEVER], ground.anchor_words("Indiana Fever " + POST))
    true = "Boston had 19 points and 10 boards in the last one"
    invented = "Boston dropped 31 tonight and Mitchell had 12 assists"
    assert not gate.check(true, context=POST).passed
    assert gate.check(true, context=POST, facts=idx).passed
    flagged = {f.detail for f in gate.check(invented, context=POST, facts=idx).findings}
    assert flagged == {"number '31' isn't in the context", "number '12' isn't in the context", "name 'Mitchell' isn't in the context"}


def test_facts_never_silence_style_rules_or_handles():
    idx = ground.FactIndex([BOSTON])
    rules = {f.rule for f in gate.check("Boston — a testament @nobody", context=POST, facts=idx).findings}
    assert rules == {"em_dash", "ai_vocab", "unknown_handle"}


def test_facts_used_names_the_fact_behind_each_name_and_number_the_context_lacks():
    roster = fact("Indiana Fever roster: Aliyah Boston (#7, F), Kelsey Mitchell (#0, G).", subject="entity-indiana-fever",
                  fid="fact-roster")
    idx = ground.FactIndex([BOSTON, FEVER, roster], ground.anchor_words("Indiana Fever " + POST))
    used = gate.facts_used("so Boston had 19 points and 10 boards, Indy", context=POST, facts=idx)
    assert {(f.id, token) for f, token in used} == {(BOSTON.id, "Boston"), (roster.id, "Boston"), (BOSTON.id, "19"),
                                                    (BOSTON.id, "10")}  # "Indy" is in the post
    # A number with the wrong unit backs nothing, the same as in the gate; a [VERIFY] marker is the person's to check.
    assert gate.facts_used("so Boston dropped 19 tonight [VERIFY: 31 assists]", context="Boston", facts=idx) == []


# ---------------------------------------------------------------- ESPN parsers

SUMMARY = {
    "header": {"season": {"type": 3}, "competitions": [{
        "date": "2026-10-02T01:00Z", "status": {"type": {"completed": True}},
        "series": [{"type": "playoff", "summary": "LV wins series 2-1"}],
        "competitors": [
            {"team": {"displayName": "Las Vegas Aces"}, "score": "94", "winner": True, "record": [{"type": "total", "summary": "31-13"}]},
            {"team": {"displayName": "Indiana Fever"}, "score": "83", "winner": False, "record": [{"type": "total", "summary": "28-16"}]},
        ]}]},
    "boxscore": {"players": [{"team": {"displayName": "Indiana Fever"}, "statistics": [{
        "names": ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK"],
        "athletes": [
            {"athlete": {"displayName": "Aliyah Boston"}, "stats": ["32", "19", "8-16", "2-4", "1-2", "10", "3", "0", "1", "2"]},
            {"athlete": {"displayName": "Bench Player"}, "didNotPlay": True, "stats": []},
        ]}]}]},
}


def test_game_facts_write_one_result_per_team_and_one_line_per_player_who_played():
    facts = espn.game_facts(SUMMARY, "https://espn.test/summary")
    by_subject = {f.subject: f for f in facts}
    assert set(by_subject) == {"entity-las-vegas-aces", "entity-indiana-fever", "entity-aliyah-boston"}
    assert by_subject["entity-indiana-fever"].answer == (
        "On 2026-10-01 (postseason), the Indiana Fever lost to the Las Vegas Aces 83-94 (94-83). "
        "Record after the game: Indiana Fever 28-16. Series: LV wins series 2-1.")
    assert by_subject["entity-aliyah-boston"].answer == (
        "Aliyah Boston (Indiana Fever) against the Las Vegas Aces on 2026-10-01: 19 points, 10 rebounds, 3 assists, "
        "1 steals, 2 blocks, 0 turnovers, 32 minutes, 2 threes, 8-16 from the field, 2-4 from three, 1-2 from the line.")
    # Tip-off 01:00Z plus three hours is 04:00Z on Oct 2; live for 30 days from there.
    assert {(f.as_of, f.valid_until) for f in facts} == {("2026-10-02", "2026-11-01")}
    assert json.loads(by_subject["entity-aliyah-boston"].quote)["stats"]["PTS"] == "19"


def test_unfinished_game_gives_no_facts():
    live = json.loads(json.dumps(SUMMARY))
    live["header"]["competitions"][0]["status"]["type"]["completed"] = False
    assert espn.game_facts(live, "u") == []


def test_roster_next_game_and_injury_windows():
    roster = {"athletes": [{"displayName": "Aliyah Boston", "jersey": "7", "position": {"abbreviation": "C"}}],
              "coach": [{"firstName": "Stephanie", "lastName": "White"}]}
    [r] = espn.roster_facts(roster, "Indiana Fever", "u", AT)
    assert r.answer == "Indiana Fever roster: Aliyah Boston (#7, C). Head coach: Stephanie White."
    assert (r.as_of, r.valid_until) == ("2026-10-03", "2026-10-10")

    schedule = {"events": [
        {"id": "1", "date": "2026-10-01T01:00Z", "competitions": [{"competitors": []}]},
        {"id": "2", "date": "2026-10-17T19:00Z", "competitions": [{"venue": {"fullName": "Gainbridge Fieldhouse"}, "competitors": [
            {"homeAway": "home", "team": {"displayName": "Indiana Fever"}}, {"homeAway": "away", "team": {"displayName": "Chicago Sky"}}]}]}]}
    [n] = espn.next_game_fact(schedule, "Indiana Fever", "u", AT)
    assert n.answer == "The Indiana Fever next play against the Chicago Sky on 2026-10-17 at Gainbridge Fieldhouse."
    assert (n.as_of, n.valid_until) == ("2026-10-03", "2026-10-18")

    injuries = {"injuries": [{"displayName": "Indiana Fever", "injuries": [
        {"athlete": {"displayName": "Aliyah Boston"}, "status": "Out", "shortComment": "Knee surgery.", "date": "2026-10-02T15:05Z"},
        {"athlete": {"displayName": "Later Update"}, "status": "Out", "date": "2026-10-04T15:05Z"}]}]}
    [i] = espn.injury_facts(injuries, "u", AT)  # the entry updated after AT isn't knowable yet
    assert i.answer == "Aliyah Boston (Indiana Fever) injury status: Out. Knee surgery."
    assert (i.as_of, i.valid_until) == ("2026-10-02", "2026-10-05")


# ---------------------------------------------------------------- posts and the thread


def test_post_and_thread_facts_dates_and_subjects():
    when = datetime(2026, 9, 20, 23, 30, tzinfo=timezone.utc)
    [p] = posts.post_facts("@IndianaFever", [{"id": "99", "text": "Game 3 tickets on sale", "created_at": when, "url": "u"},
                                             {"id": "100", "text": "  ", "created_at": when}])
    assert (p.id, p.subject, p.as_of, p.valid_until) == ("fact-post-99", "entity-indiana-fever", "2026-09-20", "2026-10-20")
    [t] = posts.thread_facts("IndianaFever", "https://x.com/IndianaFever/status/1", ["one", "", "two"], AT)
    assert (t.answer, t.as_of, t.valid_until) == ("one\ntwo", "2026-10-03", "2026-10-04")
    assert posts.thread_facts("IndianaFever", "u", [" "], AT) == []


# ---------------------------------------------------------------- store


def test_store_round_trip_and_live_filter():
    s = FakeStore()
    expired = fact("gone", valid_until="2026-10-03", fid="fact-gone")
    assert store.save([BOSTON, expired], s) == 2
    assert {f.id for f in store.load(s)} == {BOSTON.id, "fact-gone"}
    assert store.load_live(AT, s) == [BOSTON]


class FakeEspn:
    def __init__(self):
        self.calls = []

    def facts_at(self, at, rosters=False):
        self.calls.append((at, rosters))
        return [BOSTON]


def test_refresh_reads_espn_for_a_profile_with_a_scoreboard():
    s, e = FakeStore(), FakeEspn()
    assert store.refresh("wnba", at=AT, espn=e, store=s) == {"espn:basketball/wnba": 1}
    assert e.calls == [(AT, True)] and store.load(s) == [BOSTON]


class FakePost:
    def __init__(self, pid, author, text, repost=False):
        self.id, self.author_handle, self.text, self.is_repost = pid, author, text, repost
        self.created_at, self.url = AT - timedelta(days=1), f"https://x.com/{author}/status/{pid}"


class FakeReader:
    def profile_statuses(self, handle, limit=20):
        return [FakePost("1", handle, "We shipped 2.0"), FakePost("2", "someone", "theirs", repost=True)]


def test_refresh_reads_recent_posts_for_a_profile_without_a_scoreboard():
    s = FakeStore()
    counts = store.refresh("devtools", at=AT, reader=FakeReader(), store=s)
    assert counts and set(counts.values()) == {1}
    assert {f.answer for f in store.load(s)} == {"We shipped 2.0"}


class FlakyReader(FakeReader):
    def __init__(self, failing):
        self.failing = failing

    def profile_statuses(self, handle, limit=20):
        from harness.context.http import FetchError
        if handle in self.failing:
            raise FetchError(f"HTTP 404 from api.fxtwitter.com for /2/profile/{handle}/statuses")
        return super().profile_statuses(handle, limit)


def test_refresh_skips_a_timeline_it_cant_read_and_keeps_the_others(capsys):
    from harness.sources.watch import load_watch
    with config.use_profile("devtools"):
        handles = load_watch().reply_targets.accounts
    s = FakeStore()
    counts = store.refresh("devtools", at=AT, reader=FlakyReader({handles[0]}), store=s)
    assert set(counts) == {f"posts:{h}" for h in handles[1:]}
    assert f"[facts] skipped @{handles[0]}: HTTP 404" in capsys.readouterr().out
    with pytest.raises(store.FactsError, match="no account's timeline"):
        store.refresh("devtools", at=AT, reader=FlakyReader(set(handles)), store=FakeStore())


def test_refresh_skips_an_unreadable_account_and_fails_only_when_none_can_be_read():
    from harness.context.http import FetchError

    class Flaky(FakeReader):
        def __init__(self, bad):
            self.bad = bad

        def profile_statuses(self, handle, limit=20):
            if handle in self.bad or self.bad == "all":
                raise FetchError(f"HTTP 404 from api.fxtwitter.com for /2/profile/{handle}/statuses")
            return super().profile_statuses(handle, limit)

    with config.use_profile("devtools"):
        from harness.sources.watch import load_watch
        handles = load_watch().reply_targets.accounts
    skipped = {}
    counts = store.refresh("devtools", at=AT, reader=Flaky({handles[0]}), store=FakeStore(), skipped=skipped)
    assert f"posts:{handles[0]}" not in counts and len(counts) == len(handles) - 1
    assert list(skipped) == [handles[0]] and "404" in skipped[handles[0]]
    with pytest.raises(store.FactsError, match="no account's timeline"):
        store.refresh("devtools", at=AT, reader=Flaky("all"), store=FakeStore())


def test_refresh_refuses_the_mock_providers_throwaway_store(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "mock")
    monkeypatch.delenv("MEMSTORE_PATH", raising=False)
    with pytest.raises(store.FactsError, match="temporary store"):
        store.refresh("wnba", at=AT, espn=FakeEspn())


# ---------------------------------------------------------------- the lane


X_URL = "https://x.com/IndianaFever/status/2105879899399668010"
REPLIES = [{"text": t, "likes": 5} for t in ("thank you for the season", "proud of this group", "next year is ours",
                                              "what a run it was", "love this team so much")]


@pytest.fixture
def tmp_out(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    lanes.PENDING.clear()


def lane_run(llm, facts):
    t = {"url": X_URL, "author": "IndianaFever", "text": POST, "age_minutes": 5.0, "engagement_per_hour": 100.0,
         "thread": REPLIES, "replies": len(REPLIES)}
    n = Narrative(id="season", title="Season over", summary="Fever out in round one", relevance=0.9, velocity=0.5)
    n.reply_targets = [t]
    cfg = ActionConfig()
    return lanes.run(llm, "mission", "lessons", [n], [], cfg=cfg, budget=lanes.Budget(cfg), takes=[],
                     fetch=lambda url: [], facts=facts)


def test_lane_gate_grounds_drafts_on_live_facts(tmp_out, monkeypatch):
    monkeypatch.setattr(writer, "continuations", lambda *a, **k: ["Boston had 19 points and 10 boards"])
    _, without = lane_run(MockLLM(), [])
    assert by_reason(without) == "gate_rejected"
    actions, with_facts = lane_run(MockLLM(), [BOSTON])
    assert [a.text for a in actions if a.type == "reply"] == ["Boston had 19 points and 10 boards"]
    assert actions[0].draft_meta["facts_live"] == 1


class VetoSpy(MockLLM):
    """Keeps the user prompt of every veto call."""

    def __init__(self):
        super().__init__()
        self.veto_prompts = []

    def complete_json(self, system, prompt, mock=None):
        if system.startswith("You screen candidate"):
            self.veto_prompts.append(prompt)
        return super().complete_json(system, prompt, mock)


def test_the_veto_sees_the_facts_the_gate_leaned_on_and_the_critic_gets_the_same(tmp_out, monkeypatch):
    monkeypatch.setattr(writer, "continuations", lambda *a, **k: ["Boston had 19 points and 10 boards"])
    gate_contexts = []
    real_check = gate.check
    monkeypatch.setattr(gate, "check", lambda *a, **k: gate_contexts.append(k.get("context")) or real_check(*a, **k))
    spy = VetoSpy()
    actions, _ = lane_run(spy, [FEVER, BOSTON])
    [reply] = [a for a in actions if a.type == "reply"]
    line = f"- fact, true as of {BOSTON.as_of}: {BOSTON.answer}"
    prompts = [p for p in spy.veto_prompts if p.startswith("POST by @IndianaFever")]  # not the caption's
    assert len(prompts) == 2 and all(lanes.VETO_FACTS_HEADER + "\n" + line in p for p in prompts)
    assert "Indiana Fever lost" not in prompts[0]  # the gate didn't need the team's result
    assert reply.draft_meta["facts_shown_to_veto"] == 1
    assert line in reply.draft_meta["veto_context"] and prompts[0].startswith(reply.draft_meta["veto_context"])
    assert gate_contexts and not any("LIVE FACTS" in c for c in gate_contexts if c)


@pytest.mark.parametrize("facts", [None, [FEVER]])
def test_the_veto_context_is_the_gates_context_when_the_gate_leaned_on_no_fact(tmp_out, monkeypatch, facts):
    monkeypatch.setattr(writer, "continuations", lambda *a, **k: ["thank you for the season, indy"])
    gate_contexts = []
    real_check = gate.check
    monkeypatch.setattr(gate, "check", lambda *a, **k: gate_contexts.append(k.get("context")) or real_check(*a, **k))
    actions, _ = lane_run(VetoSpy(), facts)
    [reply] = [a for a in actions if a.type == "reply"]
    reply_context = next(c for c in gate_contexts if c and c.startswith("POST by @IndianaFever"))
    assert reply.draft_meta["veto_context"] == reply_context and reply.draft_meta["facts_shown_to_veto"] == 0


def test_a_reply_from_the_thread_is_shown_as_a_reply_and_only_the_lines_that_hold_the_name():
    thread = posts.thread_facts("IndianaFever", X_URL, ["Mitchell was cooking", "great season", "bring back White"], AT)
    lines = lanes.veto_facts(["honestly Mitchell carried us"], POST, POST, ground.FactIndex(thread, ()))
    assert lines == ["- a reply under this post said: Mitchell was cooking"]
    assert lanes.veto_facts(["so Boston had 19 points"], POST, POST, None) == []


NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet", "Kilo", "Lima"]


def test_the_block_is_capped_and_every_draft_gets_its_best_fact_before_any_gets_a_second():
    many = [fact(f"{n} scored", subject=f"entity-{n.lower()}", fid=f"fact-{n}") for n in NAMES]
    seconds = [fact(f"{n} scored again", subject=f"entity-{n.lower()}", as_of="2026-09-01", fid=f"fact-{n}-2") for n in NAMES]
    idx = ground.FactIndex(many + seconds)
    assert len(lanes.veto_facts(["ok " + " ".join(NAMES)], POST, POST, idx)) == lanes.VETO_FACTS
    # The first draft names 9 people, each backed twice; the second draft's one name still gets its fact.
    lines = lanes.veto_facts(["ok " + " ".join(NAMES[:9]), "and Lima too"], POST, POST, idx)
    assert len(lines) == lanes.VETO_FACTS and "- fact, true as of 2026-10-02: Lima scored" not in lines
    lines = lanes.veto_facts(["ok " + " ".join(NAMES[:5]), "and Lima too"], POST, POST, idx)
    assert lines[:6] == [f"- fact, true as of 2026-10-02: {n} scored" for n in NAMES[:5] + ["Lima"]]


def test_a_long_fact_is_cut_to_a_window_around_the_whole_token():
    roster = "Team roster: " + ", ".join(f"Name{i} (#{i}, G)" for i in range(40)) + ", Kelsey Mitchell (#0, G)."
    snippet = lanes._snippet(roster, "Mitchell")
    assert "Kelsey Mitchell" in snippet and snippet.startswith("…") and len(snippet) <= lanes.FACT_CHARS + 2
    line = "Clark (Indiana Fever) shot 6-18 from the field. " + "x " * 200 + "She finished with 18 points."
    assert "18 points" in lanes._snippet(line, "18")


@pytest.mark.parametrize("draft", [
    "so Boston had 19 points and 10 boards in the last one",
    "honestly Boston dropped 31 tonight and Mitchell had 12 assists",
    "the Fever lost 83-94 and Boston had 8-16 from the field",
    "so Boston had 19 points [VERIFY: 40 minutes] and Indy",
    "Season's over. thank you",
])
def test_facts_used_names_exactly_the_grounding_findings_the_facts_cleared(draft):
    """facts_used must agree with the gate: its tokens are the name and number findings that facts remove."""
    idx = ground.FactIndex([BOSTON, FEVER], ground.anchor_words("Indiana Fever " + POST))

    def flagged(facts):
        return {f.detail.split("'")[1] for f in gate.check(draft, context=POST, facts=facts).findings
                if f.rule in ("ungrounded_name", "ungrounded_number")}

    assert {token for _, token in gate.facts_used(draft, context=POST, facts=idx, post=POST)} == flagged(None) - flagged(idx)


def by_reason(decisions):
    return next(d.reason for d in decisions if d.lane == "reply")


class PlanSpy(MockLLM):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def complete_json(self, system, prompt, mock=None):
        if "decide whether the account should reply" in system:
            self.prompts.append(prompt)
        return super().complete_json(system, prompt, mock)


def test_planner_sees_facts_about_the_post_and_the_writer_does_not(tmp_out, monkeypatch):
    seen = []
    monkeypatch.setattr(writer, "continuations", lambda llm, author, post, page, *a, **k: seen.append((post, page)) or ["proud of them"])
    spy = PlanSpy()
    lane_run(spy, [FEVER, BOSTON])
    [prompt] = spy.prompts
    assert "fact, true as of 2026-10-02: On 2026-10-01 (postseason), the Indiana Fever lost" in prompt
    assert "Aliyah Boston" not in prompt  # her fact is about neither the Fever nor anything the post names
    assert all("83-94" not in post and not any("83-94" in r for r in page) for post, page in seen)


def test_planner_prompt_is_unchanged_without_facts():
    spy = PlanSpy()
    lanes.plan_targets(spy, "mission", "lessons", [{"url": X_URL, "author": "IndianaFever", "text": POST}])
    assert "fact" not in spy.prompts[0]


def test_fact_numbers_are_found_the_way_the_gate_finds_them():
    assert ground.NUMBER.pattern == gate.NUMBER.pattern


class OneMissingSummary(espn.Espn):
    """Two finished games for one team; the older one's summary 404s."""

    def __init__(self):
        super().__init__(client=None)
        self.client = None

    def teams(self):
        return [{"id": "5", "displayName": "Indiana Fever"}]

    def get(self, path, settled_after=None, max_age_s=3600):
        done = {"completed": True}
        if path.startswith("teams/5/schedule"):
            return {"events": [{"id": "1", "date": "2026-09-27T20:00Z", "competitions": [{"status": {"type": done}}]},
                               {"id": "2", "date": "2026-10-02T01:00Z", "competitions": [{"status": {"type": done}}]}]}, "u"
        if path == "summary?event=1":
            raise espn.FetchError("HTTP 404 from site.api.espn.com")
        if path == "summary?event=2":
            return SUMMARY, "u"
        return {"injuries": []}, "u"


def test_a_missing_box_score_skips_that_game_only():
    e = OneMissingSummary()
    facts = e.facts_at(AT, history_days=30)
    assert {f.subject for f in facts} >= {"entity-indiana-fever", "entity-aliyah-boston"}
    assert list(e.skipped) == ["1"]


def test_a_teams_game_facts_cover_only_its_own_side():
    facts = espn.game_facts(SUMMARY, "u", team="Indiana Fever")
    assert {f.subject for f in facts} == {"entity-indiana-fever", "entity-aliyah-boston"}


class TwoGames(OneMissingSummary):
    """The same team's two finished games, both readable; the newer one should be what the store keeps."""

    def get(self, path, settled_after=None, max_age_s=3600):
        if path == "summary?event=1":
            older = json.loads(json.dumps(SUMMARY))
            older["header"]["competitions"][0]["date"] = "2026-09-27T20:00Z"
            older["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][1] = "31"
            return older, "u"
        return super().get(path, settled_after, max_age_s)


def test_a_newer_game_replaces_the_older_ones_facts():
    facts = TwoGames().facts_at(AT, history_days=30)
    [boston] = [f for f in facts if f.subject == "entity-aliyah-boston"]
    [fever] = [f for f in facts if f.subject == "entity-indiana-fever"]
    assert "19 points" in boston.answer and "31 points" not in boston.answer and boston.as_of == "2026-10-02"
    assert fever.as_of == "2026-10-02"


def test_game_facts_refuse_a_team_that_didnt_play():
    with pytest.raises(ValueError, match="didn't play"):
        espn.game_facts(SUMMARY, "u", team="Indiana Fever ")
