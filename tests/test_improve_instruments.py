"""The instrument ledger: the demotion rule on synthetic outcomes, the ledger's events and history,
the statistics behind it, and the scorers' caching and abstentions."""

import random
from datetime import datetime, timedelta, timezone

import pytest
from watch_threads import Threads

from harness.corpus import load
from harness.corpus.bench import Pair
from harness.improve import fold, instruments, outcomes, scorers
from harness.improve.instruments import GATES, MIN_N, REPORT_ONLY, Score
from harness.improve.outcomes import SguReply
from harness.iterate.llm import Meter, MockMeteredLLM

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
CUT = NOW - timedelta(days=5)  # after _evidence's frozen pair, before its watcher threads


def score(source="threads", n=30, ci=(0.55, 0.7), chance=0.5, ids="a"):
    est = None if ci is None else sum(ci) / 2
    return Score(source, "pairwise_accuracy" if chance == 0.5 else "spearman", chance, n, n * 3, est, ci, 1.0,
                 instruments.ids_digest([ids]))


# ---------------------------------------------------------------- the rule


@pytest.mark.parametrize("scores, status, says", [
    ({}, GATES, "no source with 20"),
    ({"threads": score(n=MIN_N - 1, ci=(0.3, 0.9))}, GATES, "no source with 20"),
    ({"threads": score(n=MIN_N, ci=(0.45, 0.6))}, REPORT_ONLY, "includes chance"),
    ({"threads": score(n=25, ci=(0.55, 0.7))}, GATES, "above chance"),
    ({"threads": score(n=25, ci=(0.30, 0.45))}, REPORT_ONLY, "below chance"),
    ({"threads": score(n=25, ci=None)}, GATES, "can't judge"),
    # A screen passes every shipped _sgu_ reply, so _sgu_ can't judge it; the threads decide.
    ({"sgu": score("sgu", 25, None, 0.0), "threads": score(n=30, ci=(0.6, 0.8))}, GATES, "threads"),
    ({"sgu": score("sgu", 25, None, 0.0), "threads": score(n=30, ci=(0.4, 0.55))}, REPORT_ONLY, "threads"),
    # Real _sgu_ results decide once they reach 20, whatever the threads say.
    ({"sgu": score("sgu", 20, (-0.1, 0.3), 0.0), "threads": score(n=50, ci=(0.6, 0.7))}, REPORT_ONLY, "sgu"),
    ({"sgu": score("sgu", 20, (0.1, 0.5), 0.0), "threads": score(n=50, ci=(0.4, 0.55))}, GATES, "sgu"),
    # Under 20 _sgu_ outcomes, the threads decide.
    ({"sgu": score("sgu", 10, (0.2, 0.9), 0.0), "threads": score(n=30, ci=(0.45, 0.6))}, REPORT_ONLY, "threads"),
])
def test_the_demotion_rule(scores, status, says):
    got, why = instruments.decide(scores)
    assert got == status and says in why


def test_a_report_only_instrument_never_gates():
    assert instruments.decide({"threads": score(n=500, ci=(0.8, 0.9))}, role="report")[0] == REPORT_ONLY


def _sgu(n, informative: bool, seed=0):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        pct = rng.random()
        out.append((f"a{i}", pct + rng.gauss(0, 0.15) if informative else rng.random(), pct))
    return out


def test_the_rule_on_synthetic_sgu_outcomes():
    # Below 20 outcomes even a useless instrument keeps gating; at 20 and more it is judged.
    for n, informative, expected in ((12, False, GATES), (40, True, GATES), (40, False, REPORT_ONLY)):
        s = scorers._spearman(_sgu(n, informative), {})
        assert s.n == n and instruments.decide({"sgu": s})[0] == expected, (n, informative, s.ci95)


