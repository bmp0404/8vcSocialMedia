"""Markdown memory store. See memory/README.md and memory/IMPLEMENTATION.md.

note.py holds the format and store_root(); store.py holds reads and the one
validated write path. The remaining stubs are replaced by the search and
round-end packages.
"""

from harness.memstore.note import (
    TYPES,
    MemoryValidationError,
    Note,
    dump,
    parse,
    selftest_note,
    store_root,
)
from harness.memstore.store import delete_note, list_notes, load, selftest_store, write_note

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
    "selftest_store",
    "store_root",
    "write_note",
]


def search(query: str, k: int = 10, types: list[str] | None = None) -> list[dict]:
    raise NotImplementedError("search lands in P2 (search.py)")


def context_for(text: str, k: int = 5, types: list[str] | None = None) -> str:
    raise NotImplementedError("context_for lands in P2 (prompt.py)")


def prompt_block(max_chars: int = 6000) -> str:
    raise NotImplementedError("prompt_block lands in P2 (prompt.py)")


def round_end(run_id: int, *, provider: str) -> list[str]:
    raise NotImplementedError("round_end lands in P4 (episodes.py)")
