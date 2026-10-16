"""Markdown memory store. See memory/README.md and memory/IMPLEMENTATION.md.

P0 ships the format (note.py) and store_root(). The other functions are stubs
that the store, search and round-end packages replace.
"""

from pathlib import Path

from harness.memstore.note import (
    TYPES,
    MemoryValidationError,
    Note,
    dump,
    parse,
    selftest_note,
    store_root,
)

__all__ = [
    "TYPES",
    "MemoryValidationError",
    "Note",
    "context_for",
    "delete_note",
    "dump",
    "list_notes",
    "load",
    "parse",
    "prompt_block",
    "round_end",
    "search",
    "selftest_note",
    "store_root",
    "write_note",
]


def load(id: str) -> Note | None:
    raise NotImplementedError("load lands in P1 (store.py)")


def list_notes(type: str | None = None) -> list[Note]:
    raise NotImplementedError("list_notes lands in P1 (store.py)")


def write_note(note: Note) -> Path:
    raise NotImplementedError("write_note lands in P1 (store.py)")


def delete_note(id: str) -> bool:
    raise NotImplementedError("delete_note lands in P1 (store.py)")


def search(query: str, k: int = 10, types: list[str] | None = None) -> list[dict]:
    raise NotImplementedError("search lands in P2 (search.py)")


def context_for(text: str, k: int = 5, types: list[str] | None = None) -> str:
    raise NotImplementedError("context_for lands in P2 (prompt.py)")


def prompt_block(max_chars: int = 6000) -> str:
    raise NotImplementedError("prompt_block lands in P2 (prompt.py)")


def round_end(run_id: int, *, provider: str) -> list[str]:
    raise NotImplementedError("round_end lands in P4 (episodes.py)")