def test_the_rule_on_synthetic_thread_pairs():
    rng = random.Random(1)
    pairs, good, noise = [], {}, {}
    for t in range(30):
        for k in range(4):
            w, l = f"t{t}w{k}", f"t{t}l{k}"
            pairs.append(Pair(f"{t}:{k}", "replies", f"t{t}", w, l, 10, 1, "rolling"))
            good[w], good[l] = 1.0 + rng.random(), rng.random()
            noise[w], noise[l] = rng.random(), rng.random()
    assert instruments.decide({"threads": scorers._pairwise(pairs, good, lambda p: p.group, {})})[0] == GATES
    s = scorers._pairwise(pairs, noise, lambda p: p.group, {})
    assert s.n == 30 and instruments.decide({"threads": s})[0] == REPORT_ONLY


# ---------------------------------------------------------------- the ledger


def test_record_logs_status_changes_with_when_and_why_and_grows_history_only_on_new_outcomes():
    led = {}
    cut = NOW - timedelta(days=3)
    ev1 = instruments.record(led, "x", role="gate", what="w", version="v1", cutoff=cut, cutoff_basis="b",
                             scores={"threads": score(n=5, ids="a")}, now=NOW)
    assert [e["event"] for e in ev1] == ["first seen", "status"] and ev1[1]["to"] == GATES
    assert len(led["instruments"]["x"]["history"]) == 1
    # The same outcomes again: no event, no history.
    assert instruments.record(led, "x", role="gate", what="w", version="v1", cutoff=cut, cutoff_basis="b",
                              scores={"threads": score(n=5, ids="a")}, now=NOW + timedelta(hours=1)) == []
    assert len(led["instruments"]["x"]["history"]) == 1
    # Enough outcomes, interval includes chance: demoted, and the event says when and why.
    later = NOW + timedelta(days=2)
    ev2 = instruments.record(led, "x", role="gate", what="w", version="v1", cutoff=cut, cutoff_basis="b",
                             scores={"threads": score(n=25, ci=(0.4, 0.6), ids="b")}, now=later)
    assert ev2 == [{"instrument": "x", "at": later.isoformat(timespec="seconds"), "event": "status",
                    "from": GATES, "to": REPORT_ONLY, "why": led["instruments"]["x"]["reason"]}]
    entry = led["instruments"]["x"]
    assert entry["status"] == REPORT_ONLY and entry["status_since"] == later.isoformat(timespec="seconds")
    assert len(entry["history"]) == 2 and "includes chance" in entry["reason"]
    # Back above chance: reinstated, recorded too.
    ev3 = instruments.record(led, "x", role="gate", what="w", version="v1", cutoff=cut, cutoff_basis="b",
                             scores={"threads": score(n=40, ci=(0.56, 0.7), ids="c")}, now=later + timedelta(days=1))
    assert ev3[0]["from"] == REPORT_ONLY and ev3[0]["to"] == GATES
    assert [e["event"] for e in led["instruments"]["x"]["events"]] == ["first seen", "status", "status", "status"]


def test_a_new_version_restarts_the_cutoff_and_keeps_the_old_numbers(tmp_path):
    led = {}
    instruments.record(led, "x", role="gate", what="w", version="v1", cutoff=NOW, cutoff_basis="b",
                       scores={"threads": score(n=25, ids="a")}, now=NOW)
    assert instruments.first_seen(led, "x", "v1", NOW + timedelta(days=5)) == NOW
    assert instruments.first_seen(led, "x", "v2", NOW + timedelta(days=5)) == NOW + timedelta(days=5)
    later = NOW + timedelta(days=5)
    events = instruments.record(led, "x", role="gate", what="w", version="v2", cutoff=later, cutoff_basis="b",
                                scores={"threads": score(n=0, ci=None, ids="")}, now=later)
    assert events[0]["event"] == "new version" and events[0]["version"] == "v2"
    entry = led["instruments"]["x"]
    assert entry["version_since"] == later.isoformat(timespec="seconds")
    assert [h["version"] for h in entry["history"]] == ["v1", "v2"]
    instruments.save(led, tmp_path / "instruments.json")
    assert instruments.load(tmp_path / "instruments.json")["instruments"]["x"]["version"] == "v2"


# ---------------------------------------------------------------- statistics


