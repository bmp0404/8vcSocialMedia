import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Each can point elsewhere, so a test or a throwaway run keeps the checkout's data/ and out/ clean.
DB_PATH = Path(os.getenv("HARNESS_DB_PATH") or ROOT / "data" / "harness.db")
OUT_DIR = Path(os.getenv("HARNESS_OUT_DIR") or ROOT / "out")
SAMPLE_SIGNALS_PATH = ROOT / "data" / "sample_signals.json"

PROVIDER = os.getenv("HARNESS_PROVIDER", "mock").lower()
MODEL = os.getenv("HARNESS_MODEL", "")
SOURCES = [s.strip() for s in os.getenv("HARNESS_SOURCES", "sample").split(",") if s.strip()]

# Mission profiles. mission/profiles/<name>/ holds one brand's mission.md, observe.toml, panel.json,
# reply_framing.json and prompts.toml (the brand wording the action prompts insert), plus an optional takes.json. HARNESS_PROFILE picks the profile; use_profile() switches
# it for a block, so one process can work for two brands. Every reader of these files goes through
# profile_file(), which raises ProfileError naming the path when a profile or file is missing.
PROFILES_DIR = ROOT / "mission" / "profiles"
DEFAULT_PROFILE = "wnba"
PROFILE_FILES = ("mission.md", "observe.toml", "panel.json", "reply_framing.json", "prompts.toml")
# Each points one profile file somewhere else, for tests and one-off runs; it applies to the active profile.
FILE_OVERRIDES = {"observe.toml": "HARNESS_OBSERVE_CONFIG", "panel.json": "HARNESS_PANEL",
                  "reply_framing.json": "HARNESS_REPLY_FRAMING"}
# Memory is per profile too: memory/seed/<name>/, memory/store/<name>/ and memory/lessons/<name>.md.
MEMORY_SEED_DIR = ROOT / "memory" / "seed"
MEMORY_STORE_DIR = ROOT / "memory" / "store"
MEMORY_LESSONS_DIR = ROOT / "memory" / "lessons"
# Set by the engine's isolated runs and by tests; None means the active profile's lessons file.
LESSONS_PATH: Path | None = None

_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_active_profile: ContextVar[str | None] = ContextVar("harness_profile", default=None)


class ProfileError(FileNotFoundError):
    """A mission profile, or one of its files, is missing or misnamed."""


def _check_name(name: str) -> str:
    if not _PROFILE_NAME.match(name):
        raise ProfileError(f"profile name {name!r} must match {_PROFILE_NAME.pattern}")
    return name


def profile_name() -> str:
    """The profile use_profile() set for this block, else HARNESS_PROFILE, else wnba."""
    return _check_name(_active_profile.get() or os.getenv("HARNESS_PROFILE") or DEFAULT_PROFILE)


def profile_dir(name: str | None = None) -> Path:
    path = PROFILES_DIR / (_check_name(name) if name else profile_name())
    if not path.is_dir():
        have = sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir()) if PROFILES_DIR.is_dir() else []
        raise ProfileError(f"no mission profile at {path}; HARNESS_PROFILE must name one of {have}")
    return path


def profile_file(filename: str, name: str | None = None) -> Path:
    """The profile's `filename`, or the path its override variable names when `name` isn't given."""
    if filename not in PROFILE_FILES:
        raise ValueError(f"{filename!r} isn't a profile file; expected one of {PROFILE_FILES}")
    variable = FILE_OVERRIDES.get(filename)
    override = os.getenv(variable, "") if variable and name is None else ""
    path = Path(override) if override else profile_dir(name) / filename
    if not path.is_file():
        raise ProfileError(f"{filename} not found at {path}" + (f" ({variable} points there)" if override else ""))
    return path


@contextmanager
def use_profile(name: str):
    """Make `name` the active profile inside the block. Fails on entry when the profile doesn't exist."""
    profile_dir(name)
    token = _active_profile.set(name)
    try:
        yield
    finally:
        _active_profile.reset(token)


def memory_seed_dir(name: str | None = None) -> Path:
    """The notes a new store starts from. A profile without one starts empty."""
    return MEMORY_SEED_DIR / (_check_name(name) if name else profile_name())


def memory_store_dir(name: str | None = None) -> Path:
    return MEMORY_STORE_DIR / (_check_name(name) if name else profile_name())


def lessons_path(name: str | None = None) -> Path:
    if LESSONS_PATH is not None and name is None:
        return LESSONS_PATH
    return MEMORY_LESSONS_DIR / f"{_check_name(name) if name else profile_name()}.md"


def mission() -> str:
    return profile_file("mission.md").read_text(encoding="utf-8")


def lessons() -> str:
    from harness.learning.memstore_adapter import written_by_learning
    from harness.memstore import MemoryValidationError, prompt_block

    path = lessons_path()
    text = path.read_text(encoding="utf-8") if path.exists() else "(no lessons yet)"
    # The lessons file is learn's rendering of the store's tested lessons; the block adds the store's other notes.
    try:
        block = prompt_block(exclude=written_by_learning)
    except (OSError, MemoryValidationError) as e:
        # A store that can't be read costs this round its memory, not the round. Anything else is a bug and raises.
        print(f"[memory] skipped the memory store this round: {type(e).__name__}: {e}")
        block = ""
    return text + "\n\n" + block if block else text
