"""Campaign accounting across runs: the weekly promotion cap, the post-proposal confirmation filter,
screening into candidates, and measuring stacked promotions together. Each rule has one right answer."""

from datetime import datetime, timedelta, timezone

import pytest

from harness.iterate import campaign, knobs, ledger
from harness.iterate.fitness import ArmResult, Item, Metric
from harness.iterate.runner import Proposal, run_batch
from harness.iterate.stats import mean_stat

T0 = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


class Fake:
    """20 items in 10 clusters, rooted one hour apart from T0 - 10h. `wins(config)` gives every item the same
    score, so a config that scores higher is a certain promotion and one that scores the same a certain tie.
    By default each of the two changes below adds 1."""

    stage = "qualify"
    unit = "post"
    cluster_unit = "account"
    llm_opinion = False
    primary = Metric("score", higher_is_better=True)

    def __init__(self, name="fake", knobs_=("qualify.panel", "qualify.model", "qualify.batch"), wins=None, safety=None):
        self.name, self.knobs = name, knobs_
        self.secondaries = (Metric("safety", higher_is_better=True, guardrail=True, margin=0.1),
                            Metric("length", higher_is_better=False))
        self.wins = wins or (lambda c: (c.get("qualify.panel") == "single_reader") + (c.get("qualify.batch") == 1))
        self.safety = safety or (lambda c: 1.0)

    def items(self):
        return [Item(f"i{n}", f"c{n // 2}", None, root_at=T0 + timedelta(hours=n - 10)) for n in range(20)]

    def instrument(self):
        return {"dataset": "fake", "dataset_sha256": "0" * 64}

    def estimate_calls(self, config, n_items, repeat=False):
        return n_items

    def run_arm(self, config, items, llm_for, *, seed, repeat):
        win = float(self.wins(config))
        return ArmResult(values={it.id: win for it in items},
                         secondary={"safety": {it.id: self.safety(config) for it in items},
                                    "length": {it.id: 1.0 for it in items}})

    def stat(self, items, values, metric):
        return mean_stat([values[it.id] for it in items])


def P(changes, why="test", kind="manual", fitness="fake", source="test"):
    return Proposal(fitness, changes, why, {"kind": kind, "source": source})


READER = {"qualify.panel": "single_reader"}
BATCH = {"qualify.batch": 1}
BATCH_CHANGE = {"qualify.batch": (0, 1)}


def BATCH_ONLY(c):
    return c.get("qualify.batch") == 1


# ---------------------------------------------------------------- the weekly cap


def _rec(rid, kind, changes, status="promoted", at=T0):
    return {"id": rid, "created_at": at.isoformat(), "proposer": {"kind": kind},
            "change": {"params": {k: {"from": f, "to": t} for k, (f, t) in changes.items()}},
            "decision": {"status": status, "at": at.isoformat()}}


PANEL_ON = {"qualify.panel": ("personas", "single_reader")}
PANEL_OFF = {"qualify.panel": ("single_reader", "personas")}


def test_cap_is_open_with_no_promotions_and_closes_for_exactly_seven_days():
    assert campaign.weekly_cap([], T0) == campaign.Cap(True)
    rec = _rec("exp-a", "manual", PANEL_ON)
    assert campaign.weekly_cap([rec], T0 + timedelta(days=6, hours=23)) == campaign.Cap(False, "exp-a", T0 + timedelta(days=7))
    assert campaign.weekly_cap([rec], T0 + timedelta(days=7)).open
    with pytest.raises(ValueError, match="timezone"):
        campaign.weekly_cap([rec], datetime(2026, 10, 20))


def test_reverts_and_confirmations_of_changes_in_force_never_close_the_window():
    early = T0 - timedelta(days=10)
    recs = [_rec("exp-a", "manual", PANEL_ON, at=early), _rec("exp-conf", "confirmation", PANEL_ON),
            _rec("exp-rev", "revert", PANEL_OFF, at=T0 + timedelta(minutes=1)),
            _rec("exp-inc", "manual", BATCH_CHANGE, "inconclusive"), _rec("exp-cand", "queue", BATCH_CHANGE, "candidate")]
    assert campaign.weekly_cap(recs, T0 + timedelta(hours=1)).open