def test_spearman_known_values():
    assert outcomes.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert outcomes.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # Ties take average ranks: x ranks 1.5, 1.5, 3, 4 against y ranks 1, 2, 3, 4.
    assert outcomes.spearman([0, 0, 1, 2], [1, 2, 3, 4]) == pytest.approx(0.9486833, abs=1e-6)
    assert outcomes.spearman([1, 1, 1], [1, 2, 3]) is None
    assert outcomes.spearman([1, 2], [1, 2]) is None
    with pytest.raises(ValueError):
        outcomes.spearman([1, 2, 3], [1, 2])


def test_an_abstention_scores_as_the_worst_rank():
    pairs = [Pair("1", "replies", "t", "w", "l", 9, 1, "rolling"), Pair("2", "replies", "t", "w2", "l2", 9, 1, "rolling"),
             Pair("3", "replies", "t", "w3", "l3", 9, 1, "rolling")]
    assert outcomes.pair_credits(pairs, {"w": None, "l": 0.0, "w2": 1.0, "l2": None, "w3": None, "l3": None}) == [0.0, 1.0, 0.5]


# ---------------------------------------------------------------- scorers on synthetic threads


def _evidence(tmp_path, texts_by_thread, likes):
    """Mature watcher threads after the frozen benchmark, folded, as Evidence."""
    t = Threads(tmp_path / "threads")
    start = NOW - timedelta(days=4)
    for i, texts in enumerate(texts_by_thread):
        t.thread(start + timedelta(hours=i), timedelta(hours=50), likes, author=f"brand{i}", texts=texts,
                 root_text="New release: faster builds and a CLI for preview deploys")
    src = t.write()
    watch = load(src)
    frozen_pairs = [Pair("r0:a:b", "replies", "r0", "a", "b", 9, 1, "test", 4, (start - timedelta(days=2)).isoformat(), 1.0)]
    f = fold.fold(watch, frozen_pairs, [])
    # The watch corpus stands in for the frozen one: CUT falls after the frozen pair, so only folded pairs count.
    return outcomes.Evidence(watch, frozen_pairs, watch, f.pairs, [], fold.profiles(src))


def _ctx(tmp_path, budget=1000, respond=None):
    meter = Meter(budget)
    calls = []

    def llm_for(model, name):
        return MockMeteredLLM(meter, name, respond or (lambda s, p: calls.append(p) or {}), model)
    return scorers.Ctx(llm_for, meter, tmp_path / "cache", log=lambda m: None), meter


class FixedLLM:
    """A model that answers every call with `answer` (or raises it), counting calls; it ignores mock=."""

    is_mock = False

    def __init__(self, answer, meter=None):
        self.answer, self.calls, self.meter = answer, 0, meter

    def complete_json(self, system, prompt, mock=None):
        if self.meter is not None:
            self.meter.reserve("test")
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(prompt) if callable(self.answer) else self.answer


def _fixed_ctx(tmp_path, llm, meter=None):
    return scorers.Ctx(lambda model, name: llm, meter or Meter(1000), tmp_path / "cache", log=lambda m: None)


def test_the_gate_credits_only_discordant_pairs_and_reports_its_pass_rate(tmp_path):
    # Likes 20, 9, 1, 0, 2: the 20 and 9 replies and the 2-like one pass; the em dash and the hashtag stack
    # fail. Per thread six pairs clear the margin: four discordant (20 and 9 each beat 1 and 0, and the
    # passing reply won each time) and two concordant (20 and 9 each beat the passing 2), which must not count.
    texts = ["faster builds are the whole pitch for me", "preview deploys from the cli finally",
             "this is great — truly a new era", "#ship #it #now #go", "the cli bit is what i wanted"]
    ev = _evidence(tmp_path, [texts] * 3, [20, 9, 1, 0, 2])
    assert len(ev.rolling) == 18
    s = scorers.SlopGate().score(ev, CUT, _ctx(tmp_path)[0])["threads"]
    assert s.detail["pass_rate_on_real_replies"] == pytest.approx(0.6)
    assert s.detail["discordant_pairs"] == 12 and s.items == 12 and s.estimate == 1.0 and s.n == 3


