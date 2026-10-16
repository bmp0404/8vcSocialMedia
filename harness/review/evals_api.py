"""The eval view's API: backtest reports and fault verdicts from sam/eval under eval/reports/.

Will (willatle) owns the eval views in the split; this reader exists so the view works before his
branch lands, and he can replace it.

Fault verdicts come from `python -m harness eval fault ... --out <path>` (harness/evals/cli.py on
sam/eval). That command has no default output path, so this reader looks where the eval CLI puts
every other report: eval/reports/fault-*.json, each `{"verdicts", "summary", "liveness_check"}`.
"""

import json
import re
from datetime import datetime, timezone

from harness import config
from harness.review.routes import HTTPError, Request, route

NAME = re.compile(r"backtest-[\w.-]+\.json")
FAULT_NAME = re.compile(r"fault-[\w.-]+\.json")
FAULT_COMMAND = ("python -m harness eval fault --stories --scores eval/reports/<backtest>.json:panel@mean "
                 "--out eval/reports/fault-<date>.json")


def reports_dir():
    return config.ROOT / "eval" / "reports"


@route("GET", r"/api/evals")
def list_evals(req: Request):
    d = reports_dir()
    if not d.is_dir():
        return {"available": False, "reason": "No eval/reports/ yet. sam/eval writes backtest-*.json there."}
    reports, problems = [], []
    for path in sorted(d.glob("backtest-*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append({"file": path.name, "error": str(e)})
            continue
        reports.append({
            "file": path.name,
            "at": data.get("at"),
            "provider": data.get("provider"),
            # Current reports list every judge model under "models"; older ones had one "model".
            "models": data.get("models") or ([data["model"]] if data.get("model") else []),
            "protocol": data.get("protocol"),
            "n_sampled": data.get("n_sampled"),
            "predictors": list((data.get("results") or {}).keys()),
        })
    return {"available": True, "reports": reports, "problems": problems}


@route("GET", r"/api/evals/(backtest-[\w.-]+\.json)")
def get_eval(req: Request):
    name = req.match[1]
    path = reports_dir() / name
    if not NAME.fullmatch(name) or not path.is_file():
        raise HTTPError(404, f"no report {name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Per-post predictions run to megabytes; the view only needs the summaries.
    for res in (data.get("results") or {}).values():
        if isinstance(res, dict):
            res.pop("predictions", None)
    data.pop("sample_ids", None)
    return data


def _fault_files(d):
    return sorted(d.glob("fault-*.json"), reverse=True)


@route("GET", r"/api/evals/faults")
def list_faults(req: Request):
    d = reports_dir()
    if not d.is_dir():
        return {"available": False, "reason": "No eval/reports/ yet.", "command": FAULT_COMMAND}
    files, problems = [], []
    for path in _fault_files(d):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append({"file": path.name, "error": str(e)})
            continue
        if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
            problems.append({"file": path.name, "error": "no verdicts list; expected the --out file of `harness eval fault`"})
            continue
        summary = data.get("summary") or {}
        files.append({
            "file": path.name,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "n_verdicts": len(data["verdicts"]),
            "counts": summary.get("counts") or {},
            "fix": summary.get("fix") or [],
        })
    return {"available": True, "files": files, "problems": problems, "command": FAULT_COMMAND}


@route("GET", r"/api/evals/faults/(fault-[\w.-]+\.json)")
def get_faults(req: Request):
    name = req.match[1]
    path = reports_dir() / name
    if not FAULT_NAME.fullmatch(name) or not path.is_file():
        raise HTTPError(404, f"no fault verdicts {name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        raise HTTPError(422, f"{name} has no verdicts list; expected the --out file of `harness eval fault`")
    return {"file": name, **data}