def test_a_confirmation_that_reapplies_a_reverted_change_counts():
    early = T0 - timedelta(days=10)
    recs = [_rec("exp-a", "manual", PANEL_ON, at=early), _rec("exp-rev", "revert", PANEL_OFF, at=early),
            _rec("exp-conf", "confirmation", PANEL_ON)]
    assert [r["id"] for r in campaign.counted_promotions(recs)] == ["exp-a", "exp-conf"]
    assert campaign.weekly_cap(recs, T0 + timedelta(hours=1)).holder == "exp-conf"


def test_a_second_winner_within_the_week_is_held_and_promoted_after_it(tmp_path):
    (first,) = run_batch([P(READER)], {"fake": Fake()}, provider="mock", max_calls=1000, directory=tmp_path)
    assert first["decision"]["status"] == "promoted"
    at = campaign.decided_at(first)
    (held,) = run_batch([P(BATCH)], {"fake": Fake()}, provider="mock", max_calls=1000, directory=tmp_path,
                        now=at + timedelta(days=1))
    assert held["decision"]["status"] == "inconclusive"
    assert first["id"] in held["decision"]["reason"] and "rolling week" in held["decision"]["reason"]
    assert ledger.champion(tmp_path)[0].get("qualify.batch") == 0
    (later,) = run_batch([P(BATCH)], {"fake": Fake()}, provider="mock", max_calls=1000, directory=tmp_path,
                         now=at + timedelta(days=7, minutes=1))
    assert later["decision"]["status"] == "promoted"
    assert ledger.champion(tmp_path)[0].get("qualify.batch") == 1


def test_a_confirmation_inside_the_window_is_not_held(tmp_path):
    (first,) = run_batch([P(READER)], {"fake": Fake()}, provider="mock", max_calls=1000, directory=tmp_path)
    recs = run_batch([P(READER, "still wins", kind="confirmation", source=first["id"])], {"fake": Fake()}, provider="mock",
                     max_calls=1000, directory=tmp_path, champion=knobs.DEFAULT,
                     now=campaign.decided_at(first) + timedelta(hours=1))
    assert [r["decision"]["status"] for r in recs] == ["promoted"]


def test_two_fitnesses_winning_in_one_run_promote_only_the_smaller_p_value(tmp_path):
    # "other" wins on 6 of 10 clusters, "fake" on all 10, so fake's p-value is smaller whatever the order.
    partial = Fake("other", ("qualify.batch",), BATCH_ONLY)
    partial.run_arm = lambda config, items, llm_for, *, seed, repeat: ArmResult(
        values={it.id: float(BATCH_ONLY(config) and int(it.cluster[1:]) < 6) for it in items},
        secondary={"safety": {it.id: 1.0 for it in items}, "length": {it.id: 1.0 for it in items}})
    for order in ([P(READER), P(BATCH, fitness="other")], [P(BATCH, fitness="other"), P(READER)]):
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        recs = run_batch(order, {"fake": Fake(), "other": partial}, provider="mock", max_calls=1000, directory=d)
        by = {r["eval"]["fitness"]: r["decision"] for r in recs}
        assert by["fake"]["status"] == "promoted" and by["other"]["status"] == "inconclusive"
        assert "one change per rolling week" in by["other"]["reason"]


# ---------------------------------------------------------------- confirmation on later items


def test_rooted_after_keeps_only_items_strictly_after_the_cutoff():
    items = Fake().items()
    after = campaign.rooted_after(items, T0)
    assert [it.id for it in after] == [f"i{n}" for n in range(11, 20)]
    assert [it.id for it in campaign.rooted_by(items, T0)] == [f"i{n}" for n in range(0, 11)]
    assert not set(it.id for it in after) & set(it.id for it in campaign.rooted_by(items, T0))


def test_rooted_after_refuses_undated_items_and_naive_cutoffs():
    with pytest.raises(ValueError, match="no root time"):
        campaign.rooted_after([Item("x", "x", None)], T0)
    with pytest.raises(ValueError, match="timezone"):
        campaign.rooted_after(Fake().items(), datetime(2026, 10, 16))


def test_restricted_filters_items_and_only_tightens_guardrails():
    r = campaign.Restricted(Fake(), after=T0, guardrails={"safety": 0.05, "length": 0.2})
    assert len(r.items()) == 9 and all(it.root_at > T0 for it in r.items())
    margins = {m.name: (m.guardrail, m.margin) for m in r.secondaries}
    assert margins == {"safety": (True, 0.05), "length": (True, 0.2)}
    assert r.instrument()["items_rooted_after"] == T0.isoformat()
    with pytest.raises(ValueError, match="looser"):
        campaign.Restricted(Fake(), guardrails={"safety": 0.5})
    with pytest.raises(ValueError, match="no secondary metric"):
        campaign.Restricted(Fake(), guardrails={"likes": 0.1})
    with pytest.raises(ValueError, match="not both"):
        campaign.Restricted(Fake(), after=T0, by=T0)