def test_the_lane_veto_caches_answers_and_defers_when_the_budget_runs_out(tmp_path):
    texts = ["faster builds are the whole pitch for me", "preview deploys from the cli finally", "nice work team here"]
    ev = _evidence(tmp_path, [texts] * 3, [20, 1, 0])
    ctx, meter = _ctx(tmp_path, budget=2)  # one thread's two veto orders, then the budget is gone
    s = scorers.LaneVeto().score(ev, CUT, ctx)["threads"]
    # Each thread has three replies in pairs (20 beats 1 and 0): one thread judged, two deferred.
    assert meter.calls == 2 and s.detail["deferred"] == 6 and s.detail["replies_judged"] == 3
    ctx2, meter2 = _ctx(tmp_path, budget=100)
    s2 = scorers.LaneVeto().score(ev, CUT, ctx2)["threads"]
    assert meter2.calls == 4 and s2.detail["deferred"] == 0 and s2.detail["replies_judged"] == 9
    ctx3, meter3 = _ctx(tmp_path, budget=100)
    scorers.LaneVeto().score(ev, CUT, ctx3)
    assert meter3.calls == 0  # every verdict came from the cache


def test_the_action_veto_judges_one_reply_per_call_and_never_caches_a_failed_call(tmp_path):
    texts = ["faster builds are the whole pitch for me", "preview deploys from the cli finally", "nice work team here"]
    ev = _evidence(tmp_path, [texts], [20, 1, 0])
    down = FixedLLM(RuntimeError("HTTP 429 cooling down"))
    s = scorers.ActionVeto().score(ev, CUT, _fixed_ctx(tmp_path, down))["threads"]
    assert down.calls == 3 and s.detail["deferred"] == 3 and s.detail["replies_judged"] == 0
    # The most-liked reply passes, the others are vetoed: every pair is discordant and the screen is right.
    up = FixedLLM(lambda prompt: {"veto": "faster builds" not in prompt.split("DRAFT REPLY:")[1]})
    s = scorers.ActionVeto().score(ev, CUT, _fixed_ctx(tmp_path, up))["threads"]
    assert up.calls == 3 and s.estimate == 1.0 and s.items == 2 and s.coverage == 1.0
    assert scorers.ActionVeto().score(ev, CUT, _fixed_ctx(tmp_path, FixedLLM({})))["threads"].items == 2  # cached


def test_an_unusable_veto_answer_is_an_abstention_that_counts_as_vetoed(tmp_path):
    texts = ["faster builds are the whole pitch for me", "preview deploys from the cli finally", "nice work team here"]
    ev = _evidence(tmp_path, [texts], [20, 1, 0])
    vague = FixedLLM(lambda prompt: {"veto": "maybe"} if "faster builds" in prompt.split("DRAFT REPLY:")[1]
                     else {"veto": False})
    s = scorers.ActionVeto().score(ev, CUT, _fixed_ctx(tmp_path, vague))["threads"]
    assert s.estimate == 0.0 and s.coverage == pytest.approx(2 / 3)  # the winner abstained, so it "failed"


