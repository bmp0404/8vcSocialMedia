"""The experiments view's API: the self-iteration ledger sam/iterate writes.

One JSON file per experiment at eval/experiments/<id>.json (schema "iterate.experiment/v1"), plus
eval/experiments/champion.json for the configuration currently in force and
eval/experiments/runs/<run_id>.json for each batch. A batch is one Benjamini-Hochberg family, so the
view groups experiments by run and shows the run's note and call budget beside them. A file that
doesn't parse or carries another schema is listed under `problems` instead of being skipped silently.
"""

import json

from harness import config
from harness.review.routes import Request, route

SCHEMA = "iterate.experiment/v1"
# The run summary fields the view shows; the rest (per-arm token counts) repeats what each record has.
RUN_FIELDS = ("run_id", "started_at", "finished_at", "provider", "champion", "max_calls", "estimated_calls",
              "llm_calls", "bh", "experiments", "note")


def experiments_dir():
    return config.ROOT / "eval" / "experiments"


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, str(e)


def _runs(d, problems):
    runs = {}
    for path in sorted((d / "runs").glob("*.json")):
        data, err = _read(path)
        if err or not isinstance(data, dict) or not data.get("run_id"):
            problems.append({"file": f"runs/{path.name}", "error": err or "not a run summary (no run_id)"})
            continue
        runs[data["run_id"]] = {k: data.get(k) for k in RUN_FIELDS}
    return runs


@route("GET", r"/api/experiments")
def get_experiments(req: Request):
    d = experiments_dir()
    if not d.is_dir():
        return {
            "available": False,
            "reason": "No eval/experiments/ yet. The self-iteration loop (sam/iterate) writes one JSON "
            "file per experiment there.",
        }
    experiments, problems = [], []
    for path in sorted(d.glob("*.json")):
        if path.name == "champion.json":
            continue
        data, err = _read(path)
        if err:
            problems.append({"file": path.name, "error": err})
        elif not isinstance(data, dict) or data.get("schema") != SCHEMA:
            got = data.get("schema") if isinstance(data, dict) else type(data).__name__
            problems.append({"file": path.name, "error": f"schema is {got!r}, expected {SCHEMA!r}"})
        else:
            experiments.append({**data, "file": f"eval/experiments/{path.name}"})
    experiments.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    champion, err = _read(d / "champion.json") if (d / "champion.json").exists() else (None, None)
    if err:
        problems.append({"file": "champion.json", "error": err})
    return {"available": True, "experiments": experiments, "champion": champion, "runs": _runs(d, problems),
            "problems": problems}
