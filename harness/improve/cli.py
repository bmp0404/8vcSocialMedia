"""`harness improve`, `harness improve status` and `harness improve stop`."""

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from harness.improve import scheduler as sch


def _positive(value: str) -> float:
    n = float(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be above 0")
    return n


def _run(args) -> None:
    provider = (args.provider or os.getenv("HARNESS_PROVIDER") or "mock").lower()
    if provider not in ("mock", "anthropic"):
        raise SystemExit("harness improve runs on --provider mock or anthropic (the gateway serves every model it uses)")
    paths = {k: Path(v) for k, v in (("threads_dir", args.threads_dir), ("watch_dir", args.watch_dir),
                                     ("hot_dir", args.hot_dir), ("outcomes_db", args.outcomes_db)) if v}
    cfg = sch.ImproveConfig(provider=provider, every_s=args.every * 3600, report_hour=args.report_hour,
                            instrument_calls=args.instrument_calls, experiment_calls=args.experiment_calls,
                            once=args.once, force_report=args.report, **paths)
    try:
        sch.Scheduler(cfg).run()
    except sch.AlreadyRunning as e:
        raise SystemExit(f"[improve] {e}") from e


def _pid() -> int | None:
    path = sch.OUT_DIR / "improve.pid"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text.isdigit():
        raise SystemExit(f"[improve] {path} holds {text!r}, not a pid; delete it if no loop is running")
    return int(text)


def _is_loop(pid: int) -> bool:
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return out.returncode == 0 and "harness" in out.stdout and "improve" in out.stdout


def _status(args) -> None:
    pid = _pid()
    running = pid is not None and _is_loop(pid)
    print(f"[improve] {'running as pid ' + str(pid) if running else 'not running'}")
    db = sch.OUT_DIR / "state.db"
    if not db.exists():
        print(f"[improve] no state at {db}; start it with `python -m harness improve`")
        return
    state = sch.State(db)
    last = state.last_finished()
    print(f"[improve] last cycle finished {last.isoformat() if last else 'never'}")
    for row in state.steps_since(None)[-12:]:
        summary = json.loads(row["summary"] or "{}")
        brief = row["error"] or ", ".join(f"{k} {v}" for k, v in summary.items() if not isinstance(v, (dict, list)))
        print(f"  {row['finished_at']}  {row['step']:<12} {row['status']:<7} {brief[:110]}")
    report = state.last_report()
    if report:
        print(f"[improve] latest report: {report['path']}")


def _stop(args) -> None:
    pid = _pid()
    if pid is None:
        print("[improve] not running (no pid file)")
        return
    if not _is_loop(pid):
        raise SystemExit(f"[improve] pid {pid} in {sch.OUT_DIR / 'improve.pid'} isn't a running harness improve; "
                         "delete the file if it's stale")
    os.kill(pid, signal.SIGTERM)
    for _ in range(int(args.wait)):
        if not _is_loop(pid):
            print(f"[improve] pid {pid} stopped")
            return
        time.sleep(1)
    raise SystemExit(f"[improve] sent SIGTERM to pid {pid}; it was still running {args.wait:.0f}s later "
                     "(an experiment run finishes before it stops)")


def _validate(args) -> None:
    from harness.improve import validate

    raise SystemExit(validate.main(args))


def add_commands(sub) -> None:
    p = sub.add_parser("improve", help="run the unattended improvement loop: facts, benchmark, instruments, "
                                       "queued experiments, morning report")
    p.add_argument("--provider", help="mock or anthropic; defaults to HARNESS_PROVIDER")
    p.add_argument("--once", action="store_true", help="run one cycle now and exit")
    p.add_argument("--report", action="store_true",
                   help="write a report this cycle, whatever the hour and even if today's exists")
    p.add_argument("--every", type=_positive, default=sch.DEFAULT_EVERY_S / 3600, help="hours between cycles")
    p.add_argument("--report-hour", type=int, default=sch.REPORT_HOUR, help="UTC hour from which a cycle writes the report")
    p.add_argument("--instrument-calls", type=int, default=300, help="LLM call cap for instrument scoring per cycle")
    p.add_argument("--experiment-calls", type=int, default=800, help="LLM call cap for experiments per cycle")
    p.add_argument("--threads-dir", help="the watcher's thread reads (default data/threads)")
    p.add_argument("--watch-dir", help="the watcher's state directory (default out/watch)")
    p.add_argument("--hot-dir", help="the watcher's hot files (default out/hot)")
    p.add_argument("--outcomes-db", help="the outcomes ledger (default data/outcomes.db)")
    p.set_defaults(func=_run)
    isub = p.add_subparsers(dest="improve_cmd")
    s = isub.add_parser("status", help="whether the loop runs, and its latest steps")
    s.set_defaults(func=_status)
    s = isub.add_parser("stop", help="send the running loop SIGTERM and wait for it to exit")
    s.add_argument("--wait", type=_positive, default=1800.0, help="seconds to wait for the step in flight")
    s.set_defaults(func=_stop)
    s = isub.add_parser("validate", help="run queue specs through the loop's parser and admission checks; no model "
                                         "calls, and nothing written outside a temporary directory")
    s.add_argument("specs", nargs="*", help="spec files (default: every file in the queue directory)")
    s.add_argument("--queue-dir", help="the queue directory (default eval/experiments/queue)")
    s.add_argument("--threads-dir", help="the watcher's thread reads the loop folds in (default data/threads)")
    s.add_argument("--ledger-dir", help="the experiment ledger the loop reads (default eval/experiments)")
    s.add_argument("--instruments", help="the instrument ledger (default eval/experiments/instruments.json)")
    s.add_argument("--experiment-calls", type=int, help="the loop's per-cycle experiment allowance (default 800)")
    s.set_defaults(func=_validate)
