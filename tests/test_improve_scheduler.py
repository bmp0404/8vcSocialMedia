"""The scheduler: one cycle runs every step, a second cycle or a restart repeats none of the finished
work, a failed step doesn't stop the others, and the pid file and SIGTERM behave."""

import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from watch_threads import Threads

from harness.improve import scheduler as sch
from harness.iterate import ledger
from harness.iterate.fitness import ArmResult, Item, Metric
from harness.iterate.stats import mean_stat

T0 = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


class FakeFit:
    """80 items in 40 clusters rooted an hour apart around T0. The reader change wins every item."""

    name, stage, unit, cluster_unit = "fake", "qualify", "post", "account"
    llm_opinion = False
    primary = Metric("score", higher_is_better=True)
    secondaries = (Metric("safety", higher_is_better=True, guardrail=True, margin=0.1),)
    knobs = ("qualify.panel", "qualify.batch")

    def items(self):
        return [Item(f"i{n}", f"c{n // 2}", None, root_at=T0 + timedelta(hours=n - 40)) for n in range(80)]

    def instrument(self):
        return {"dataset": "fake", "dataset_sha256": "0" * 64}

    def estimate_calls(self, config, n_items, repeat=False):
        return n_items

    def run_arm(self, config, items, llm_for, *, seed, repeat):
        win = float(config.get("qualify.panel") == "single_reader")
        return ArmResult(values={it.id: win for it in items}, secondary={"safety": {it.id: 1.0 for it in items}})

    def stat(self, items, values, metric):
        return mean_stat([values[it.id] for it in items])


SPEC = '''
id = "reader"
proposed_at = 2026-10-21T12:00:00Z
proposed_by = "agent:test"
profile = "wnba"
fitness = "fake"
hypothesis = "One reader beats the persona panel."

[change]
kind = "knobs"
knobs = { "qualify.panel" = "single_reader" }
'''


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def setup(tmp_path):
    t = Threads(tmp_path / "threads")
    t.thread(T0 - timedelta(days=3), timedelta(hours=50), [12, 1, 0], texts=["one reply here", "two replies here",
                                                                             "three replies here"])
    t.write()
    (tmp_path / "queue").mkdir()
    (tmp_path / "queue" / "reader.toml").write_text(SPEC, encoding="utf-8")
    cfg = sch.ImproveConfig(provider="mock", out_dir=tmp_path / "improve", threads_dir=tmp_path / "threads",
                            watch_dir=tmp_path / "watch", hot_dir=tmp_path / "hot", outcomes_db=tmp_path / "none.db",
                            queue_dir=tmp_path / "queue", rolling_dir=tmp_path / "rolling", report_hour=11)
    facts_calls = []

    def facts(profile, provider):
        facts_calls.append(profile)
        return f"[facts] {profile}: wrote 3 facts"

    clock = Clock(T0 + timedelta(minutes=1))

    def make():
        return sch.Scheduler(cfg, clock=clock, echo=False, facts_runner=facts, catalog={"fake": FakeFit},
                             build=lambda fitness, profile, threads=(): FakeFit())
    return cfg, make, facts_calls, clock


def _records(cfg):
    return ledger.records(cfg.ledger_dir)


def test_one_cycle_runs_every_step(setup):
    cfg, make, facts_calls, _ = setup
    make().run_cycle()
    assert facts_calls == ["wnba", "devtools"]
    recs = _records(cfg)
    assert [(r["proposer"]["phase"], r["decision"]["status"]) for r in recs] == [("screen", "candidate")]
    assert (cfg.rolling_dir / "manifest.json").is_file() and cfg.instruments_path.is_file()
    report = (cfg.out_dir / "report-2026-10-21.md").read_text(encoding="utf-8")
    assert "## Needs a person" in report and "reader" in report and "candidate" in report


