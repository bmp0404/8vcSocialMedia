"""Scope legacy component configuration to one serial engine execution."""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from harness import config


@contextmanager
def runtime(provider: str, sources: list[str], state_dir: Path | None = None):
    """Mock executions use isolated storage retained for inspection after the run."""
    names = ("PROVIDER", "SOURCES", "DB_PATH", "OUT_DIR", "LESSONS_PATH")
    previous = {name: getattr(config, name) for name in names}
    previous_store = os.environ.get("MEMSTORE_PATH")
    if provider == "mock" and state_dir is None:
        state_dir = Path(tempfile.mkdtemp(prefix="media-engine-mock-"))
    try:
        config.PROVIDER, config.SOURCES = provider, sources
        if state_dir is not None:
            state_dir = state_dir.resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            config.DB_PATH = state_dir / "harness.db"
            config.OUT_DIR = state_dir / "out"
            config.LESSONS_PATH = state_dir / "lessons.md"
            os.environ["MEMSTORE_PATH"] = str(state_dir / "store")
        config.OUT_DIR.mkdir(parents=True, exist_ok=True)
        yield config.OUT_DIR
    finally:
        for name, value in previous.items():
            setattr(config, name, value)
        if previous_store is None:
            os.environ.pop("MEMSTORE_PATH", None)
        else:
            os.environ["MEMSTORE_PATH"] = previous_store
