import os
import re
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
OUT_DIR = ROOT / "out"
SAMPLE_SIGNALS_PATH = ROOT / "data" / "sample_signals.json"

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