def test_later_cycles_and_a_restart_repeat_nothing_finished(setup):
    cfg, make, facts_calls, clock = setup
    first = make()
    first.run_cycle()
    clock.t += timedelta(hours=6)
    first.run_cycle()  # confirms on the items rooted after the spec was first seen
    phases = [(r["proposer"]["phase"], r["decision"]["status"]) for r in _records(cfg)]
    assert phases == [("screen", "candidate"), ("confirm", "promoted")]
    assert ledger.champion(cfg.ledger_dir)[0].get("qualify.panel") == "single_reader"
    confirm = _records(cfg)[1]
    assert confirm["eval"]["instrument"]["items_rooted_after"] == (T0 + timedelta(minutes=1)).isoformat()
    report_mtime = (cfg.out_dir / "report-2026-10-21.md").stat().st_mtime_ns

    clock.t += timedelta(hours=1)
    restarted = make()  # a new process: state comes from out/improve/state.db and the ledger
    restarted.run_cycle()
    assert len(_records(cfg)) == 2
    assert facts_calls == ["wnba", "devtools"]  # once per profile per day
    assert (cfg.out_dir / "report-2026-10-21.md").stat().st_mtime_ns == report_mtime
    clock.t += timedelta(days=1)
    restarted.run_cycle()
    assert facts_calls == ["wnba", "devtools", "wnba", "devtools"]
    assert (cfg.out_dir / "report-2026-10-22.md").is_file() and len(_records(cfg)) == 2


def test_a_failed_step_is_recorded_and_retried_while_the_rest_runs(setup, tmp_path):
    cfg, make, facts_calls, clock = setup
    broken = {"devtools"}

    def facts(profile, provider):
        facts_calls.append(profile)
        if profile in broken:
            raise RuntimeError("ESPN timed out")
        return "ok"

    s = sch.Scheduler(cfg, clock=clock, echo=False, facts_runner=facts, catalog={"fake": FakeFit},
                      build=lambda fitness, profile, threads=(): FakeFit())
    s.run_cycle()
    assert facts_calls == ["wnba", "devtools"] and len(_records(cfg)) == 1
    report = (cfg.out_dir / "report-2026-10-21.md").read_text(encoding="utf-8")
    assert "The facts step failed: RuntimeError: ESPN timed out" in report
    broken.clear()
    s.run_cycle()
    assert facts_calls == ["wnba", "devtools", "devtools"]


def test_a_second_loop_refuses_to_start_and_the_pid_file_is_released(setup):
    cfg, make, _, _ = setup
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.out_dir / "improve.pid").write_text(str(os.getppid()), encoding="utf-8")
    with pytest.raises(sch.AlreadyRunning):
        make().run()
    (cfg.out_dir / "improve.pid").unlink()
    cfg.once = True
    assert make().run() == "one cycle done"
    assert not (cfg.out_dir / "improve.pid").exists()


def test_sigterm_stops_the_loop_between_cycles(setup):
    cfg, make, _, _ = setup
    loop = sch.Scheduler(cfg, echo=False, facts_runner=lambda p, q: "ok", catalog={"fake": FakeFit},
                         build=lambda fitness, profile, threads=(): FakeFit())

    def term_after_first_cycle():
        state = sch.State(cfg.out_dir / "state.db")
        for _ in range(600):
            if state.last_finished() is not None:
                break
            time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=term_after_first_cycle, daemon=True)
    killer.start()
    assert loop.run() == "SIGTERM"
    killer.join(5)
    assert not (cfg.out_dir / "improve.pid").exists()
    assert sch.State(cfg.out_dir / "state.db").last_finished() is not None


def test_a_cycle_cut_short_by_a_signal_does_not_count_as_finished(setup):
    cfg, make, _, clock = setup
    s = make()
    s.stop.set()  # as if SIGTERM arrived before the first step
    s.run_cycle()
    assert s.state.last_finished() is None


def test_a_phase_waits_while_a_model_it_needs_is_down(setup):
    cfg, make, _, clock = setup
    probed = []

    def probe(models):
        probed.append(set(models))
        return {"gpt-x": "RateLimitError: usage limit reached"}

    s = sch.Scheduler(cfg, clock=clock, echo=False, facts_runner=lambda p, q: "ok", catalog={"fake": FakeFit},
                      build=lambda fitness, profile, threads=(): FakeFit(), probe=probe)
    s.run_cycle()
    assert _records(cfg) == [] and probed
    report = (cfg.out_dir / "report-2026-10-21.md").read_text(encoding="utf-8")
    assert "waiting for gpt-x (RateLimitError: usage limit reached)" in report


def test_a_change_the_ledger_already_decided_on_the_same_items_is_not_screened_again(setup):
    from harness.iterate.runner import Proposal, run_batch

    cfg, make, _, clock = setup
    run_batch([Proposal("fake", {"qualify.panel": "single_reader"}, "decided by hand", {"kind": "manual"})],
              {"fake": FakeFit()}, provider="mock", max_calls=1000, directory=cfg.ledger_dir, mode="screen")
    make().run_cycle()
    assert [r["proposer"]["kind"] for r in _records(cfg)] == ["manual"]


