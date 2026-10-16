"""The observability console at /console: read-only views over memory, out/ traces and eval/ reports.

It sits beside the review page rather than inside it, and nothing here writes.
"""

import csv
import html
import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from datetime import datetime
from statistics import median

from harness import config, memory
from harness.sources import REGISTRY
from harness.trace import seed_minds

PAGE = Path(__file__).parent / "console.html"
EVAL_DIR = config.ROOT / "eval"
DATASET = config.ROOT / "data" / "devtools_virality_30d.csv"
RUN_LOG = config.OUT_DIR / "console-run.log"

ENGAGEMENT_KEYS = ("likes", "replies", "reposts", "retweets", "quotes", "bookmarks")


def engagement(metrics: dict) -> float:
    return float(sum(v for k, v in metrics.items() if k in ENGAGEMENT_KEYS and isinstance(v, (int, float))))


def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def live() -> dict | None:
    path = config.OUT_DIR / "trace-live.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None  # caught mid-write; the page polls again


def trace(run_id: int) -> dict | None:
    path = config.OUT_DIR / f"trace-run{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _rows() -> list[dict]:
    """Every action with its run time, review and outcome, oldest run first."""
    with memory.connect() as conn:
        rows = conn.execute(
            """SELECT a.run_id, a.data, r.started_at, r.provider, rv.status, rv.text AS edited, rv.note, rv.reviewed_at,
                      o.metrics
               FROM actions a JOIN runs r ON r.id = a.run_id
               LEFT JOIN reviews rv ON rv.action_id = a.id
               LEFT JOIN outcomes o ON o.action_id = a.id
               ORDER BY a.run_id"""
        ).fetchall()
    out = []
    for r in rows:
        a = json.loads(r["data"])
        out.append({
            **a,
            "run_id": r["run_id"],
            "started_at": r["started_at"],
            "provider": r["provider"],
            "status": r["status"] or "pending",
            "edited": bool(r["edited"]),
            "noted": bool(r["note"]),
            "reviewed_at": r["reviewed_at"],
            "actual": engagement(json.loads(r["metrics"])) if r["metrics"] else None,
        })
    return out


def overview() -> dict:
    rows = _rows()

    runs: dict[int, dict] = {}
    for a in rows:
        run = runs.setdefault(a["run_id"], {
            "id": a["run_id"], "started_at": a["started_at"], "provider": a["provider"],
            "drafts": 0, "vetoed": 0, "approved": 0, "killed": 0, "edited": 0, "outcomes": 0, "latencies": [],
        })
        run["drafts"] += 1
        run["vetoed"] += a.get("vetoed", False)
        run["approved"] += a["status"] == "approved"
        run["killed"] += a["status"] == "killed"
        run["edited"] += a["edited"]
        run["outcomes"] += a["actual"] is not None
        start, done = _ts(a["started_at"]), _ts(a["reviewed_at"])
        if start and done:
            run["latencies"].append((done - start).total_seconds())
    for run in runs.values():
        lat = run.pop("latencies")
        run["decision_s"] = round(median(lat)) if lat else None
        t = trace(run["id"])
        run["total_ms"] = t["total_ms"] if t else None
        run["signals"] = len(t["signals"]) if t else None
        run["narratives"] = len(t["narratives"]) if t else None
        run["kept"] = len(t["ranked_ids"]) if t else None

    minds: dict[str, dict] = {name: {"name": name, "mind": name} for name in seed_minds()}
    for a in rows:
        key = a.get("variant") or a["mind"]
        m = minds.setdefault(key, {"name": key, "mind": a["mind"]})
        for k in ("drafts", "vetoed", "approved", "killed", "edited", "outcomes"):
            m.setdefault(k, 0)
        m.setdefault("predicted", []).append(a.get("predicted_score", 0))
        m["drafts"] += 1
        m["vetoed"] += a.get("vetoed", False)
        m["approved"] += a["status"] == "approved"
        m["killed"] += a["status"] == "killed"
        m["edited"] += a["edited"]
        if a["actual"] is not None:
            m["outcomes"] += 1
            m.setdefault("actual", []).append(a["actual"])
    for m in minds.values():
        p, act = m.pop("predicted", []), m.pop("actual", [])
        m.setdefault("drafts", 0)
        m["avg_predicted"] = round(sum(p) / len(p), 3) if p else None
        m["avg_actual"] = round(sum(act) / len(act), 1) if act else None

    calibration = [
        {"id": a["id"], "mind": a["mind"], "type": a["type"], "run_id": a["run_id"], "text": a["text"],
         "predicted": a.get("predicted_score", 0), "actual": a["actual"]}
        for a in rows if a["actual"] is not None
    ]
    reviewed = [a for a in rows if a["status"] != "pending"]
    return {
        "runs": sorted(runs.values(), key=lambda r: r["id"], reverse=True),
        "minds": sorted(minds.values(), key=lambda m: m["drafts"], reverse=True),
        "calibration": calibration,
        "feedback": {
            "drafts": len(rows),
            "reviewed": len(reviewed),
            "edited": sum(a["edited"] for a in rows),
            "noted": sum(a["noted"] for a in rows),
            "outcomes": len(calibration),
        },
    }


