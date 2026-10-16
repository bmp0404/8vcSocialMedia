"""Real replies the reply lane learns from: exemplars for the drafting prompt and references for the judge.

The corpus is a threads file in the shape `harness.minds.lab.fetch_x_threads` writes: real X posts
and their direct replies with likes and the replier's follower count. The default is the fan
account's fixture; point HARNESS_REPLY_CORPUS at another file to run the same code for another
brand (fetch one with `python -m harness.minds.lab.fetch_x_threads <handles>`).
"""

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path

from harness import config

DEFAULT_CORPUS = config.ROOT / "action" / "fixtures" / "x_threads.json"
# Exemplars come from accounts our size or a bit bigger, so the prompt shows what a small
# account can get away with. 2,000 followers is the cut the reply experiment used.
SMALL_ACCOUNT = 2000
MAX_EXEMPLAR_CHARS = 140
CRUDE = re.compile(r"racis|bitch|fuck|fk\b|dick|shit|ass\b|retard|whore", re.I)


def corpus_path() -> Path:
    return Path(os.getenv("HARNESS_REPLY_CORPUS") or DEFAULT_CORPUS)


def label() -> str:
    """The corpus path relative to the repo when it lives inside it, for draft_meta."""
    p = corpus_path().resolve()
    return str(p.relative_to(config.ROOT)) if p.is_relative_to(config.ROOT) else str(p)


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[list[dict], str]:
    p = Path(path)
    if not p.exists():
        return [], ""
    raw = p.read_bytes()
    threads = json.loads(raw.decode("utf-8"))["threads"]
    return threads, hashlib.sha256(raw).hexdigest()


def threads() -> list[dict]:
    return _load(str(corpus_path()))[0]


def _usable(text: str) -> bool:
    return len(text) >= 4 and not text.startswith(("http", "@")) and not CRUDE.search(text)


def _pool(exclude_url: str | None) -> list[tuple[dict, dict]]:
    return [(t, r) for t in threads() if t["url"] != exclude_url for r in t["direct_replies"] if _usable(r["text"])]


def exemplars(exclude_url: str | None = None, k: int = 12) -> list[dict]:
    """The k most-liked short replies by small accounts, leaving out the thread being answered."""
    pool = [r for _, r in _pool(exclude_url)
            if (r["replier_followers"] or 0) < SMALL_ACCOUNT and len(r["text"]) <= MAX_EXEMPLAR_CHARS]
    return sorted(pool, key=lambda r: -r["likes"])[:k]


def references(exclude_url: str | None = None, per_class: int = 3) -> list[dict]:
    """Judge references in post_judge's ReferencePost shape: top small-account replies as
    Successful, zero-like replies as Poor. Empty when the corpus is missing."""
    path = corpus_path()
    _, sha = _load(str(path))
    good = exemplars(exclude_url, k=per_class)
    poor = sorted((r for t, r in _pool(exclude_url) if r["likes"] == 0),
                  key=lambda r: hashlib.sha256(r["text"].encode()).hexdigest())[:per_class]
    if len(good) < per_class or len(poor) < per_class:
        return []

    def ref(r: dict, i: int, perf: str) -> dict:
        return {"id": f"{perf[0]}{i + 1}", "text": r["text"], "account": "x_reply", "performance": perf,
                "category": "x_reply", "engagement": r["likes"], "media": "none", "date": "", "corpus_sha256": sha}

    return [ref(r, i, "Successful") for i, r in enumerate(good)] + [ref(r, i, "Poor") for i, r in enumerate(poor)]