def test_needed_models_are_the_named_judges_and_the_model_knobs_of_both_arms():
    from harness.iterate import knobs

    class Lineup(FakeFit):
        knobs = ("action.reply.model", "action.reply.generation")

        def instrument(self):
            return {"detector_model": "judge-a", "veto_model": "judge-b", "dataset": "x"}

    challenger = knobs.DEFAULT.with_changes({"action.reply.model": knobs.OPUS55})
    assert sch.Scheduler.needed_models(Lineup(), [knobs.DEFAULT, challenger]) == {
        "judge-a", "judge-b", knobs.SONNET, knobs.OPUS55}


def _screened(cfg, make, clock):
    make().run_cycle()
    clock.t += timedelta(hours=6)
    assert [r["decision"]["status"] for r in _records(cfg)] == ["candidate"]


def test_a_confirmation_is_held_while_the_weekly_cap_is_closed(setup):
    from harness.iterate.runner import Proposal, run_batch

    cfg, make, _, clock = setup
    _screened(cfg, make, clock)
    class BatchWins(FakeFit):
        def run_arm(self, config, items, llm_for, *, seed, repeat):
            win = float(config.get("qualify.batch") == 1)
            return ArmResult(values={it.id: win for it in items}, secondary={"safety": {it.id: 1.0 for it in items}})

    (other,) = run_batch([Proposal("fake", {"qualify.batch": 1}, "an unrelated promotion", {"kind": "manual"})],
                         {"fake": BatchWins()}, provider="mock", max_calls=1000, directory=cfg.ledger_dir, now=clock.t)
    assert other["decision"]["status"] == "promoted"
    s = make()
    out = s.experiments()
    (entry,) = out["specs"]
    assert entry["state"] == "held" and "weekly cap" in entry["note"]
    assert [r["proposer"].get("phase") for r in _records(cfg)] == ["screen", None]


def test_a_report_only_primary_holds_its_fitness_and_a_report_only_guardrail_stops_blocking(setup, monkeypatch):
    from harness.improve import instruments as led

    cfg, make, _, clock = setup
    monkeypatch.setattr(sch, "RULERS", {"fake": {"primary": "detector", "guardrails": {"safety": "veto"}}})
    ledger_file = {"instruments": {"detector": {"status": "report_only", "reason": "includes chance"},
                                   "veto": {"status": "gates", "reason": "n < 20"}}}
    led.save(ledger_file, cfg.instruments_path)
    s = make()
    (entry,) = s.experiments()["specs"]
    assert entry["state"] == "held" and "detector is report-only" in entry["note"] and _records(cfg) == []
    ledger_file["instruments"]["detector"]["status"] = "gates"
    ledger_file["instruments"]["veto"]["status"] = "report_only"
    led.save(ledger_file, cfg.instruments_path)
    (entry,) = make().experiments()["specs"]
    assert entry["state"] == "ran"
    assert _records(cfg)[0]["eval"]["instrument"]["guardrails_dropped"] == {"safety": "veto is report-only (n < 20)"}


def test_a_phase_that_never_runs_goes_stale_after_three_tries(setup):
    cfg, make, _, clock = setup

    class Few(FakeFit):  # three items: the runner records "pending, only 3 items" every time
        def items(self):
            return super().items()[38:41]

    s = sch.Scheduler(cfg, clock=clock, echo=False, facts_runner=lambda p, q: "ok", catalog={"fake": FakeFit},
                      build=lambda fitness, profile, threads=(): Few())
    for _ in range(4):
        s.experiments()
        clock.t += timedelta(hours=1)
    assert [r["decision"]["status"] for r in _records(cfg)] == ["pending"] * 3
    (entry,) = s.experiments()["specs"]
    assert entry["state"] == "stale" and "didn't run 3 times" in entry["note"]


def test_the_cutoff_is_the_proposal_time_when_the_loop_saw_the_spec_first(setup):
    cfg, make, _, clock = setup
    clock.t = T0 - timedelta(minutes=5)  # the file landed five minutes before its own proposed_at
    make().experiments()
    (rec,) = _records(cfg)
    assert rec["eval"]["instrument"]["items_rooted_by"] == T0.isoformat()


