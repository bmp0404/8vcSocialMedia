"""The loop view's API: start one small round from the page and watch each stage report in.

A round is `python -m harness run` with a fixed, small argv in a child process. Nothing from the
request reaches the command line; the provider and sources come from the server's own environment
(HARNESS_PROVIDER, HARNESS_SOURCES), the same ones `python -m harness run` reads. Each stdout line
is kept with the stage tag it starts with ("[observe] ...") and the round is saved to
out/rounds/<id>.json when it ends, so the view can show the last round after a restart.

The harness prints human log lines, not structured events. The page (static/js/views/loop.js)
reads its per-stage numbers from those lines, so the fixture mode's simulated round, which only
appends lines, and a real round go through the same parser. Stage timings and counts for a saved
run come from harness/trace.py's out/trace-run<id>.json, which is structured.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from harness import config, memory
from harness.review.checkpoint import load_decisions, load_trace
from harness.review.routes import HTTPError, Request, route

STAGES = ("observe", "orient", "decide", "qualify", "review", "learn")
# Small on purpose: CLAUDE.md asks real runs to use 2 narratives and 1 draft per mind.
ARGV = ["-m", "harness", "run", "--narratives", "2", "--per-mind", "1", "--retries", "1"]
TAG = re.compile(r"^\[(\w+)\]\s?(.*)$")
RUN_ID = re.compile(r"\(run (\d+)\)")
KEEP_ROUNDS = 20


def rounds_dir():
    return config.OUT_DIR / "rounds"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Round:
    id: str
    started_at: str
    provider: str
    sources: list[str]
    command: str
    status: str = "running"  # running, done, failed, stopped
    stage: str = ""
    lines: list[dict] = field(default_factory=list)  # {t: seconds since start, stage, text}
    run_id: int | None = None
    exit_code: int | None = None
    finished_at: str | None = None


class Runner:
    """At most one round at a time. Thread-safe: the HTTP server is threaded."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current: Round | None = None
        self.proc: subprocess.Popen | None = None

    def start(self) -> Round:
        with self.lock:
            if self.current and self.current.status == "running":
                raise HTTPError(409, "A round is already running. Wait for it or stop it first.")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            rnd = Round(
                id=f"round-{stamp}",
                started_at=now(),
                provider=config.PROVIDER,
                sources=list(config.SOURCES),
                command="python " + " ".join(ARGV),
            )
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen(
                [sys.executable, *ARGV],
                cwd=config.ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            self.current = rnd
        threading.Thread(target=self._pump, args=(rnd, self.proc, time.monotonic()), daemon=True).start()
        return rnd

    def _pump(self, rnd: Round, proc: subprocess.Popen, t0: float) -> None:
        for raw in proc.stdout:
            text = raw.rstrip("\n")
            if not text.strip():
                continue
            m = TAG.match(text)
            stage = m[1] if m else None
            with self.lock:
                rnd.lines.append({"t": round(time.monotonic() - t0, 2), "stage": stage, "text": text})
                if stage in STAGES:
                    rnd.stage = stage
                if rid := RUN_ID.search(text):
                    rnd.run_id = int(rid[1])
        code = proc.wait()
        with self.lock:
            rnd.exit_code = code
            rnd.finished_at = now()
            if rnd.status == "running":
                rnd.status = "done" if code == 0 else "failed"
            if rnd.status == "done":
                rnd.stage = "review"  # the drafts are waiting for a person now
        save(rnd)

    def stop(self) -> Round:
        with self.lock:
            if not (self.current and self.current.status == "running" and self.proc):
                raise HTTPError(409, "No round is running.")
            self.current.status = "stopped"
            self.proc.terminate()
            return self.current

    def snapshot(self) -> dict | None:
        with self.lock:
            return json.loads(json.dumps(asdict(self.current))) if self.current else None


RUNNER = Runner()


def save(rnd: Round) -> None:
    d = rounds_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rnd.id}.json").write_text(json.dumps(asdict(rnd), indent=1), encoding="utf-8")
    for old in sorted(d.glob("round-*.json"))[:-KEEP_ROUNDS]:
        old.unlink()


def last_saved() -> dict | None:
    files = sorted(rounds_dir().glob("round-*.json")) if rounds_dir().exists() else []
    for f in reversed(files):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
    return None


# --- Summaries of the newest run -----------------------------------------------------------