def test_a_confirmation_runs_only_on_later_items(tmp_path):
    fit = campaign.Restricted(Fake(), after=T0)
    (rec,) = run_batch([P(READER)], {"fake": fit}, provider="mock", max_calls=1000, directory=tmp_path)
    assert rec["eval"]["n"] == 9 and rec["eval"]["instrument"]["items_rooted_after"] == T0.isoformat()


# ---------------------------------------------------------------- screening


def test_a_screen_pass_is_a_candidate_and_leaves_the_champion_alone(tmp_path):
    (rec,) = run_batch([P(READER, kind="queue")], {"fake": Fake()}, provider="mock", max_calls=1000,
                       directory=tmp_path, mode="screen")
    assert rec["decision"]["status"] == "candidate"
    assert "confirmation on items rooted after" in rec["decision"]["reason"]
    assert ledger.champion(tmp_path)[0] == knobs.DEFAULT
    assert campaign.weekly_cap(ledger.records(tmp_path), T0 + timedelta(days=1)).open


def test_run_batch_refuses_an_unknown_mode_and_a_stack_without_a_baseline(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        run_batch([P(READER)], {"fake": Fake()}, provider="mock", max_calls=10, directory=tmp_path, mode="yolo")
    with pytest.raises(ValueError, match="baseline"):
        run_batch([P(READER, kind="stack")], {"fake": Fake()}, provider="mock", max_calls=10, directory=tmp_path,
                  mode="stack")


# ---------------------------------------------------------------- stacked promotions


def _stacked_ledger(tmp_path, fake):
    """READER promoted on `fake`; a week later BATCH promoted on a fitness that reads only qualify.batch,
    so `fake` never scored the two together."""
    (first,) = run_batch([P(READER)], {"fake": fake}, provider="mock", max_calls=1000, directory=tmp_path)
    (second,) = run_batch([P(BATCH, fitness="other")], {"other": Fake("other", ("qualify.batch",), BATCH_ONLY)},
                          provider="mock", max_calls=1000, directory=tmp_path,
                          now=campaign.decided_at(first) + timedelta(days=8))
    assert second["decision"]["status"] == "promoted"
    return first, second


def _fit_knobs(*fits):
    return {f.name: f.knobs for f in fits}


def test_unmeasured_finds_the_stack_and_its_baseline(tmp_path):
    fake, other = Fake(), Fake("other", ("qualify.batch",))
    first, second = _stacked_ledger(tmp_path, fake)
    champ, _ = ledger.champion(tmp_path)
    stacks = campaign.unmeasured(ledger.records(tmp_path), ledger.configs(tmp_path), _fit_knobs(fake, other), champ)
    assert [s.fitness for s in stacks] == ["fake"]
    (s,) = stacks
    assert s.latest == second["id"] and s.latest_by == "auto"
    assert s.baseline.get("qualify.batch") == 0 and s.baseline.get("qualify.panel") == "single_reader"
    assert s.champion == champ


def test_a_stack_check_that_holds_marks_the_combination_measured(tmp_path):
    fake, other = Fake(), Fake("other", ("qualify.batch",))
    _, second = _stacked_ledger(tmp_path, fake)
    champ, _ = ledger.champion(tmp_path)
    (s,) = campaign.unmeasured(ledger.records(tmp_path), ledger.configs(tmp_path), _fit_knobs(fake, other), champ)
    changes = {k: v["to"] for k, v in knobs.diff(s.baseline, s.champion).items()}
    (rec,) = run_batch([P(changes, kind="stack", source=s.latest)], {"fake": fake}, provider="mock",
                       max_calls=1000, directory=tmp_path, champion=s.baseline, mode="stack")
    assert rec["decision"]["status"] == "stack_ok"
    assert ledger.champion(tmp_path)[0] == champ  # a stack record never changes the champion
    assert campaign.unmeasured(ledger.records(tmp_path), ledger.configs(tmp_path), _fit_knobs(fake, other), champ) == []


def test_a_stack_check_that_shows_the_combination_worse_fails(tmp_path):
    # On this instrument either change alone wins, but both together lose everything.
    fake = Fake(wins=lambda c: (c.get("qualify.panel") == "single_reader") != (c.get("qualify.batch") == 1))
    other = Fake("other", ("qualify.batch",), BATCH_ONLY)
    _stacked_ledger(tmp_path, fake)
    champ, _ = ledger.champion(tmp_path)
    (s,) = campaign.unmeasured(ledger.records(tmp_path), ledger.configs(tmp_path), _fit_knobs(fake, other), champ)
    changes = {k: v["to"] for k, v in knobs.diff(s.baseline, s.champion).items()}
    (rec,) = run_batch([P(changes, kind="stack", source=s.latest)], {"fake": fake}, provider="mock",
                       max_calls=1000, directory=tmp_path, champion=s.baseline, mode="stack")
    assert rec["decision"]["status"] == "stack_failed"
    assert s.latest in rec["decision"]["reason"]


def test_a_single_promotion_measured_on_its_own_fitness_is_not_a_stack(tmp_path):
    fake = Fake()
    run_batch([P(READER)], {"fake": fake}, provider="mock", max_calls=1000, directory=tmp_path)
    champ, _ = ledger.champion(tmp_path)
    assert campaign.unmeasured(ledger.records(tmp_path), ledger.configs(tmp_path), _fit_knobs(fake), champ) == []


def test_limit_clusters_keeps_the_earliest_or_latest_whole_clusters():
    items = Fake().items()  # clusters c0..c9, two items each, rooted in order
    early = campaign.limit_clusters(items, 3, earliest=True)
    late = campaign.limit_clusters(items, 3, earliest=False)
    assert sorted({it.cluster for it in early}) == ["c0", "c1", "c2"] and len(early) == 6
    assert sorted({it.cluster for it in late}) == ["c7", "c8", "c9"]
    r = campaign.Restricted(Fake(), after=T0, max_clusters=2)
    assert sorted({it.cluster for it in r.items()}) == ["c5", "c6"]


def test_a_guardrail_whose_instrument_is_report_only_stops_blocking(tmp_path):
    unsafe = Fake(safety=lambda c: 0.0 if c.get("qualify.panel") == "single_reader" else 1.0)
    (blocked,) = run_batch([P(READER)], {"fake": unsafe}, provider="mock", max_calls=1000, directory=tmp_path / "a")
    assert blocked["decision"]["status"] == "inconclusive" and "safety" in blocked["decision"]["reason"]
    loose = campaign.Restricted(unsafe, untrusted={"safety": "its instrument is report-only"})
    (passed,) = run_batch([P(READER)], {"fake": loose}, provider="mock", max_calls=1000, directory=tmp_path / "b")
    assert passed["decision"]["status"] == "promoted"
    assert passed["eval"]["instrument"]["guardrails_dropped"] == {"safety": "its instrument is report-only"}
    with pytest.raises(ValueError, match="no secondary metric"):
        campaign.Restricted(unsafe, untrusted={"likes": "x"})


def test_a_stack_check_fails_when_the_combination_fails_on_more_items(tmp_path):
    class Flaky(Fake):
        def run_arm(self, config, items, llm_for, *, seed, repeat):
            res = super().run_arm(config, items, llm_for, seed=seed, repeat=repeat)
            if config.get("qualify.batch") == 1 and config.get("qualify.panel") == "single_reader":
                res.failures = {it.id: "crashed" for it in items[:8]}
                for k in list(res.values)[:8]:
                    res.values.pop(k)
                    for m in res.secondary.values():
                        m.pop(k)
            return res

    fake, other = Flaky(), Fake("other", ("qualify.batch",), BATCH_ONLY)
    _stacked_ledger(tmp_path, fake)
    champ, _ = ledger.champion(tmp_path)
    (s,) = campaign.unmeasured(ledger.records(tmp_path), ledger.configs(tmp_path), _fit_knobs(fake, other), champ)
    changes = {k: v["to"] for k, v in knobs.diff(s.baseline, s.champion).items()}
    (rec,) = run_batch([P(changes, kind="stack", source=s.latest)], {"fake": fake}, provider="mock",
                       max_calls=1000, directory=tmp_path, champion=s.baseline, mode="stack")
    assert rec["decision"]["status"] == "stack_failed" and "failed on 8 more items" in rec["decision"]["reason"]
