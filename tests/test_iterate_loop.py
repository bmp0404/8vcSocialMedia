"""The loop end to end with a deterministic fake instrument: runner, gate, ledger, champion, revert, proposers."""

import json

import pytest

from harness.iterate import knobs, ledger, propose
from harness.iterate.fitness import ArmResult, Item, Metric
from harness.iterate.runner import Proposal, run_batch
from harness.iterate.stats import mean_stat


class FakeFitness:
    """20 items in 10 clusters. single_reader scores 1 on every item, everything else 0, so the
    single-reader challenger is a certain win and every other change is a certain tie. `guard_drop`
    makes the guardrail metric fall for single_reader."""

    name = "fake"
    stage = "qualify"
    unit = "post"
    cluster_unit = "account"
    primary = Metric("score", higher_is_better=True)
    knobs = ("qualify.panel", "qualify.model", "qualify.batch")

    def __init__(self, guard_drop: bool = False):
        self.secondaries = (Metric("safety", higher_is_better=True, guardrail=True, margin=0.1),)
        self.guard_drop = guard_drop
        self.calls = 0

    def items(self):
        return [Item(f"i{n}", f"c{n // 2}", None) for n in range(20)]

    def instrument(self):
        return {"dataset": "fake", "dataset_sha256": "0" * 64}

    def estimate_calls(self, config, n_items, repeat=False):
        return n_items

    def run_arm(self, config, items, llm_for, *, seed, repeat):
        self.calls += len(items)
        win = config.get("qualify.panel") == "single_reader" or config.get("qualify.batch") == 1
        safety = 0.0 if (win and self.guard_drop) else 1.0
        return ArmResult(values={it.id: float(win) for it in items},
                         secondary={"safety": {it.id: safety for it in items}})

    def stat(self, items, values, metric):
        return mean_stat([values[it.id] for it in items])


WIN = Proposal("fake", {"qualify.panel": "single_reader"}, "the reader wins", {"kind": "manual", "source": "test"})
TIE = Proposal("fake", {"qualify.model": knobs.GPT_SOL}, "the model ties", {"kind": "manual", "source": "test"})


def test_runner_promotes_the_certain_win_and_keeps_the_tie(tmp_path):
    recs = run_batch([WIN, TIE], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path)
    by = {r["change"]["summary"]: r for r in recs}
    win = next(r for r in recs if "qualify.panel" in r["change"]["params"])
    tie = next(r for r in recs if "qualify.model" in r["change"]["params"])
    assert win["decision"]["status"] == "promoted"
    assert tie["decision"]["status"] == "inconclusive"
    assert win["eval"]["correction"]["family_size"] == 2
    assert win["eval"]["noise_floor"]["value"] == 0.0
    assert win["eval"]["metrics"][0]["delta"]["value"] == 1.0
    assert win["champion"]["config_hash"] == knobs.DEFAULT.hash
    champ, history = ledger.champion(tmp_path)
    assert champ.get("qualify.panel") == "single_reader" and history == [win["id"]]
    saved = json.loads((tmp_path / "champion.json").read_text())
    assert saved["config_hash"] == champ.hash
    assert (tmp_path / "CHANGELOG.md").read_text().count("## exp-") == 2
    assert len(by) == 2


def test_guardrail_blocks_a_promotion_that_cannot_show_non_inferiority(tmp_path):
    (rec,) = run_batch([WIN], {"fake": FakeFitness(guard_drop=True)}, provider="mock", max_calls=1000,
                       directory=tmp_path)
    assert rec["decision"]["status"] == "inconclusive"
    assert "non-inferior on safety" in rec["decision"]["reason"]
    assert ledger.champion(tmp_path)[0] == knobs.DEFAULT