def test_an_edited_spec_is_screened_again_instead_of_confirmed(setup):
    cfg, make, _, clock = setup
    _screened(cfg, make, clock)
    edited = SPEC.replace('"qualify.panel" = "single_reader"', '"qualify.batch" = 1')
    (cfg.queue_dir / "reader.toml").write_text(edited, encoding="utf-8")
    make().experiments()
    assert [(r["proposer"]["phase"], r["proposer"]["spec_digest"] != _records(cfg)[0]["proposer"]["spec_digest"])
            for r in _records(cfg)] == [("screen", False), ("screen", True)]


def test_a_failed_stack_check_left_unreverted_is_reverted_next_cycle(setup):
    from harness.iterate.runner import Proposal, run_batch

    cfg, make, _, clock = setup
    (cfg.queue_dir / "reader.toml").unlink()
    (prom,) = run_batch([Proposal("fake", {"qualify.panel": "single_reader"}, "promoted by the loop", {"kind": "queue"})],
                        {"fake": FakeFit()}, provider="mock", max_calls=1000, directory=cfg.ledger_dir, now=clock.t)
    stack = {**prom, "id": "exp-stack-x", "proposer": {"kind": "stack", "source": prom["id"]},
             "decision": {"status": "stack_failed", "by": "auto", "at": (clock.t + timedelta(minutes=1)).isoformat(),
                          "reason": "worse together"}}
    ledger.write(cfg.ledger_dir, [stack])  # the crash came before the revert was written
    out = make().experiments()
    assert out["stacks"][0]["action"] == "reverted"
    assert ledger.champion(cfg.ledger_dir)[0].get("qualify.panel") == "personas"
    assert make().experiments()["stacks"] == []  # done once


def test_a_failed_stack_check_on_a_persons_promotion_asks_a_person(setup):
    from harness.iterate.runner import Proposal, run_batch

    cfg, make, _, clock = setup
    (cfg.queue_dir / "reader.toml").unlink()
    # `harness iterate run` by hand: the gate decides ("by": "auto"), but a person started the run.
    (rec,) = run_batch([Proposal("fake", {"qualify.panel": "single_reader"}, "promoted by hand", {"kind": "manual"})],
                       {"fake": FakeFit()}, provider="mock", max_calls=1000, directory=cfg.ledger_dir, now=clock.t)
    assert rec["decision"]["by"] == "auto"
    stack = {**rec, "id": "exp-stack-y", "proposer": {"kind": "stack", "source": rec["id"]},
             "decision": {"status": "stack_failed", "by": "auto", "at": (clock.t + timedelta(minutes=1)).isoformat(),
                          "reason": "worse together"}}
    ledger.write(cfg.ledger_dir, [stack])
    s = make()
    out = s.experiments()
    assert out["stacks"][0]["action"] == "needs a person" and "started by a person" in out["stacks"][0]["note"]
    assert ledger.champion(cfg.ledger_dir)[0].get("qualify.panel") == "single_reader"
    clock.t += timedelta(days=2)
    assert make().experiments()["stacks"] == []  # asked on the day it failed, not every day after


class Many(FakeFit):
    """300 items in 150 clusters rooted around T0: more than any phase may use."""

    def items(self):
        return [Item(f"m{n}", f"k{n // 2:03d}", None, root_at=T0 + timedelta(hours=n - 150)) for n in range(300)]


def test_a_phase_uses_at_most_its_cluster_cap_and_shrinks_to_fit_its_budget(setup):
    from harness.improve import queue as q

    cfg, make, _, clock = setup
    s = sch.Scheduler(cfg, clock=clock, echo=False, facts_runner=lambda p, x: "ok", catalog={"fake": FakeFit},
                      build=lambda fitness, profile, threads=(): Many())
    s.experiments()
    (screen,) = _records(cfg)
    assert screen["eval"]["clusters"] == q.SCREEN_CLUSTERS  # FakeFit costs 1 call per item, 2 items a cluster
    assert screen["eval"]["instrument"]["max_clusters"] == q.SCREEN_CLUSTERS
    clock.t += timedelta(hours=1)
    small = SPEC.replace("[change]", '[budget]\nconfirm_calls = 150\n\n[change]')
    (cfg.queue_dir / "reader.toml").write_text(small, encoding="utf-8")  # a new digest: screened again first
    s.experiments()
    s.experiments()
    confirm = [r for r in _records(cfg) if r["proposer"]["phase"] == "confirm"][-1]
    # 150 calls cover 25 clusters (3 arms x 2 items x 25); the first ones rooted after the cutoff.
    assert confirm["eval"]["clusters"] == 25 and confirm["eval"]["instrument"]["max_clusters"] == 25


