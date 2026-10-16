"""`harness improve`: run the improvement steps on a schedule, unattended, and survive restarts.

Each cycle runs five steps in order. Every step has a key, and a key marked done in out/improve/state.db
is skipped, so a restart never repeats finished work:

| Step | Key | What it does |
| --- | --- | --- |
| facts | profile and UTC date | `python -m harness.facts refresh --profile <p>`, once a day per profile |
| fold | size and mtime of data/threads | the watcher's +48h threads into the rolling benchmark (fold.py) |
| instruments | cycle | score every instrument; LLM verdicts are cached, so a rerun costs nothing |
| experiments | cycle | stack checks, then each queued spec's next phase, read from the ledger |
| report | UTC date | out/improve/report-<date>.md, on the first cycle at or after `report_hour` |

Outcome sync already runs inside the watcher, so it isn't here. SIGTERM or SIGINT stops the loop after
the step in flight; `harness improve stop` sends SIGTERM to the pid in out/improve/improve.pid.
"""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import config
from harness.improve import fold, queue, report, watchday
from harness.improve import instruments as instrument_ledger
from harness.iterate import campaign, ledger
from harness.iterate.knobs import Config
from harness.iterate.runner import Proposal, run_batch

OUT_DIR = config.OUT_DIR / "improve"
DEFAULT_EVERY_S = 6 * 3600
REPORT_HOUR = 11  # UTC: 6 a.m. in Austin, 7 a.m. in New York
# Which instruments each fitness measures with: a report-only primary holds the fitness, and a report-only
# guardrail instrument stops blocking (harness/improve/instruments.py has the rule).
RULERS = {"action_lineup": {"primary": "lineup_detector", "guardrails": {"veto_pass_rate": "action_veto"}}}
# Promotions the loop made on its own: a queue spec's confirmation, or an engine confirmation. A stack check
# may revert these; anything a person started is put to a person instead.
LOOP_KINDS = {"queue", "confirmation"}
STALE_AFTER = timedelta(days=21)  # a candidate waiting this long for confirmation items goes to "needs a person"
MAX_PENDING = 3  # a phase that recorded this many unrun attempts stops retrying
SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY, pid INTEGER, started_at TEXT, finished_at TEXT,
    stop_reason TEXT);
CREATE TABLE IF NOT EXISTS steps (key TEXT PRIMARY KEY, step TEXT, cycle INTEGER, started_at TEXT, finished_at TEXT,
    status TEXT, summary TEXT, error TEXT, llm_calls INTEGER);
CREATE TABLE IF NOT EXISTS specs_seen (spec TEXT PRIMARY KEY, first_seen TEXT);
CREATE TABLE IF NOT EXISTS reports (date TEXT PRIMARY KEY, path TEXT, written_at TEXT);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


