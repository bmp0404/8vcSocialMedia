import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MISSION_PATH = ROOT / "mission" / "mission.md"
LESSONS_PATH = ROOT / "memory" / "lessons.md"
DB_PATH = ROOT / "data" / "harness.db"
OUT_DIR = ROOT / "out"
SAMPLE_SIGNALS_PATH = ROOT / "data" / "sample_signals.json"

PROVIDER = os.getenv("HARNESS_PROVIDER", "mock").lower()
MODEL = os.getenv("HARNESS_MODEL", "")
SOURCES = [s.strip() for s in os.getenv("HARNESS_SOURCES", "sample").split(",") if s.strip()]


def mission() -> str:
    return MISSION_PATH.read_text(encoding="utf-8")


def lessons() -> str:
    return LESSONS_PATH.read_text(encoding="utf-8") if LESSONS_PATH.exists() else "(no lessons yet)"
