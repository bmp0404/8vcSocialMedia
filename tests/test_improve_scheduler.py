"""The scheduler: one cycle runs every step, a second cycle or a restart repeats none of the finished
work, a failed step doesn't stop the others, and the pid file and SIGTERM behave."""

import os
import signal
import threading
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
    timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        assert loop.run() == "SIGTERM"
    finally:
        timer.cancel()
    assert not (cfg.out_dir / "improve.pid").exists()
    assert sch.State(cfg.out_dir / "state.db").last_finished() is not None


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
