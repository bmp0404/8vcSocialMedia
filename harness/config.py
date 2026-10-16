import json
import os
import re
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(env: str, default: str) -> Path:
    """Env-overridable path; relative paths resolve against the repo root."""
    p = Path(os.getenv(env) or default)
    return p if p.is_absolute() else ROOT / p


# Swap the brand by pointing these at another mission (and, ideally, a fresh DB + lessons file).
MISSION_PATH = _path("HARNESS_MISSION", "mission/mission.md")
LESSONS_PATH = _path("HARNESS_LESSONS", "memory/lessons.md")
DB_PATH = _path("HARNESS_DB", "data/harness.db")


def run_tag(run_id: int | str) -> str:
    """Name for a run's out/ files. Run ids restart in every DB, so a non-default HARNESS_DB is
    prefixed (`coordigent_demo-1`) to keep throwaway runs from overwriting the main DB's files."""
    return str(run_id) if DB_PATH == ROOT / "data" / "harness.db" else f"{DB_PATH.stem}-{run_id}"
OUT_DIR = ROOT / "out"
SAMPLE_SIGNALS_PATH = ROOT / "data" / "sample_signals.json"
# Teammate scraper output for the `feed` source: a file, a glob, or a directory of .json/.jsonl/.csv.
FEED_PATH = os.getenv("HARNESS_FEED_PATH", "")


def _targets_path() -> Path | None:
    """HARNESS_TARGETS, else `<mission stem>_targets.json` next to the mission if it exists."""
    if os.getenv("HARNESS_TARGETS"):
        return _path("HARNESS_TARGETS", "")
    guess = MISSION_PATH.with_name(f"{MISSION_PATH.stem}_targets.json")
    return guess if guess.exists() else None


TARGETS_PATH = _targets_path()

PROVIDER = os.getenv("HARNESS_PROVIDER", "mock").lower()
MODEL = os.getenv("HARNESS_MODEL", "")
SOURCES = [s.strip() for s in os.getenv("HARNESS_SOURCES", "sample").split(",") if s.strip()]


def mission() -> str:
    if not MISSION_PATH.exists():
        raise SystemExit(f"Mission file not found: {MISSION_PATH} (set HARNESS_MISSION)")
    return MISSION_PATH.read_text(encoding="utf-8")


def mission_name() -> str:
    """The mission's first heading, minus a leading 'Mission:'; used in run logs and hand-off emails."""
    for line in mission().splitlines():
        if line.startswith("#"):
            return re.sub(r"^mission:\s*", "", line.lstrip("#").strip(), flags=re.I) or "mission"
    return MISSION_PATH.stem


def lessons() -> str:
    return LESSONS_PATH.read_text(encoding="utf-8") if LESSONS_PATH.exists() else "(no lessons yet)"


@lru_cache(maxsize=1)
def targets() -> dict:
    """The targets file: {brand, keywords: [...], accounts: [{handle, priority, relationship, notes, aliases}]}.

    Returns an empty shape when no targets file is configured, so callers never need to check.
    """
    empty = {"keywords": [], "accounts": []}
    if not TARGETS_PATH:
        return empty
    if not TARGETS_PATH.exists():
        raise SystemExit(f"Targets file not found: {TARGETS_PATH} (set HARNESS_TARGETS)")
    data = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    data.setdefault("keywords", [])
    data.setdefault("accounts", [])
    return data


def watchlist() -> dict[str, dict]:
    """{lowercase handle or alias, no @: account} for every account in the targets file."""
    out = {}
    for acct in targets()["accounts"]:
        for h in [acct.get("handle"), *acct.get("aliases", [])]:
            if h:
                out[h.lstrip("@").lower()] = acct
    return out
