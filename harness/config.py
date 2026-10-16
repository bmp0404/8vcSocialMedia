import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Brand-specific text lives in files, not Python (see CONTRIBUTING.md).
MISSION_PATH = ROOT / "mission" / "mission.md"
MINDS_DIR = ROOT / "harness" / "minds" / "prompts"
PANEL_PATH = ROOT / "eval" / "panel.json"
SAMPLE_SIGNALS_PATH = ROOT / "data" / "sample_signals.json"
DATASET_PATH = Path(os.getenv("HARNESS_DATASET") or ROOT / "data" / "devtools_virality_30d.csv")

# Learned state. Override both to experiment without touching the team's files.
DATA_DIR = Path(os.getenv("HARNESS_DATA_DIR") or ROOT / "data")
MEMORY_DIR = Path(os.getenv("HARNESS_MEMORY_DIR") or ROOT / "memory")
DB_PATH = DATA_DIR / "harness.db"
LESSONS_PATH = MEMORY_DIR / "lessons.md"
PLAYBOOK_PATH = MEMORY_DIR / "playbook.md"
OUT_DIR = Path(os.getenv("HARNESS_OUT_DIR") or ROOT / "out")
EVAL_DIR = Path(os.getenv("HARNESS_EVAL_DIR") or ROOT / "eval")

PROVIDER = os.getenv("HARNESS_PROVIDER", "mock").lower()
MODEL = os.getenv("HARNESS_MODEL", "")
SOURCES = [s.strip() for s in os.getenv("HARNESS_SOURCES", "sample").split(",") if s.strip()]


def _read(path: Path, default: str) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else default


def mission() -> str:
    return MISSION_PATH.read_text(encoding="utf-8")


def lessons() -> str:
    return _read(LESSONS_PATH, "(no lessons yet)")


def playbook() -> str:
    return _read(PLAYBOOK_PATH, "(no playbook yet)")


def panel() -> dict[str, str]:
    return json.loads(PANEL_PATH.read_text(encoding="utf-8"))


def seed_minds() -> dict[str, str]:
    """harness/minds/prompts/<name>.md -> prompt. Editing a file seeds a new variant of that mind."""
    return {p.stem: p.read_text(encoding="utf-8").strip() for p in sorted(MINDS_DIR.glob("*.md"))}