def run_summary(run_id: int) -> dict:
    """Counts for one run from the store: what was drafted, vetoed, and what people decided."""
    actions = memory.run_actions(run_id)
    statuses = [a["review"]["status"] for a in actions]
    by_type: dict[str, int] = {}
    for a in actions:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    out = {
        "run_id": run_id,
        "actions": len(actions),
        "vetoed": sum(bool(a.get("vetoed")) for a in actions),
        "approved": statuses.count("approved"),
        "killed": statuses.count("killed"),
        "pending": statuses.count("pending"),
        # What the Queue's "to review" bay holds: the critic's vetoed drafts wait apart from these.
        "to_review": sum(a["review"]["status"] == "pending" and not a.get("vetoed") for a in actions),
        "by_type": by_type,
        "decisions": None,
    }
    try:
        decisions = load_decisions(run_id)
    except HTTPError as e:
        out["decisions_error"] = e.message
        decisions = None
    if decisions is not None:
        abstained: dict[str, int] = {}
        for d in decisions:
            if d.get("tool") == "abstain":
                abstained[d.get("reason") or "unknown"] = abstained.get(d.get("reason") or "unknown", 0) + 1
        out["decisions"] = {
            "total": len(decisions),
            "shipped": sum(bool(d.get("action_id")) for d in decisions),
            "abstained": abstained,
            "asks": sum(d.get("tool") == "ask_human" for d in decisions),
        }
    return out


def learn_summary() -> dict:
    path = config.LESSONS_PATH
    if not path.exists():
        return {"lessons_file": False}
    text = path.read_text(encoding="utf-8")
    return {
        "lessons_file": True,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "bullets": sum(1 for ln in text.splitlines() if ln.lstrip().startswith(("- ", "* "))),
    }


def trace_live() -> dict | None:
    """out/trace-live.json: harness/trace.py's progress snapshot for the run in flight."""
    path = config.OUT_DIR / "trace-live.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None  # caught mid-write; the page polls again


def trace_summary(run_id: int) -> dict | None:
    """Stage timings and counts from out/trace-run<id>.json, without the bulky signal and call lists."""
    trace = load_trace(run_id)
    if trace is None:
        return None
    calls = trace.get("llm_calls") or []
    by_stage: dict[str, dict] = {}
    for c in calls:
        b = by_stage.setdefault(c.get("stage") or "other", {"n": 0, "ms": 0, "errors": 0})
        b["n"] += 1
        b["ms"] += c.get("ms") or 0
        b["errors"] += not c.get("ok", True)
    return {
        "run_id": trace.get("run_id", run_id),
        "total_ms": trace.get("total_ms"),
        "stages": trace.get("stages") or [],
        "sources": trace.get("sources") or [],
        "signals": len(trace.get("signals") or []),
        "narratives": [
            {k: n.get(k) for k in ("id", "title", "relevance", "velocity", "angle", "velocity_basis") if k in n}
            for n in trace.get("narratives") or []
        ],
        "n_drafts": trace.get("n_drafts"),
        "kept": len(trace.get("ranked_ids") or []),
        "llm_calls": len(calls),
        "llm_errors": sum(not c.get("ok", True) for c in calls),
        "llm_ms": sum(c.get("ms") or 0 for c in calls),
        "calls_by_stage": by_stage,
    }


@route("GET", r"/api/loop")
def get_loop(req: Request):
    rnd = RUNNER.snapshot() or last_saved()
    runs = memory.list_runs(limit=12)
    # The newest run in the store. A round in flight has no run id until it saves, and a run started
    # from a terminal after the last page round is newer than that round's run.
    ids = [runs[0]["id"] if runs else None, (rnd or {}).get("run_id")]
    latest_id = max((i for i in ids if i is not None), default=None)
    return {
        "provider": config.PROVIDER,
        "sources": config.SOURCES,
        "command": "python " + " ".join(ARGV),
        "round": rnd,
        "live": trace_live(),
        "latest": run_summary(latest_id) if latest_id else None,
        "trace": trace_summary(latest_id) if latest_id else None,
        "runs": runs,
        "learn": learn_summary(),
    }


@route("POST", r"/api/rounds")
def post_round(req: Request):
    return asdict(RUNNER.start())


@route("POST", r"/api/rounds/stop")
def post_round_stop(req: Request):
    return asdict(RUNNER.stop())