class AlreadyRunning(RuntimeError):
    pass


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    def start_cycle(self, pid: int, at: datetime) -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO cycles (pid, started_at) VALUES (?, ?)", (pid, iso(at))).lastrowid

    def finish_cycle(self, cycle: int, at: datetime, reason: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE cycles SET finished_at = ?, stop_reason = ? WHERE id = ?", (iso(at), reason, cycle))

    def first_started(self) -> datetime | None:
        with self._conn() as c:
            row = c.execute("SELECT MIN(started_at) FROM cycles").fetchone()[0]
        return datetime.fromisoformat(row) if row else None

    def last_finished(self) -> datetime | None:
        """When the last cycle that ran every step ended; a cycle a signal cut short doesn't count."""
        with self._conn() as c:
            row = c.execute("SELECT MAX(finished_at) FROM cycles WHERE stop_reason = 'done'").fetchone()[0]
        return datetime.fromisoformat(row) if row else None

    def mark_interrupted(self, at: datetime) -> int:
        """Steps a dead process left running. Only one loop runs at a time (the pid file), so at the start
        of a cycle any running step belongs to a process that died mid-step; its key isn't done, so the
        step runs again."""
        with self._conn() as c:
            return c.execute("UPDATE steps SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
                             (iso(at),)).rowcount

    def done(self, key: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM steps WHERE key = ? AND status = 'done'", (key,)).fetchone() is not None

    def start_step(self, key: str, step: str, cycle: int, at: datetime) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO steps (key, step, cycle, started_at, status) VALUES (?, ?, ?, ?, 'running') "
                      "ON CONFLICT (key) DO UPDATE SET cycle = excluded.cycle, started_at = excluded.started_at, "
                      "status = 'running', error = NULL", (key, step, cycle, iso(at)))

    def finish_step(self, key: str, status: str, at: datetime, summary: dict | None = None, error: str | None = None,
                    llm_calls: int = 0) -> None:
        with self._conn() as c:
            c.execute("UPDATE steps SET status = ?, finished_at = ?, summary = ?, error = ?, llm_calls = ? WHERE key = ?",
                      (status, iso(at), json.dumps(summary or {}, ensure_ascii=False), error, llm_calls, key))

    def steps_since(self, since: datetime | None, strict: bool = False) -> list[sqlite3.Row]:
        """Steps finished at or after `since`; strictly after when `since` is the previous report, whose own
        cycle finished in the same second it was written."""
        op = ">" if strict else ">="
        with self._conn() as c:
            return c.execute(f"SELECT * FROM steps WHERE finished_at {op} ? ORDER BY finished_at",
                             (iso(since) if since else "",)).fetchall()

    def cycles_since(self, since: datetime | None) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM cycles WHERE started_at >= ?", (iso(since) if since else "",)).fetchone()[0]

    def spec_seen(self, spec: str, at: datetime) -> datetime:
        """When the loop first saw a spec. Never moves later, so a spec can't be re-dated by a restart."""
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO specs_seen VALUES (?, ?)", (spec, iso(at)))
            return datetime.fromisoformat(c.execute("SELECT first_seen FROM specs_seen WHERE spec = ?",
                                                    (spec,)).fetchone()[0])

    def last_report(self) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM reports ORDER BY written_at DESC LIMIT 1").fetchone()

    def save_report(self, date: str, path: Path, at: datetime) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO reports VALUES (?, ?, ?)", (date, str(path), iso(at)))


@dataclass
class ImproveConfig:
    provider: str = "mock"
    profiles: tuple[str, ...] = ("wnba", "devtools")
    every_s: float = DEFAULT_EVERY_S
    report_hour: int = REPORT_HOUR
    instrument_calls: int = 300  # per cycle
    experiment_calls: int = 800  # per cycle
    out_dir: Path = OUT_DIR
    threads_dir: Path = fold.THREADS_DIR
    watch_dir: Path = config.OUT_DIR / "watch"
    hot_dir: Path = config.OUT_DIR / "hot"
    outcomes_db: Path = config.ROOT / "data" / "outcomes.db"
    queue_dir: Path = queue.QUEUE_DIR
    rolling_dir: Path = fold.ROLLING_DIR
    ledger_dir: Path | None = None  # default: eval/experiments/, or <out_dir>/experiments/ on mock
    instruments_path: Path | None = None  # default: eval/experiments/instruments.json, or <out_dir>/ on mock
    once: bool = False
    force_report: bool = False

    def __post_init__(self):
        # A mock run keeps everything, its step keys included, under out/improve/mock/, so a mock cycle can't
        # mark today's facts or report done for the live loop, and fake numbers never reach eval/experiments/.
        mock = self.provider == "mock"
        if mock and self.out_dir == OUT_DIR:
            self.out_dir = OUT_DIR / "mock"
        if self.ledger_dir is None:
            self.ledger_dir = self.out_dir / "experiments" if mock else ledger.EXPERIMENTS_DIR
        if self.instruments_path is None:
            self.instruments_path = self.out_dir / "instruments.json" if mock else instrument_ledger.LEDGER_PATH


def run_facts(profile: str, provider: str) -> str:
    """`python -m harness.facts refresh` in its own process. Returns its last line; raises with its error."""
    env = {**os.environ, "HARNESS_PROVIDER": provider}
    out = subprocess.run([sys.executable, "-m", "harness.facts", "refresh", "--profile", profile], cwd=config.ROOT,
                         env=env, capture_output=True, text=True, timeout=900)
    if out.returncode != 0:
        last = [line for line in (out.stderr or out.stdout).splitlines() if line.strip()]
        raise RuntimeError(f"facts refresh for {profile} exited {out.returncode}: {last[-1] if last else 'no output'}")
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else "(no output)"


@contextmanager
def profile_env(name: str):
    """The profile for this block, in the environment too: instruments read it from worker threads,
    which don't inherit config.use_profile's context variable."""
    saved = os.environ.get("HARNESS_PROFILE")
    os.environ["HARNESS_PROFILE"] = name
    try:
        with config.use_profile(name):
            yield
    finally:
        if saved is None:
            os.environ.pop("HARNESS_PROFILE", None)
        else:
            os.environ["HARNESS_PROFILE"] = saved


def default_catalog() -> dict[str, type]:
    from harness.iterate.fitness.action import ActionLineup
    from harness.iterate.fitness.qualify import QualifyAUC

    return {"action_lineup": ActionLineup, "qualify_auc": QualifyAUC}


@dataclass(frozen=True)
class Plan:
    """A spec's next phase as the loop would run it (Scheduler.admit)."""

    phase: str  # "screen" or "confirm"
    mode: str  # run_batch's mode: "screen", or "promote" for a confirmation
    proposal: Proposal
    base: Config  # the config both arms start from: the champion with the spec's baseline applied
    challenger: Config
    fit: campaign.Restricted  # the instrument cut to the items this phase uses
    budget: int  # calls the phase may spend, after the probes
    needed: set[str]  # models probed before the run
    probes: int
    cutoff: datetime


class Scheduler:
    def __init__(self, cfg: ImproveConfig, *, clock: Callable[[], datetime] = utcnow, echo: bool = True,
                 facts_runner: Callable[[str, str], str] | None = None, catalog: dict[str, type] | None = None,
                 build: Callable | None = None, probe: Callable[[set[str]], dict[str, str]] | None = None):
        self.cfg, self.clock, self.echo = cfg, clock, echo
        self.facts_runner = facts_runner
        self.probe = probe or self.probe_models
        self.catalog = catalog if catalog is not None else default_catalog()
        self._build = build or self.build
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.state = State(cfg.out_dir / "state.db")
        self.log_path, self.pid_path = cfg.out_dir / "improve.log", cfg.out_dir / "improve.pid"
        self.snapshot_dir = cfg.out_dir / "threads"
        self._log_lock = threading.Lock()
        self.stop = threading.Event()

    # ------------------------------------------------------------ plumbing

    def log(self, message: str) -> None:
        line = f"{iso(self.clock())} {message}"
        with self._log_lock:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if self.echo:
                print(line, flush=True)

    def step(self, key: str, name: str, cycle: int, fn: Callable[[], dict]) -> dict | None:
        """Run `fn` unless `key` is done. A failure is logged and recorded; the cycle goes on."""
        if self.state.done(key):
            return None
        self.state.start_step(key, name, cycle, self.clock())
        try:
            summary = fn() or {}
        except Exception as e:  # noqa: BLE001 - one failed step must not stop the others
            error = f"{type(e).__name__}: {e}"
            self.state.finish_step(key, "failed", self.clock(), error=error)
            self.log(f"[{name}] failed: {error}")
            return None
        self.state.finish_step(key, "done", self.clock(), summary, llm_calls=int(summary.get("llm_calls", 0)))
        self.log(f"[{name}] done: {json.dumps(summary, ensure_ascii=False)[:300]}")
        return summary

    def claim_pid(self) -> None:
        if self.pid_path.exists():
            text = self.pid_path.read_text(encoding="utf-8").strip()
            other = int(text) if text.isdigit() else None
            if other and other != os.getpid() and _alive(other):
                raise AlreadyRunning(f"harness improve is already running as pid {other} ({self.pid_path}); "
                                     "stop it with `python -m harness improve stop`")
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def release_pid(self) -> None:
        if self.pid_path.exists() and self.pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            self.pid_path.unlink()

    # ------------------------------------------------------------ the loop

    def run(self) -> str:
        self.claim_pid()
        reason = "stopped"
        handlers = {}
        if threading.current_thread() is threading.main_thread():
            def on_signal(signum, _frame):
                nonlocal reason
                reason = signal.Signals(signum).name
                self.stop.set()
            handlers = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
        self.log(f"[improve] pid {os.getpid()}, provider {self.cfg.provider}, a cycle every {self.cfg.every_s / 3600:g} h"
                 + (" (once)" if self.cfg.once else ""))
        try:
            while not self.stop.is_set():
                last = self.state.last_finished()
                due = last + timedelta(seconds=self.cfg.every_s) if last and not self.cfg.once else self.clock()
                while not self.stop.is_set() and self.clock() < due:
                    self.stop.wait(min(30.0, max(0.05, (due - self.clock()).total_seconds())))
                if self.stop.is_set():
                    break
                self.run_cycle()
                if self.cfg.once:
                    reason = "one cycle done"
                    break
        finally:
            for s, h in handlers.items():
                signal.signal(s, h)
            self.log(f"[improve] stopped: {reason}")
            self.release_pid()
        return reason

    def run_cycle(self) -> int:
        now = self.clock()
        interrupted = self.state.mark_interrupted(now)
        cycle = self.state.start_cycle(os.getpid(), now)
        self.log(f"[improve] cycle {cycle} starts" + (f"; {interrupted} step(s) an earlier process left "
                                                      "unfinished will run again" if interrupted else ""))
        for profile in self.cfg.profiles:
            if self.stop.is_set():
                break
            self.step(f"facts:{profile}:{now.date()}", "facts", cycle, lambda p=profile: self.facts(p))
        steps = [(self._fold_key(now), "fold", self.fold), (f"instruments:{cycle}", "instruments", self.instruments),
                 (f"experiments:{cycle}", "experiments", self.experiments)]
        if self.cfg.force_report:  # a person asked for one: write it even if today's exists
            steps.append((f"report:{now.date()}:cycle{cycle}", "report", self.write_report))
        elif now.hour >= self.cfg.report_hour:
            steps.append((f"report:{now.date()}", "report", self.write_report))
        for key, name, fn in steps:
            if self.stop.is_set():
                break
            self.step(key, name, cycle, fn)
        self.state.finish_cycle(cycle, self.clock(), "stopped" if self.stop.is_set() else "done")
        self.log(f"[improve] cycle {cycle} finished")
        return cycle

    # ------------------------------------------------------------ steps

    def facts(self, profile: str) -> dict:
        if self.facts_runner is None and self.cfg.provider == "mock":
            return {"profile": profile, "skipped": "the mock provider keeps memory in a throwaway store"}
        return {"profile": profile, "result": (self.facts_runner or run_facts)(profile, self.cfg.provider)}

    def _fold_key(self, now: datetime) -> str:
        try:
            return f"fold:{fold.digest(self.cfg.threads_dir)}"
        except FileNotFoundError:
            return f"fold:missing:{now.date()}"

    @staticmethod
    def fitted_before(frozen_pairs) -> list[datetime]:
        """Every fitted ranker's training cutoff: the corpus benchmark's own fits and the instrument ranker."""
        from harness.corpus.bench import DEV_CUTOFFS, TEST_CUTOFFS
        from harness.improve.scorers import ReplyRanker

        return [*DEV_CUTOFFS, *TEST_CUTOFFS, ReplyRanker.fit_before(frozen_pairs)]

    def fold(self) -> dict:
        from harness.corpus import load
        from harness.corpus.bench import load_pairs

        snap = fold.snapshot(self.cfg.threads_dir, self.snapshot_dir)
        frozen = load_pairs("replies")
        result = fold.fold(load(snap), frozen, self.fitted_before(frozen))
        fold.write(result, self.cfg.rolling_dir)
        return result.manifest()

    def _watch(self):
        from harness.corpus import load

        if not (self.snapshot_dir / "author_timelines.jsonl.gz").is_file():
            return None, {}
        return load(self.snapshot_dir), fold.profiles(self.snapshot_dir)

    def _llm_for(self, meter):
        from harness.iterate.llm import MeteredLLM, MockMeteredLLM

        def llm_for(model: str, arm: str):
            if self.cfg.provider == "mock":
                return MockMeteredLLM(meter, arm, lambda s, p: {}, model)
            return MeteredLLM(model, meter, arm)
        return llm_for

    def instruments(self) -> dict:
        from harness.corpus import load
        from harness.corpus.bench import load_pairs
        from harness.improve import outcomes, scorers
        from harness.iterate.llm import Meter

        watch, profiles = self._watch()
        ev = outcomes.Evidence(load(), load_pairs("replies"), watch, fold.load_rolling(self.cfg.rolling_dir),
                               fold.post_pairs(watch) if watch is not None else [], profiles,
                               outcomes.sgu_replies(self.cfg.outcomes_db, self.cfg.hot_dir))
        meter = Meter(self.cfg.instrument_calls)
        cache = self.cfg.out_dir / "cache" / ("mock" if self.cfg.provider == "mock" else "live")
        ctx = scorers.Ctx(self._llm_for(meter), meter, cache, self.log)
        led, events, errors = scorers.rescore(ev, ctx, self.cfg.instruments_path, self.clock())
        return {"llm_calls": meter.calls, "sgu_replies": len(ev.sgu), "rolling_pairs": len(ev.rolling),
                "status": {n: e["status"] for n, e in led["instruments"].items()}, "events": events, "errors": errors}

    def build(self, fitness: str, profile: str, threads: tuple[Path, ...] = ()):
        """An instrument for one spec: the action lineup on its fixture files plus the profile's watcher threads."""
        if fitness == "action_lineup":
            watch, profiles = self._watch()
            extra = fold.lineup_threads(watch, profiles, profile) if watch is not None else []
            return self.catalog[fitness](list(threads), extra_threads=extra, extra_label="watcher threads")
        if fitness == "qualify_auc":
            from harness.iterate.fitness.qualify import CACHE_REPORT

            return self.catalog[fitness](cache_report=None if self.cfg.provider == "mock" else CACHE_REPORT)
        raise ValueError(f"the scheduler doesn't know how to build {fitness!r}")

    def _run(self, proposals, fit, *, mode: str, max_calls: int, champion=None) -> tuple[list[dict], int]:
        recs = run_batch(proposals, {fit.name: fit}, provider=self.cfg.provider, max_calls=max_calls,
                         directory=self.cfg.ledger_dir, mode=mode, champion=champion, now=self.clock(),
                         run_note=f"harness improve, {mode}")
        run_file = self.cfg.ledger_dir / "runs" / f"{recs[0]['run_id']}.json"
        calls = json.loads(run_file.read_text(encoding="utf-8"))["llm_calls"] if run_file.is_file() else 0
        return recs, calls

    def probe_models(self, models: set[str]) -> dict[str, str]:
        """{model: error} for each model that doesn't answer a five-token request now. A run whose detector or
        guardrail model is out of quota would spend its budget on retries and decide nothing, so the phase waits."""
        if self.cfg.provider == "mock" or not models:
            return {}
        import anthropic

        client, bad = anthropic.Anthropic(timeout=60, max_retries=0), {}
        for model in sorted(models):
            try:
                client.messages.create(model=model, max_tokens=5, messages=[{"role": "user", "content": "Say ok."}])
            except Exception as e:  # noqa: BLE001 - any failure means the phase can't run on this model now
                bad[model] = api_error(e)
        return bad

    def _probe_note(self, needed: set[str]) -> str | None:
        """Why a phase must wait for a model, or None when every model answered."""
        down = self.probe(needed)
        return "waiting for " + "; ".join(f"{m} ({e})" for m, e in sorted(down.items())) if down else None

    @staticmethod
    def needed_models(fit, configs) -> set[str]:
        """The judge models an instrument names and the model knobs it reads in the configs it will run: the ones
        each arm reads when the instrument says (`reads`), else every model knob it lists."""
        named = {v for k, v in fit.instrument().items() if k.endswith("_model") and isinstance(v, str)}
        reads = getattr(fit, "reads", None)
        return named | {c.get(k) for c in configs for k in (reads(c) if reads else fit.knobs) if k.endswith(".model")}

    @staticmethod
    def _estimate(fit, champion, changes: dict, n: int) -> int:
        """What run_batch will budget: the champion, its repeat (never imported from a cache) and the challenger."""
        return (fit.estimate_calls(champion, n) + fit.estimate_calls(champion, n, repeat=True)
                + fit.estimate_calls(champion.with_changes(changes), n))

    def rulers(self, fitness: str) -> tuple[str | None, dict[str, str]]:
        """(why the fitness may not decide anything now, {guardrail metric: why it no longer blocks}) from the
        instrument ledger: a report-only primary instrument holds the fitness, a report-only guardrail
        instrument is still reported but stops blocking."""
        led = instrument_ledger.load(self.cfg.instruments_path).get("instruments", {})
        spec = RULERS.get(fitness, {})
        primary = led.get(spec.get("primary") or "", {})
        hold = (f"its primary instrument {spec['primary']} is report-only ({primary.get('reason')})"
                if primary.get("status") == instrument_ledger.REPORT_ONLY else None)
        untrusted = {metric: f"{name} is report-only ({led[name].get('reason')})"
                     for metric, name in spec.get("guardrails", {}).items()
                     if led.get(name, {}).get("status") == instrument_ledger.REPORT_ONLY}
        return hold, untrusted

    def _finish_stack_reverts(self, directory: Path, records: list[dict], out: dict,
                              only: set[str] | None = None) -> None:
        """Act on failed stack checks whose target nobody reverted. The loop reverts a promotion it made
        (proposer kind `queue` or `confirmation`) while the champion still holds that promotion's values,
        which also finishes a revert a crash lost between two writes. Anything else (a person's run, a
        revert, a value changed since) is put to a person, on the day the check failed only, so the report
        doesn't repeat it forever. `only` limits this to the given stack records."""
        champion, history = ledger.champion(directory)
        by_id = {r["id"]: r for r in records}
        reverted = {r["proposer"].get("source") for r in records if r["proposer"].get("kind") == "revert"}
        now = self.clock()
        for r in records:
            target = by_id.get(r["proposer"].get("source"))
            if (r["decision"]["status"] != "stack_failed" or (only is not None and r["id"] not in only)
                    or target is None or target["id"] not in history or target["id"] in reverted):
                continue
            in_force = all(champion.values[k] == knobs_value(v["to"]) for k, v in target["change"]["params"].items())
            loop_made = target["proposer"].get("kind") in LOOP_KINDS
            entry = {"fitness": r["eval"].get("fitness"), "latest": target["id"], "record": r["id"]}
            if loop_made and in_force:
                rev = ledger.revert_record(directory, target["id"], f"Stack check {r['id']} showed the promotions "
                                                                    "together worse than without this one.", by="auto",
                                           at=iso(now))
                ledger.write(directory, [rev])
                reverted.add(target["id"])
                out["stacks"].append({**entry, "action": "reverted", "reverted": rev["id"]})
            elif now - campaign.decided_at(r) <= timedelta(days=1):
                why = ("its values have changed since" if not in_force
                       else "a revert" if target["proposer"].get("kind") == "revert" else "started by a person")
                out["stacks"].append({**entry, "action": "needs a person",
                                      "note": f"stack check {r['id']} found the promotions together worse; {target['id']} "
                                              f"is {why}, so the loop won't undo it. Revert it with `harness iterate "
                                              f"revert {target['id']} --reason ...`, or leave it in force"})

    def experiments(self) -> dict:
        directory = self.cfg.ledger_dir
        directory.mkdir(parents=True, exist_ok=True)
        now, left = self.clock(), self.cfg.experiment_calls
        out = {"llm_calls": 0, "stacks": [], "specs": [], "errors": {}}
        self._finish_stack_reverts(directory, ledger.records(directory), out)
        records, configs = ledger.records(directory), ledger.configs(directory)
        champion, _ = ledger.champion(directory)
        blocked: dict[str, str] = {}  # fitness -> why its specs can't run this cycle
        reads = {n: c.reads for n, c in self.catalog.items() if hasattr(c, "reads")}
        for s in campaign.unmeasured(records, configs, {n: c.knobs for n, c in self.catalog.items()}, champion, reads):
            latest = next(r for r in records if r["id"] == s.latest)
            profile = latest["proposer"].get("profile") or config.profile_name()
            hold, untrusted = self.rulers(s.fitness)
            entry = {"fitness": s.fitness, "latest": s.latest}
            if hold:
                blocked[s.fitness] = f"a stack check is due and held: {hold}"
                out["stacks"].append({**entry, "action": "deferred", "note": hold})
                continue
            changes = _diff(s.baseline, s.champion)
            with profile_env(profile):
                base = campaign.Restricted(self._build(s.fitness, profile), untrusted=untrusted)
                needed = self.needed_models(base, [s.baseline, s.champion])
                probes = len(needed) if self.cfg.provider != "mock" else 0
                fit = self._sized(base, dict(untrusted=untrusted, earliest=False), queue.CONFIRM_CLUSTERS, 1,
                                  left - probes, s.baseline, changes)
                waiting = (f"even its smallest set needs more than the {left - probes} calls left this cycle"
                           if fit is None else None)
                if not waiting:
                    waiting = self._probe_note(needed)
                    left, out["llm_calls"] = left - probes, out["llm_calls"] + probes
                if waiting:
                    blocked[s.fitness] = f"a stack check is due and waiting: {waiting}"
                    out["stacks"].append({**entry, "action": "deferred", "note": waiting})
                    continue
                prop = Proposal(s.fitness, changes, f"The promotions in force, measured together on {s.fitness}.",
                                {"kind": "stack", "source": s.latest, "profile": profile}, "stack check")
                recs, calls = self._run([prop], fit, mode="stack", max_calls=left, champion=s.baseline)
            left -= calls
            out["llm_calls"] += calls
            status = recs[0]["decision"]["status"]
            entry.update(action=status, record=recs[0]["id"])
            out["stacks"].append(entry)
            if status == "stack_failed":
                self._finish_stack_reverts(directory, ledger.records(directory), out, only={recs[0]["id"]})
            elif status != "stack_ok":
                blocked[s.fitness] = f"its stack check didn't run: {recs[0]['decision']['reason'][:160]}"

        specs, errors = queue.load(self.cfg.queue_dir, self.catalog, now=now)
        out["errors"] = errors
        for spec in sorted(specs, key=lambda x: (x.proposed_at, x.id)):
            entry = {"spec": spec.id, "fitness": spec.fitness, "profile": spec.profile}
            out["specs"].append(entry)
            if spec.fitness in blocked:
                entry.update(state="blocked", note=blocked[spec.fitness])
                continue
            left, calls = self._spec(spec, entry, now, left)
            out["llm_calls"] += calls
        return out

    def _spec(self, spec: queue.Spec, entry: dict, now: datetime, left: int) -> tuple[int, int]:
        """Run or explain one spec's next phase. Returns (calls left this cycle, calls spent)."""
        with profile_env(spec.profile):
            plan = self.admit(spec, entry, now, left)
            if plan is None:
                return left, 0
            waiting = self._probe_note(plan.needed)
            if waiting:
                entry.update(state="deferred", note=waiting)
                return left - plan.probes, plan.probes
            recs, calls = self._run([plan.proposal], plan.fit, mode=plan.mode, max_calls=plan.budget,
                                    champion=plan.base if spec.baseline else None)
        entry.update(state="ran", phase=plan.phase, record=recs[0]["id"], decision=recs[0]["decision"]["status"],
                     note=recs[0]["decision"]["reason"][:200])
        return left - plan.probes - calls, plan.probes + calls

    def admit(self, spec: queue.Spec, entry: dict, now: datetime, left: int) -> Plan | None:
        """Everything the loop decides about a spec's next phase before it spends a call: which phase, whether
        it may run now, on which items, within which budget, and which models it would probe. Returns None
        with `entry` saying why when the phase can't run; `harness improve validate` prints the same answer.
        Call it inside the spec's profile (profile_env)."""
        directory = self.cfg.ledger_dir
        first_seen = self.state.spec_seen(f"{spec.id}:{spec.digest}", now)
        cutoff = max(spec.proposed_at, first_seen)
        ph = queue.phase(spec, ledger.records(directory))
        if ph.next == "done":
            entry.update(state="done", note="decided")
            return None
        pending = [r for r in (ph.screen if ph.next == "screen" else ph.confirm) if r["decision"]["status"] == "pending"]
        if len(pending) >= MAX_PENDING:
            entry.update(state="stale", note=f"the {ph.next} phase didn't run {len(pending)} times (last reason: "
                                             f"{pending[-1]['decision']['reason'][:140]}); change what the spec tests "
                                             "or give it a new id to try again")
            return None
        champion, _ = ledger.champion(directory)
        base = champion.with_changes(spec.baseline)  # both arms start here; the champion when there's no baseline
        challenger = base.with_changes(spec.knobs)
        if challenger.hash == base.hash:
            entry.update(state="done", note="the champion already has these values" if not spec.baseline
                         else "the champion with the baseline already has these values")
            return None
        hold, untrusted = self.rulers(spec.fitness)
        if hold:
            entry.update(state="held", note=hold)
            return None
        prop = Proposal(spec.fitness, spec.knobs, spec.hypothesis,
                        {**spec.proposer(ph.next, cutoff), "profile": spec.profile}, spec.label)
        screen = ph.next == "screen"
        waited = now - (first_seen if screen else campaign.decided_at(ph.screen[-1]))
        built = self._build(spec.fitness, spec.profile, spec.threads)
        refused = self.refusal(built, spec, base, challenger)
        if refused:
            entry.update(state="refused", note=refused)
            return None
        # A screen always keeps the fixture threads its spec names; the cap applies to the watcher's.
        keep = frozenset(it.id for it in self.catalog[spec.fitness](list(spec.threads)).items()) \
            if screen and spec.threads else frozenset()
        window = {"by": cutoff} if screen else {"after": cutoff}
        restricted = campaign.Restricted(built, **window, guardrails=spec.guardrails, untrusted=untrusted)
        if not screen:
            clusters = len({it.cluster for it in restricted.items()})
            if clusters < queue.MIN_CONFIRM_CLUSTERS:
                entry.update(state="stale" if waited > STALE_AFTER else "waiting",
                             note=f"a candidate waiting for {queue.MIN_CONFIRM_CLUSTERS} {restricted.cluster_unit}s "
                                  f"rooted after {cutoff.isoformat(timespec='minutes')}; it has {clusters}")
                return None
            cap = campaign.weekly_cap(ledger.records(directory), now)
            if not cap.open:
                entry.update(state="held", note=f"confirmation waits for the weekly cap ({cap.holder}) to "
                                                f"reopen at {cap.opens_at.isoformat(timespec='minutes')}")
                return None
        else:
            from harness.iterate.propose import is_settled

            # Another spec version's candidate isn't a decision: its confirmation never ran.
            decided = [r for r in ledger.records(directory)
                       if not (r["proposer"].get("kind") == "queue" and r["decision"]["status"] == "candidate")]
            sha = {spec.fitness: restricted.instrument().get("dataset_sha256")}
            if is_settled(prop, base, decided, {spec.fitness: restricted.knobs}, ledger.configs(directory), sha):
                entry.update(state="done", note="the ledger already decided this change against an equivalent "
                                                "champion on the same items; screening it again would test them twice")
                return None
        needed = self.needed_models(restricted, [base, challenger])
        probes = len(needed) if self.cfg.provider != "mock" else 0
        budget = min(spec.screen_calls if screen else spec.confirm_calls, left) - probes
        sized = self._sized(restricted, dict(**window, guardrails=spec.guardrails, untrusted=untrusted, keep=keep),
                            queue.SCREEN_CLUSTERS if screen else queue.CONFIRM_CLUSTERS,
                            1 if screen else queue.MIN_CONFIRM_CLUSTERS, budget, base, spec.knobs)
        if sized is None:
            entry.update(state="stale" if waited > STALE_AFTER else "deferred",
                         note=f"even its smallest set of items needs more than the {budget} calls available "
                              "to it this cycle")
            return None
        return Plan(ph.next, "screen" if screen else "promote", prop, base, challenger, sized, budget, needed, probes,
                    cutoff)

    @staticmethod
    def refusal(fit, spec: queue.Spec, base: Config, challenger: Config) -> str | None:
        """Why the instrument wouldn't test what the spec asks, or None: a changed knob neither arm reads, so both
        would run the same code, or a config the instrument says it can't run as written (check_config)."""
        reads = getattr(fit, "reads", None)
        if reads is not None:
            unread = sorted(set(spec.knobs) - set(reads(base)) - set(reads(challenger)))
            if unread:
                return (f"neither arm reads {unread}: the arm both sides would run reads only {list(reads(base))}, so "
                        "they'd run the same code; add a [baseline] whose arm reads them")
        check = getattr(fit, "check_config", None)
        for cfg in (base, challenger) if check is not None else ():
            why = check(cfg)
            if why:
                return why
        return None

    def _sized(self, base: campaign.Restricted, kwargs: dict, cap: int, floor: int, budget: int, champion,
               changes: dict) -> campaign.Restricted | None:
        """The instrument limited to the most clusters, up to `cap` and at least `floor`, whose run fits
        `budget`; None when even `floor` doesn't. Items are read once; the cut is a pure function of them."""
        items = base.items()
        earliest = kwargs.get("earliest", "after" in kwargs)
        keep = kwargs.get("keep", frozenset())
        for k in range(cap, floor - 1, -1):
            chosen = campaign.limit_clusters(items, k, earliest=earliest, keep=keep)
            if self._estimate(base, champion, changes, len(chosen)) <= budget:
                return campaign.Restricted(base.fit, **{**kwargs, "max_clusters": k, "earliest": earliest})
        return None

    def write_report(self) -> dict:
        now = self.clock()
        last = self.state.last_report()
        # The first report covers what the loop has done, not the ledger's whole history.
        since = datetime.fromisoformat(last["written_at"]) if last else self.state.first_started()
        steps = self.state.steps_since(since, strict=last is not None)
        led = instrument_ledger.load(self.cfg.instruments_path)
        events = [{"instrument": n, **e} for n, i in led.get("instruments", {}).items() for e in i.get("events", [])
                  if since is None or datetime.fromisoformat(e["at"]) >= since]
        recs = ledger.records(self.cfg.ledger_dir) if self.cfg.ledger_dir.is_dir() else []
        recent = [r for r in recs if since is None or campaign.decided_at(r) >= since]
        decided = [r for r in recent if r["decision"]["status"] not in ("pending",)]
        champion, _ = ledger.champion(self.cfg.ledger_dir) if self.cfg.ledger_dir.is_dir() else (None, [])
        facts, queue_state, queue_errors, failures, interrupted = {}, [], {}, [], []
        for row in steps:
            summary = json.loads(row["summary"] or "{}")
            if row["status"] == "failed":
                failures.append(f"The {row['step']} step failed: {row['error']}")
            elif row["status"] == "interrupted":
                interrupted.append(row["step"])
            elif row["step"] == "facts":
                facts[summary.get("profile", "?")] = summary.get("result") or summary.get("skipped", "")
            elif row["step"] == "experiments":
                queue_state, queue_errors = summary.get("specs", []), summary.get("errors", {})
                failures += [f"Stack check on {st['fitness']} ({st['action']}): {st.get('note', st.get('record'))}"
                             for st in summary.get("stacks", []) if st.get("action") not in ("stack_ok", "reverted")]
        manifest = self.cfg.rolling_dir / "manifest.json"
        day = watchday.read(self.cfg.watch_dir, now)
        data = report.ReportData(
            now=now, since=since, cycles=self.state.cycles_since(since),
            llm_calls=sum(row["llm_calls"] or 0 for row in steps), facts=facts,
            fold=json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None,
            instruments=led, events=events, decided=decided, champion=champion.ref if champion else "",
            champion_changed=[r["id"] for r in recent if r["decision"]["status"] == "promoted"],
            queue=queue_state, queue_errors=queue_errors, day=day,
            hot_offers={h["status_id"]: watchday.hot_offer(h["path"]) for h in (day.hot if day else [])},
            failures=failures, interrupted=interrupted, disk_free_gb=shutil.disk_usage("/").free / 1e9)
        # A report a person forced keeps its own file, so it never overwrites the morning one.
        name = f"report-{now.date().isoformat()}" + (f"-{now:%H%M}" if self.cfg.force_report else "")
        path = self.cfg.out_dir / f"{name}.md"
        path.write_text(report.render(data), encoding="utf-8")
        self.state.save_report(now.date().isoformat(), path, now)
        return {"path": str(path)}


def knobs_value(v):
    """A ledger value in the form Config stores it: lists become sorted tuples."""
    return tuple(sorted(v)) if isinstance(v, list) else v


def api_error(e: Exception) -> str:
    """"HTTP 429: All credentials for model ... are cooling down" rather than the whole response body."""
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    message = body.get("error", {}).get("message") if isinstance(body, dict) else None
    text = (message or str(e)).split("(last error")[0].strip()
    return f"{'HTTP ' + str(status) if status else type(e).__name__}: {text[:140]}"


def _diff(a, b) -> dict:
    from harness.iterate.knobs import diff

    return {k: v["to"] for k, v in diff(a, b).items()}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