def test_invalid_and_over_budget_proposals_are_recorded_as_pending(tmp_path):
    bad = Proposal("fake", {"action.temperature": 0.7}, "not a knob")
    unmeasured = Proposal("fake", {"orient.narratives": 3}, "fake can't see orient")
    fit = FakeFitness()
    recs = run_batch([WIN, bad, unmeasured, TIE], {"fake": fit}, provider="mock", max_calls=70, directory=tmp_path)
    status = {r["hypothesis"]: (r["decision"]["status"], r["decision"]["reason"]) for r in recs}
    assert status["not a knob"][0] == "pending" and "unknown knob" in status["not a knob"][1]
    assert status["fake can't see orient"][0] == "pending" and "can't measure" in status["fake can't see orient"][1]
    # 60 calls for champion, repeat and the first challenger; the second challenger needs 20 more.
    assert status["the model ties"][0] == "pending" and "not run" in status["the model ties"][1]
    assert fit.calls == 60


def test_revert_restores_the_previous_champion_without_deleting_anything(tmp_path):
    (win,) = run_batch([WIN], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path)
    rec = ledger.revert_record(tmp_path, win["id"], "test revert")
    ledger.write(tmp_path, [rec])
    champ, history = ledger.champion(tmp_path)
    assert champ == knobs.DEFAULT
    assert history == [win["id"], rec["id"]]
    assert {r["id"] for r in ledger.records(tmp_path)} == {win["id"], rec["id"]}


def test_revert_refuses_unknown_or_unpromoted_records(tmp_path):
    (tie,) = run_batch([TIE], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path)
    with pytest.raises(KeyError):
        ledger.revert_record(tmp_path, "exp-missing", "x")
    with pytest.raises(ValueError, match="not promoted"):
        ledger.revert_record(tmp_path, tie["id"], "x")


def test_write_refuses_to_overwrite_a_record(tmp_path):
    (rec,) = run_batch([TIE], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path)
    with pytest.raises(FileExistsError):
        ledger.write(tmp_path, [rec])


FIT_KNOBS = {"fake": FakeFitness.knobs}


def test_validate_rejects_everything_outside_the_registry():
    raw = [
        {"fitness": "fake", "changes": {"qualify.panel": "single_reader"}, "hypothesis": "ok"},
        {"fitness": "nope", "changes": {"qualify.panel": "single_reader"}, "hypothesis": "x"},
        {"fitness": "fake", "changes": {"qualify.panel": "focus_group"}, "hypothesis": "x"},
        {"fitness": "fake", "changes": {"orient.narratives": 3}, "hypothesis": "x"},
        {"fitness": "fake", "changes": {}, "hypothesis": "x"},
        {"fitness": "fake", "changes": {"qualify.panel": "personas"}, "hypothesis": "equals champion"},
        {"fitness": "fake", "changes": {"qualify.model": knobs.GPT_SOL}, "hypothesis": ""},
        {"fitness": "fake", "changes": {"qualify.panel": "single_reader"}, "hypothesis": "dup"},
        "rm -rf /",
    ]
    batch = propose.validate(raw, knobs.DEFAULT, [], FIT_KNOBS, {"kind": "llm"}, limit=3)
    assert [p.changes for p in batch.accepted] == [{"qualify.panel": "single_reader"}]
    reasons = [r.reason for r in batch.rejected]
    assert any("unknown fitness" in r for r in reasons)
    assert any("not one of" in r for r in reasons)
    assert any("can't measure" in r for r in reasons)
    assert any("one or two knobs" in r for r in reasons)
    assert any("equals the champion" in r for r in reasons)
    assert any("needs a hypothesis" in r for r in reasons)
    assert any("duplicate" in r for r in reasons)
    assert any("JSON object" in r for r in reasons)
    assert len(batch.rejected) == len(raw) - 1


