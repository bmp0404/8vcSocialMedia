"""The one place this package touches Krish's store (`harness/memstore/`, contract in `memory/IMPLEMENTATION.md`).

`MemstoreAdapter` gives his module the `MemoryStore` interface in `store.py`: `write_memory` goes
through his `write_note`, `search` through his `search` plus `load`, `list_memories` through
`list_notes`.

His frontmatter is one `key: value` per line, lists are inline and split on commas, and every value
comes back as a string except `confidence` and lists. Our records hold nested dicts, numbers, booleans
and post text with commas and newlines, none of which survive that. So each note carries:

- the keys his store reads, written plainly: the required six, `evidence`, `confidence`, `source`,
  `supersedes`, `valid_until`, `tags`, and `quote` on facts;
- `record`: the whole frontmatter as JSON, base64url-encoded so it contains no comma, colon, bracket,
  quote or newline for his parser to split on. Reading decodes `record` and nothing else, so the round
  trip is exact; `tests/test_learning_kinds.py` fails on any lost or changed field.
"""

import base64
import importlib
import json
import re
from dataclasses import dataclass, field

from harness.learning.store import Memory

REQUIRED = ("id", "type", "title", "aliases", "created", "updated")
PLAIN_OPTIONAL = ("evidence", "confidence", "source", "supersedes", "valid_until", "tags")
PLAIN_BY_TYPE = {"fact": ("quote",)}


def encode_record(frontmatter: dict) -> str:
    raw = json.dumps(frontmatter, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_record(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8"))


def _title(text: str) -> str:
    """One line, no double quotes, so his writer can quote it."""
    return re.sub(r"\s+", " ", str(text)).replace('"', "'").strip()[:120]


def to_note(kind: str, body: str, frontmatter: dict, note_cls):
    fm = {k: v for k, v in frontmatter.items() if k != "kind"}
    fm["type"] = kind
    missing = [k for k in REQUIRED if not fm.get(k)]
    if missing:
        raise ValueError(f"note {fm.get('id')!r} is missing required keys {missing}")
    meta = {}
    for key in PLAIN_OPTIONAL + PLAIN_BY_TYPE.get(kind, ()):
        value = fm.get(key)
        if value in (None, "", []):
            continue
        if key == "confidence":
            value = max(0.0, min(1.0, float(value)))
        elif isinstance(value, list):
            value = [str(v) for v in value if str(v)]
        else:
            value = str(value)
        meta[key] = value
    meta["record"] = encode_record(fm)
    return note_cls(id=fm["id"], type=kind, title=_title(fm["title"]), aliases=[str(a) for a in fm["aliases"]],
                    created=str(fm["created"]), updated=str(fm["updated"]), body=body, meta=meta)


def from_note(note, score: float = 0.0) -> Memory:
    record = (note.meta or {}).get("record")
    if not record:
        raise ValueError(f"note {note.id!r} has no `record`; it wasn't written by harness.learning")
    meta = decode_record(record)
    meta["kind"] = note.type
    return Memory(id=note.id, kind=note.type, body=note.body, meta=meta, score=score)


@dataclass
class StubNote:
    """The Note dataclass from memory/IMPLEMENTATION.md, for tests while harness.memstore isn't on this branch."""

    id: str
    type: str
    title: str
    aliases: list[str]
    created: str
    updated: str
    body: str
    meta: dict = field(default_factory=dict)
    path: object = None


class MemstoreAdapter:
    """MemoryStore over harness.memstore. Notes it didn't write (no `record`) are skipped by list and search."""

    def __init__(self, module=None) -> None:
        self.ms = module or importlib.import_module("harness.memstore")

    def write_memory(self, kind: str, body: str, frontmatter: dict) -> str:
        note = to_note(kind, body, frontmatter, self.ms.Note)
        self.ms.write_note(note)  # raises MemoryValidationError; let it propagate
        return note.id

    def search(self, query: str, k: int = 10) -> list[Memory]:
        out = []
        for hit in self.ms.search(query, k=k):
            note = self.ms.load(hit["id"])
            if note is None:
                raise KeyError(f"memstore.search returned {hit['id']!r} but load() can't find it")
            if written_by_learning(note):
                out.append(from_note(note, float(hit.get("score", 0.0))))
        return out

    def list_memories(self, kind: str | None = None) -> list[Memory]:
        return [from_note(n) for n in self.ms.list_notes(kind) if written_by_learning(n)]

    def write_run_episodes(self, run_ids: list[int]) -> list[str]:
        """His round_end for each run: one ep-<action id> episode per draft, carrying its review and outcome.

        round_end picks its store by provider; config.PROVIDER is the one write_note resolves, so both land in
        the same store.
        """
        from harness import config

        return [nid for run_id in run_ids for nid in self.ms.round_end(run_id, provider=config.PROVIDER)]


def written_by_learning(note) -> bool:
    """True for a note this package wrote, which carries `record`.

    config.lessons() keeps these out of prompt_block(): memory/lessons.md already lists this package's lessons
    with their held-out results, and its procedures and episodes belong to one mind or one narrative.
    """
    return bool((note.meta or {}).get("record"))
