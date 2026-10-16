"""Disk cache + call counters for evaluator LLM / decision-model calls, so backtest reruns are cheap.

Entries live in out/eval_cache/<sha256>.json (gitignored). Disable with HARNESS_EVAL_CACHE=0.
`STATS` counts calls vs cache hits per label (e.g. "claude:claude-haiku-5-5", "jev").
"""

import hashlib
import json
import os
import threading
from collections import Counter
from typing import Any, Callable

from harness import config
from harness.llm import LLM, MockLLM, parse_json

CACHE_DIR = config.ROOT / "out" / "eval_cache"
ENABLED = os.getenv("HARNESS_EVAL_CACHE", "1").strip() not in {"0", "false", "no"}
STATS: Counter = Counter()
_lock = threading.Lock()


def key(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(k: str) -> Any | None:
    if not ENABLED:
        return None
    p = CACHE_DIR / f"{k}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def put(k: str, value: Any) -> None:
    if not ENABLED:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / f"{k}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE_DIR / f"{k}.json")


def drop(k: str) -> None:
    try:
        (CACHE_DIR / f"{k}.json").unlink()
    except OSError:
        pass


def count(label: str, hit: bool) -> None:
    with _lock:
        STATS[f"{label}:{'hit' if hit else 'call'}"] += 1


def cached(label: str, k: str, fn: Callable[[], Any]) -> tuple[Any, bool]:
    """(value, was_hit). `fn` runs only on a miss; its JSON-serializable result is stored."""
    v = get(k)
    if v is not None:
        count(label, True)
        return v, True
    v = fn()
    count(label, False)
    put(k, v)
    return v, False


class CachedLLM(LLM):
    """Wraps any harness LLM; caches raw completions keyed by (label, system, prompt)."""

    def __init__(self, inner: LLM, label: str):
        self.inner, self.label = inner, label

    @property
    def is_mock(self) -> bool:
        return isinstance(self.inner, MockLLM)

    def complete(self, system: str, prompt: str) -> str:
        text, _ = cached(self.label, key(self.label, system, prompt), lambda: self.inner.complete(system, prompt))
        return text

    def complete_json(self, system: str, prompt: str, mock: Callable[[], Any] | None = None) -> Any:
        if self.is_mock:
            return self.inner.complete_json(system, prompt, mock)
        system += "\n\nRespond with JSON only, no prose."
        try:
            return parse_json(self.complete(system, prompt))
        except ValueError:  # bad output was cached: drop it and retry once
            drop(key(self.label, system, prompt))
            return parse_json(self.complete(system, prompt))