def test_validate_skips_what_the_ledger_settled_unless_the_dataset_grew(tmp_path):
    run_batch([TIE], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path)
    recs, cfgs = ledger.records(tmp_path), ledger.configs(tmp_path)
    raw = [{"fitness": "fake", "changes": {"qualify.model": knobs.GPT_SOL}, "hypothesis": "again"}]
    same = propose.validate(raw, knobs.DEFAULT, recs, FIT_KNOBS, {"kind": "rule"}, configs=cfgs,
                            datasets={"fake": "0" * 64})
    assert not same.accepted and "already decided" in same.rejected[0].reason
    grown = propose.validate(raw, knobs.DEFAULT, recs, FIT_KNOBS, {"kind": "rule"}, configs=cfgs,
                             datasets={"fake": "1" * 64})
    assert len(grown.accepted) == 1
    moved = knobs.DEFAULT.with_changes({"qualify.batch": 1})  # a champion that differs on a knob fake reads
    assert len(propose.validate(raw, moved, recs, FIT_KNOBS, {"kind": "rule"}, configs=cfgs).accepted) == 1


def test_rule_proposer_puts_priors_first_and_respects_the_limit():
    batch = propose.RuleProposer([TIE, WIN], limit=2).propose(knobs.DEFAULT, [], FIT_KNOBS)
    assert [p.changes for p in batch.accepted] == [TIE.changes, WIN.changes]
    assert not batch.rejected  # running out of room is not a rejection


def test_llm_proposer_validates_model_output():
    from harness.iterate.llm import Meter, MockMeteredLLM

    llm = MockMeteredLLM(Meter(5), "proposer", lambda s, p: {})
    batch = propose.LLMProposer(llm, [], limit=3).propose(knobs.DEFAULT, [], FIT_KNOBS)
    assert len(batch.accepted) == 1 and batch.accepted[0].proposer["kind"] == "llm"
    assert any("unknown knob" in r.reason or "can't measure" in r.reason for r in batch.rejected)


def test_at_most_one_promotion_per_fitness_per_run(tmp_path):
    batch1 = Proposal("fake", {"qualify.batch": 1}, "batching wins too", {"kind": "manual", "source": "test"})
    recs = run_batch([WIN, batch1], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path)
    statuses = sorted(r["decision"]["status"] for r in recs)
    assert statuses == ["inconclusive", "promoted"]
    loser = next(r for r in recs if r["decision"]["status"] == "inconclusive")
    assert "same fitness" in loser["decision"]["reason"]
    # The two tie exactly, so the winner is fixed by the config hash, not by proposal order.
    again = run_batch([batch1, WIN], {"fake": FakeFitness()}, provider="mock", max_calls=1000,
                      directory=tmp_path / "again")
    first = next(r for r in recs if r["decision"]["status"] == "promoted")["challenger"]["ref"]
    second = next(r for r in again if r["decision"]["status"] == "promoted")["challenger"]["ref"]
    assert first == second


def test_a_failed_confirmation_reverts_the_promotion(tmp_path):
    (win,) = run_batch([WIN], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path)
    assert ledger.champion(tmp_path)[0].get("qualify.panel") == "single_reader"
    confirm = Proposal("fake", WIN.changes, "still wins with the stricter guardrail",
                       {"kind": "confirmation", "source": win["id"]})
    recs = run_batch([confirm], {"fake": FakeFitness(guard_drop=True)}, provider="mock", max_calls=1000,
                     directory=tmp_path, champion=knobs.DEFAULT)
    assert recs[0]["decision"]["status"] == "inconclusive"
    assert recs[1]["proposer"]["kind"] == "revert" and recs[1]["decision"]["by"] == "auto"
    assert ledger.champion(tmp_path)[0] == knobs.DEFAULT


def test_a_passed_confirmation_leaves_the_promotion_in_force(tmp_path):
    (win,) = run_batch([WIN], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path)
    confirm = Proposal("fake", WIN.changes, "still wins", {"kind": "confirmation", "source": win["id"]})
    recs = run_batch([confirm], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path,
                     champion=knobs.DEFAULT)
    assert [r["decision"]["status"] for r in recs] == ["promoted"]
    assert ledger.champion(tmp_path)[0].get("qualify.panel") == "single_reader"


def test_an_explicit_champion_is_only_for_confirmations(tmp_path):
    with pytest.raises(ValueError, match="confirmation"):
        run_batch([WIN], {"fake": FakeFitness()}, provider="mock", max_calls=1000, directory=tmp_path,
                  champion=knobs.DEFAULT)
