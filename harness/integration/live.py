"""Run `harness run` unchanged and keep what each stage saw.

`cli.run` keeps the observed signals and orient's narratives in memory only, so nobody can later
check whether a draft's facts came from a source or whether its reply target was real. This
wraps the stage functions `cli.run` imported by name, records their inputs and outputs, and
writes `out/integration/run<id>.json`. The run itself is the real CLI path with the same
arguments, so what gets measured is what `harness run` does.

    uv run python -m harness.integration.live --sources espn_news,google_news,x_targets --narratives 2 --per-mind 1
"""

import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

from harness import cli, config, memory

RECORD_DIR = config.OUT_DIR / "integration"


def record_path(run_id: int):
    return RECORD_DIR / f"run{run_id}.json"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rec: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "argv": argv,
        "provider": argv[argv.index("--provider") + 1] if "--provider" in argv[:-1] else config.PROVIDER,
        "signals": [],
        "narratives": [],
        "decide_calls": [],
        "qualify_calls": [],
        "run_id": None,
        "decision_logs": [],
        "recommendations_file": None,
    }
    before = {p.name for p in config.OUT_DIR.glob("*.json")} if config.OUT_DIR.exists() else set()
    started = time.time()
    orig = {name: getattr(cli, name) for name in ("observe", "orient", "decide", "qualify")}
    orig_save = memory.save_run

    def observe(*a, **k):
        signals = orig["observe"](*a, **k)
        rec["signals"] = [asdict(s) for s in signals]
        return signals

    def orient(*a, **k):
        narratives = orig["orient"](*a, **k)
        rec["narratives"] = [asdict(n) for n in narratives]
        return narratives

    def decide(*a, **k):
        actions = orig["decide"](*a, **k)
        rec["decide_calls"].append({"revising": bool(k.get("vetoed")), "shipped": [x.to_dict() for x in actions]})
        return actions

    def qualify(llm, mission, actions, *a, **k):
        kept = orig["qualify"](llm, mission, actions, *a, **k)
        rec["qualify_calls"].append({"in": [x.id for x in actions], "kept": [x.id for x in kept]})
        return kept

    def save_run(actions, *a, **k):
        run_id = orig_save(actions, *a, **k)
        rec["run_id"] = run_id
        return run_id

    cli.observe, cli.orient, cli.decide, cli.qualify = observe, orient, decide, qualify
    memory.save_run = save_run
    old_argv = sys.argv
    sys.argv = ["harness", "run", *argv]
    try:
        cli.main()
    finally:
        sys.argv = old_argv
        for name, fn in orig.items():
            setattr(cli, name, fn)
        memory.save_run = orig_save

    if rec["run_id"] is None:
        raise SystemExit("[live] the run never reached memory.save_run, so there is nothing to record")
    # Other runs (mock checks, pytest's smoke test) can write to out/ while this one runs, so pick
    # files by run id, not by time: Action writes decisions-run<id>.json, and the recommendations
    # file carries its run_id.
    new = [p for p in config.OUT_DIR.glob("*.json") if p.name not in before or p.stat().st_mtime >= started]
    rec["decision_logs"] = [p.name for p in new if p.name == f"decisions-run{rec['run_id']}.json"]
    recs = [p for p in new if p.name.startswith("recommendations-")
            and json.loads(p.read_text(encoding="utf-8")).get("run_id") == rec["run_id"]]
    rec["recommendations_file"] = recs[0].name if recs else None
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    path = record_path(rec["run_id"])
    path.write_text(json.dumps(rec, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"[live] recorded run {rec['run_id']} -> {path}")
    return rec["run_id"]


if __name__ == "__main__":
    main()