def test_an_edited_spec_gets_its_own_first_seen(setup):
    cfg, make, _, clock = setup
    s = make()
    s.experiments()
    clock.t += timedelta(hours=3)
    edited = SPEC.replace('"qualify.panel" = "single_reader"', '"qualify.batch" = 1')
    (cfg.queue_dir / "reader.toml").write_text(edited, encoding="utf-8")
    s.experiments()
    second = _records(cfg)[-1]
    assert second["eval"]["instrument"]["items_rooted_by"] == clock.t.isoformat()


def test_a_report_only_primary_also_holds_a_due_stack_check(setup, monkeypatch):
    from harness.improve import instruments as led
    from harness.iterate.runner import Proposal, run_batch

    cfg, make, _, clock = setup
    (cfg.queue_dir / "reader.toml").unlink()
    monkeypatch.setattr(sch, "RULERS", {"fake": {"primary": "detector"}})
    led.save({"instruments": {"detector": {"status": "report_only", "reason": "includes chance"}}}, cfg.instruments_path)

    class Other(FakeFit):
        name, knobs = "other", ("qualify.panel",)

    run_batch([Proposal("other", {"qualify.panel": "single_reader"}, "moves a shared knob", {"kind": "queue"})],
              {"other": Other()}, provider="mock", max_calls=1000, directory=cfg.ledger_dir, now=clock.t)
    s = sch.Scheduler(cfg, clock=clock, echo=False, facts_runner=lambda p, x: "ok",
                      catalog={"fake": FakeFit, "other": Other}, build=lambda fitness, profile, threads=(): FakeFit())
    out = s.experiments()
    assert [st["action"] for st in out["stacks"]] == ["deferred"] and "detector is report-only" in out["stacks"][0]["note"]
    assert [r["proposer"]["kind"] for r in _records(cfg)] == ["queue"]


def test_a_crash_revert_is_put_to_a_person_once_the_value_has_changed(setup):
    from harness.iterate.runner import Proposal, run_batch

    cfg, make, _, clock = setup
    (cfg.queue_dir / "reader.toml").unlink()
    (prom,) = run_batch([Proposal("fake", {"qualify.panel": "single_reader"}, "by the loop", {"kind": "queue"})],
                        {"fake": FakeFit()}, provider="mock", max_calls=1000, directory=cfg.ledger_dir, now=clock.t)
    stack = {**prom, "id": "exp-stack-z", "proposer": {"kind": "stack", "source": prom["id"]},
             "decision": {"status": "stack_failed", "by": "auto", "at": (clock.t + timedelta(minutes=1)).isoformat(),
                          "reason": "worse together"}}
    ledger.write(cfg.ledger_dir, [stack])
    # Before the restart, a person set the same knob to a third value.
    later = {**prom, "id": "exp-later", "proposer": {"kind": "manual"},
             "change": {**prom["change"], "params": {"qualify.panel": {"from": "single_reader", "to": "personas"}}},
             "decision": {**prom["decision"], "at": (clock.t + timedelta(minutes=2)).isoformat()}}
    ledger.write(cfg.ledger_dir, [later])
    out = make().experiments()
    assert out["stacks"][0]["action"] == "needs a person" and "changed since" in out["stacks"][0]["note"]


def test_rulers_name_real_instruments_and_real_guardrails():
    from harness.improve import scorers
    from harness.iterate.fitness.action import ActionLineup

    names = {c.name for c in scorers.ALL}
    guards = {m.name for m in ActionLineup.secondaries if m.guardrail}
    for fitness, spec in sch.RULERS.items():
        assert fitness in sch.default_catalog()
        assert spec["primary"] in names
        assert set(spec["guardrails"]) <= guards and set(spec["guardrails"].values()) <= names


def test_a_step_a_dead_process_left_running_is_marked_interrupted_and_run_again(setup):
    cfg, make, _, clock = setup
    s = make()
    s.state.start_step("experiments:99", "experiments", 99, clock.t)  # the process died here
    s.run_cycle()
    rows = {r["key"]: r["status"] for r in s.state.steps_since(None)}
    assert rows["experiments:99"] == "interrupted" and len(_records(cfg)) == 1
    report = (cfg.out_dir / "report-2026-10-21.md").read_text(encoding="utf-8")
    assert "stopped in the middle of the experiments step" in report