def context() -> dict:
    mission = config.mission() if config.MISSION_PATH.exists() else ""
    title = next((ln.lstrip("# ").strip() for ln in mission.splitlines() if ln.startswith("#")), "Harness")
    lessons_mtime = (
        datetime.fromtimestamp(config.LESSONS_PATH.stat().st_mtime).isoformat(timespec="seconds")
        if config.LESSONS_PATH.exists() else None
    )
    return {
        "title": re.sub(r"^Mission:\s*", "", title),
        "mission": mission,
        "lessons": config.lessons(),
        "lessons_updated": lessons_mtime,
        "provider": config.PROVIDER,
        "model": config.MODEL,
        "sources_enabled": config.SOURCES,
        "sources_available": sorted(REGISTRY),
        "minds": {name: persona.strip().split("\n", 1)[0] for name, persona in seed_minds().items()},
    }


def evals() -> list[dict]:
    """Backtest reports (eval/backtest-*.json), newest first."""
    reports = []
    for path in sorted(EVAL_DIR.glob("backtest-*.json"), reverse=True):
        try:
            reports.append({"file": path.name, **json.loads(path.read_text(encoding="utf-8"))})
        except (OSError, json.JSONDecodeError) as e:
            reports.append({"file": path.name, "error": str(e)})
    return reports


def _median(xs: list[float]) -> float | None:
    return median(xs) if xs else None


def _group(rows: list[dict], key) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return [
        {"key": k, "posts": len(g), "median": _median([r["eng"] for r in g]),
         "viral_rate": round(sum(r["viral"] for r in g) / len(g), 3)}
        for k, g in groups.items()
    ]


_market_cache: dict = {}


