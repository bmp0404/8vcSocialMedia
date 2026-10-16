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
    (prom,) = run_batch([Proposal("fake", {"qualify.panel": "single_reader"}, "promoted by hand", {"kind": "manual"})],
                        {"fake": FakeFit()}, provider="mock", max_calls=1000, directory=cfg.ledger_dir, now=clock.t)
    rec = ledger.records(cfg.ledger_dir)[0]
    rec["decision"]["by"] = "human"
    (cfg.ledger_dir / f"{rec['id']}.json").write_text(__import__("json").dumps(rec), encoding="utf-8")
    stack = {**rec, "id": "exp-stack-y", "proposer": {"kind": "stack", "source": rec["id"]},
             "decision": {"status": "stack_failed", "by": "auto", "at": (clock.t + timedelta(minutes=1)).isoformat(),
                          "reason": "worse together"}}
    ledger.write(cfg.ledger_dir, [stack])
    s = make()
    out = s.experiments()
    assert out["stacks"][0]["action"] == "needs a person" and "made by a person" in out["stacks"][0]["note"]
    assert ledger.champion(cfg.ledger_dir)[0].get("qualify.panel") == "single_reader"
