"""The interface this package reads and writes memories through, plus two stand-ins.

Krish's markdown store (`harness/memstore/`) is the real backend, reached through
`memstore_adapter.MemstoreAdapter`. Until his code is on this branch, `FakeStore` keeps memories in a
dict for tests and `FileFakeStore` saves that dict to `out/` so CLI commands persist between runs.
"""

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from harness import config


@dataclass
class Memory:
    """One stored memory: a kind, a markdown body, and frontmatter. `score` is set by search."""

    id: str
    kind: str
    body: str
    meta: dict = field(default_factory=dict)
    score: float = 0.0


class MemoryStore(Protocol):
    def write_memory(self, kind: str, body: str, frontmatter: dict) -> str:
        """Create or replace a memory. A frontmatter `id` makes the write idempotent. Returns the id."""

    def search(self, query: str, k: int = 10) -> list[Memory]:
        """Keyword search over every memory, best first."""

    def list_memories(self, kind: str | None = None) -> list[Memory]:
        """Every memory, or every memory of one kind."""


STOPWORDS = set(
    "a an and are as at be but by for from has have in into is it its of on or that the this to was "
    "we were will with you your our not no so if then than they them their there here what which who "
    "how why when can just now new".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in STOPWORDS]


def _index_text(m: Memory) -> str:
    extra = [str(m.meta.get(key, "")) for key in ("author_username", "subject", "name", "tags")]
    return " ".join([m.body, m.kind, *extra])


class FakeStore:
    """In-memory store with BM25 keyword search (k1=1.2, b=0.75, the usual defaults)."""

    def __init__(self) -> None:
        self.items: dict[str, Memory] = {}
        self._tokens: dict[str, Counter] = {}
        self._counter = 0

    def write_memory(self, kind: str, body: str, frontmatter: dict) -> str:
        meta = dict(frontmatter)
        mid = meta.get("id")
        if not mid:
            self._counter += 1
            mid = f"{kind}-{self._counter:05d}"
        meta["id"], meta["kind"] = mid, kind
        self._put(Memory(id=mid, kind=kind, body=body, meta=meta))
        return mid

    def _put(self, m: Memory) -> None:
        self.items[m.id] = m
        self._tokens[m.id] = Counter(tokenize(_index_text(m)))

    def list_memories(self, kind: str | None = None) -> list[Memory]:
        return [m for m in self.items.values() if kind is None or m.kind == kind]

    def search(self, query: str, k: int = 10) -> list[Memory]:
        terms = set(tokenize(query))
        if not terms or not self.items:
            return []
        docs = self._tokens
        n = len(docs)
        avg_len = sum(sum(c.values()) for c in docs.values()) / n or 1.0
        df = Counter(t for c in docs.values() for t in set(c) if t in terms)
        k1, b = 1.2, 0.75
        scored = []
        for mid, counts in docs.items():
            length = sum(counts.values())
            s = 0.0
            for t in terms & set(counts):
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                tf = counts[t]
                s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg_len))
            if s > 0:
                scored.append((s, mid))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out = []
        for s, mid in scored[:k]:
            m = self.items[mid]
            out.append(Memory(id=m.id, kind=m.kind, body=m.body, meta=dict(m.meta), score=s))
        return out


class FileFakeStore(FakeStore):
    """FakeStore saved as one JSON file, so `ingest`, `reflect` and `recall` can run as separate commands."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        if path.exists():
            for row in json.loads(path.read_text(encoding="utf-8")):
                self._put(Memory(**row))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [asdict(m) for m in self.items.values()]
        self.path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")


FAKE_PATH = config.OUT_DIR / "memory-fake.json"


def get_store():
    """Krish's store when `harness.memstore` is importable, else the JSON-file stand-in in out/."""
    try:
        from harness.learning.memstore_adapter import MemstoreAdapter

        return MemstoreAdapter()
    except ModuleNotFoundError as e:
        if e.name != "harness.memstore":
            raise
        print(f"[memory] harness.memstore not on this branch; using {FAKE_PATH}")
        return FileFakeStore(FAKE_PATH)


def flush(store) -> None:
    """Persist the JSON stand-in; Krish's store writes each note as it goes."""
    if isinstance(store, FileFakeStore):
        store.save()