def test_the_lineup_detector_credits_the_named_reply_losing(tmp_path):
    texts = [f"reply number {i} with words" for i in range(6)]
    likes = [30, 12, 6, 3, 1, 0]
    ev = _evidence(tmp_path, [texts], likes)

    def name_least_liked(prompt):
        shown = [line.split(". ", 1)[1] for line in prompt.split("REPLIES:\n")[1].splitlines()]
        return {"ai_reply": 1 + min(range(5), key=lambda i: likes[int(shown[i].split()[2])])}

    good = FixedLLM(name_least_liked)
    s = scorers.LineupDetector().score(ev, CUT, _fixed_ctx(tmp_path / "good", good))["threads"]
    assert good.calls == 1 and s.items == 4 and s.estimate == 1.0 and s.coverage == 1.0

    def name_most_liked(prompt):
        shown = [line.split(". ", 1)[1] for line in prompt.split("REPLIES:\n")[1].splitlines()]
        return {"ai_reply": 1 + max(range(5), key=lambda i: likes[int(shown[i].split()[2])])}

    s = scorers.LineupDetector().score(ev, CUT, _fixed_ctx(tmp_path / "bad", FixedLLM(name_most_liked)))["threads"]
    assert s.estimate == 0.0
    abstains = FixedLLM({"ai_reply": 9})  # names no reply: scored as naming the most-liked one
    s = scorers.LineupDetector().score(ev, CUT, _fixed_ctx(tmp_path / "none", abstains))["threads"]
    assert s.estimate == 0.0 and s.coverage == 0.0 and s.detail["lineups_named"] == 0


def test_a_failed_lineup_call_is_retried_next_cycle_not_cached(tmp_path):
    ev = _evidence(tmp_path, [[f"reply number {i} with words" for i in range(6)]], [30, 12, 6, 3, 1, 0])
    s = scorers.LineupDetector().score(ev, CUT, _fixed_ctx(tmp_path, FixedLLM(TimeoutError("read timed out"))))
    assert s["threads"].detail["deferred"] == 1 and s["threads"].detail["lineups"] == 0
    retry = FixedLLM({"ai_reply": 2})
    scorers.LineupDetector().score(ev, CUT, _fixed_ctx(tmp_path, retry))
    assert retry.calls == 1


def test_sgu_replies_before_the_cutoff_are_left_out(tmp_path):
    ev = _evidence(tmp_path, [["a reply with words in it"] * 3], [20, 1, 0])
    from watch_threads import sid

    old = SguReply("a-old", sid(CUT - timedelta(days=1)), "p", "great point", "post", 3.0, 10, 0.9, 24, "devtools", None)
    new = SguReply("a-new", sid(CUT + timedelta(days=1)), "p", "great point", "post", 3.0, 10, 0.4, 24, "devtools", None)
    ev.sgu = [old, new]
    assert [r.action_id for r in ev.sgu_after(CUT)] == ["a-new"]
    s = scorers.SlopGate().score(ev, CUT, _ctx(tmp_path)[0])["sgu"]
    assert s.n == 1


def test_rescore_writes_every_instrument_and_reports_failures(tmp_path, monkeypatch):
    ev = _evidence(tmp_path, [["a reply with words in it"] * 3], [20, 1, 0])
    monkeypatch.setattr(scorers.Panel, "version", lambda self: (_ for _ in ()).throw(RuntimeError("panel file gone")))
    ctx, _ = _ctx(tmp_path)
    led, events, errors = scorers.rescore(ev, ctx, tmp_path / "instruments.json", NOW,
                                          instruments=(scorers.SlopGate, scorers.Panel))
    assert set(led["instruments"]) == {"slop_gate"} and errors == {"panel": "RuntimeError: panel file gone"}
    assert {e["event"] for e in events} == {"first seen", "status"}


def test_the_lineup_detector_scores_only_sgu_replies_after_its_cutoff(tmp_path):
    from watch_threads import sid

    texts = [f"reply number {i} with words" for i in range(6)]
    ev = _evidence(tmp_path, [texts], [30, 12, 6, 3, 1, 0])
    root = ev.rolling[0].group
    before = SguReply("a-old", sid(CUT - timedelta(hours=1)), root, "my take on it here", "", 3.0, 10, 0.9, 24, None, None)
    after = SguReply("a-new", sid(CUT + timedelta(days=4)), root, "my take on it here", "", 3.0, 10, 0.4, 24, None, None)
    ev.sgu = [before, after]
    llm = FixedLLM({"ai_reply": 1})
    s = scorers.LineupDetector().score(ev, CUT, _fixed_ctx(tmp_path, llm))["sgu"]
    assert s.n == 1 and llm.calls == 2  # one thread lineup, one _sgu_ lineup; the old reply never asked about
