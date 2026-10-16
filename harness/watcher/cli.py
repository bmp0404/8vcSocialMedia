"""`harness watch`, `harness watch status` and `harness watch stop`."""

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from harness.llm import get_llm
from harness.watcher import loop, modelrec, state as st
from harness.watcher.arms import DEFAULT_SEED


def _positive(value: str) -> float:
    n = float(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be above 0")
    return n


def _run(args) -> None:
    profiles = tuple(p.strip() for p in args.profiles.split(",") if p.strip())
    cfg = loop.WatchConfig(profiles=profiles, interval_s=args.interval, duration_s=args.duration,
                           arm_seed=args.arm_seed, sync_every_s=None if args.no_sync else loop.SYNC_EVERY_S)
    modelrec.install()
    try:
        watcher = loop.Watcher(cfg, get_llm(args.provider))
        watcher.run()
    except loop.AlreadyRunning as e:
        raise SystemExit(f"[watch] {e}") from e


def _pid(watch_dir: Path) -> int | None:
    path = watch_dir / "watch.pid"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text.isdigit():
        raise SystemExit(f"[watch] {path} holds {text!r}, not a pid; delete it if no watcher is running")
    return int(text)


def _is_watcher(pid: int) -> bool:
    """True when `pid` is alive and its command line is a harness watch."""
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    command = out.stdout.strip()
    return out.returncode == 0 and "harness" in command and "watch" in command


def _status(args) -> None:
    pid = _pid(loop.WATCH_DIR)
    running = pid is not None and _is_watcher(pid)
    print(f"[watch] {'running as pid ' + str(pid) if running else 'not running'}")
    db = loop.WATCH_DIR / "state.db"
    if not db.exists():
        print(f"[watch] no state at {db}; start it with `python -m harness watch`")
        return
    print(json.dumps(st.State(db).summary(), indent=2))


def _stop(args) -> None:
    pid = _pid(loop.WATCH_DIR)
    if pid is None:
        print("[watch] not running (no pid file)")
        return
    if not _is_watcher(pid):
        raise SystemExit(f"[watch] pid {pid} in {loop.WATCH_DIR / 'watch.pid'} isn't a running harness watch; "
                         "delete the file if it's stale")
    os.kill(pid, signal.SIGTERM)
    for _ in range(int(args.wait)):
        if not _is_watcher(pid):
            print(f"[watch] pid {pid} stopped")
            return
        time.sleep(1)
    raise SystemExit(f"[watch] sent SIGTERM to pid {pid}; it was still running {args.wait:.0f}s later")


def add_commands(sub) -> None:
    p = sub.add_parser("watch", help="poll the watch lists, draft for posts under 10 minutes old, ingest threads")
    p.add_argument("--profiles", default=",".join(loop.DEFAULT_PROFILES), help="comma-separated mission profiles")
    p.add_argument("--interval", type=_positive, default=loop.DEFAULT_INTERVAL_S,
                   help="seconds between two polls of one handle")
    p.add_argument("--duration", type=_positive, help="stop after this many seconds (default: run until stopped)")
    p.add_argument("--provider", help="override HARNESS_PROVIDER for the hot-path drafts")
    p.add_argument("--arm-seed", default=DEFAULT_SEED, help="seed of the coin that picks each hot target's arm")
    p.add_argument("--no-sync", action="store_true", help="don't run `harness outcomes sync` every 30 minutes")
    p.set_defaults(func=_run)
    wsub = p.add_subparsers(dest="watch_cmd")
    s = wsub.add_parser("status", help="whether the watcher runs, and its counts")
    s.set_defaults(func=_status)
    s = wsub.add_parser("stop", help="send the running watcher SIGTERM and wait for it to exit")
    s.add_argument("--wait", type=_positive, default=150.0, help="seconds to wait for the hot job in flight")
    s.set_defaults(func=_stop)