def market() -> dict | None:
    """What spreads in the dev-tool dataset (data/devtools_virality_30d.csv)."""
    if not DATASET.exists():
        return None
    mtime = DATASET.stat().st_mtime
    if _market_cache.get("mtime") == mtime:
        return _market_cache["data"]
    with DATASET.open(encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    rows = []
    for r in raw:
        try:
            eng = int(r["Engagement"])
        except (KeyError, ValueError):
            continue
        text = html.unescape(r.get("Post Content", ""))
        rows.append({
            "account": r.get("Account", ""), "text": text, "media": r.get("Media") or "none", "eng": eng,
            "likes": int(r.get("Likes") or 0), "replies": int(r.get("Replies") or 0), "reposts": int(r.get("Retweets") or 0),
            "url": r.get("Post URL", ""), "date": r.get("Date", ""), "segment": r.get("Segment", ""),
            "category": r.get("Category", ""), "viral": r.get("Category") in ("Medium Viral", "Super Viral"),
            "chars": len(re.sub(r"https://t\.co/\S+", "", text).strip()),
        })
    length = lambda r: "<80" if r["chars"] < 80 else "80–159" if r["chars"] < 160 else "160–239" if r["chars"] < 240 else "240+"
    accounts = []
    for g in _group(rows, lambda r: r["account"]):
        posts = [r for r in rows if r["account"] == g["key"]]
        best = max(posts, key=lambda r: r["eng"])
        accounts.append({**g, "segment": posts[0]["segment"], "best": {k: best[k] for k in ("text", "eng", "url", "date", "media")}})
    data = {
        "file": DATASET.name,
        "posts": len(rows),
        "accounts": len(accounts),
        "dates": [min(r["date"] for r in rows), max(r["date"] for r in rows)] if rows else [],
        "median": _median([r["eng"] for r in rows]),
        "viral_rate": round(sum(r["viral"] for r in rows) / len(rows), 3) if rows else 0,
        "categories": _group(rows, lambda r: r["category"]),
        "media": sorted(_group(rows, lambda r: r["media"]), key=lambda g: -g["posts"]),
        "length": sorted(_group(rows, length), key=lambda g: ["<80", "80–159", "160–239", "240+"].index(g["key"])),
        "segments": sorted(_group(rows, lambda r: r["segment"]), key=lambda g: -g["posts"]),
        "account_rows": sorted(accounts, key=lambda a: -(a["median"] or 0)),
        "top": sorted(rows, key=lambda r: -r["eng"])[:40],
    }
    _market_cache.update(mtime=mtime, data=data)
    return data


_run_lock = threading.Lock()
_run: dict = {"proc": None, "provider": None, "started": None}


def start_run(body: dict) -> tuple[int, dict]:
    """Start `python -m harness run` in the background; the page watches /api/runs for the result."""
    provider = str(body.get("provider") or config.PROVIDER).lower()
    if provider not in ("mock", "anthropic", "xai", "openai"):
        return 400, {"error": f"unknown provider '{provider}'"}
    with _run_lock:
        if _run["proc"] and _run["proc"].poll() is None:
            return 409, {"error": "a run is already in progress"}
        config.OUT_DIR.mkdir(parents=True, exist_ok=True)
        (config.OUT_DIR / "trace-live.json").unlink(missing_ok=True)  # don't let the page read the last run's "done"
        log = RUN_LOG.open("w", encoding="utf-8")
        _run.update(provider=provider, started=datetime.now().isoformat(timespec="seconds"), proc=subprocess.Popen(
            [sys.executable, "-m", "harness", "run", "--provider", provider],
            cwd=config.ROOT, stdout=log, stderr=subprocess.STDOUT,
        ))
    return 200, {"ok": True, "provider": provider}


def run_status() -> dict:
    proc = _run["proc"]
    log = RUN_LOG.read_text(encoding="utf-8")[-4000:] if RUN_LOG.exists() else ""
    return {
        "running": bool(proc and proc.poll() is None),
        "exit_code": proc.poll() if proc else None,
        "provider": _run["provider"], "started": _run["started"], "log": log,
        "default_provider": config.PROVIDER,
    }


def post(path: str, body: dict) -> tuple[int, dict] | None:
    """Answer a console POST, or None if the path isn't the console's."""
    if path == "/api/console/run":
        return start_run(body)
    return None


def get(path: str) -> tuple[int, bytes, str] | None:
    """Answer a console request, or None if the path isn't the console's."""
    if path == "/console":
        return 200, PAGE.read_bytes(), "text/html; charset=utf-8"
    routes = {"/api/console/overview": overview, "/api/console/context": context, "/api/console/evals": evals,
              "/api/console/market": market, "/api/console/run": run_status,
              "/api/console/live": live}
    if path in routes:
        data = routes[path]()
    elif m := re.fullmatch(r"/api/console/trace/(\d+)", path):
        data = trace(int(m[1]))
    else:
        return None
    return 200, json.dumps(data).encode(), "application/json"
