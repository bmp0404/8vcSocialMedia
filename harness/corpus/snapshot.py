"""Copy the context worker's raw fetch files into `data/corpus/raw/` as gzip, with a manifest.

The raw `.jsonl` files are gitignored on `sam/context-dynamics` and a tracker process keeps writing
next to them, so the corpus reads a frozen, committed copy instead. Rebuild it with:

    uv run python -m harness.corpus snapshot --src <repo>/data/context
"""

import gzip
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from harness import config

RAW_DIR = config.ROOT / "data" / "corpus" / "raw"

# File name -> what it holds. Every one is required: a missing input is an error, not an empty corpus.
# reposters.jsonl is left out: nothing here reads it, and it is 16,000 strangers' bios in a public repo.
FILES = {
    "x_metrics.jsonl": "the dataset posts (data/devtools_virality_30d.csv) re-read through fxtwitter",
    "author_timelines.jsonl": "posts from the dataset accounts' timelines, reposts included",
    "reply_samples.jsonl": "replies to dataset posts, first page in likes and recency order, merged",
    "boost_flags.csv": "boost, thread-continuation and maturity flags for the dataset posts",
    "timeline_posts.csv": "the accounts' own posts in the window with enriched columns and flags",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(src: Path, dest: Path = RAW_DIR) -> dict:
    src, dest = Path(src), Path(dest)
    missing = [name for name in FILES if not (src / name).is_file()]
    if missing:
        raise FileNotFoundError(f"snapshot source {src} lacks {', '.join(missing)}")
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {"source_dir": str(src), "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": {}}
    for name, about in FILES.items():
        raw = src / name
        out = dest / f"{name}.gz"
        # mtime=0 keeps the gzip bytes identical across rebuilds of the same input.
        with raw.open("rb") as fin, out.open("wb") as fraw, gzip.GzipFile(fileobj=fraw, mode="wb", mtime=0) as fout:
            shutil.copyfileobj(fin, fout)
        with raw.open("rb") as f:
            lines = sum(1 for _ in f)
        manifest["files"][name] = {"about": about, "sha256": _sha256(raw), "lines": lines,
                                   "bytes": raw.stat().st_size, "gz_bytes": out.stat().st_size}
    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(dest: Path = RAW_DIR) -> list[str]:
    """Return a list of problems; empty means every gzip decompresses to the bytes the manifest recorded."""
    dest = Path(dest)
    manifest = json.loads((dest / "MANIFEST.json").read_text(encoding="utf-8"))
    problems = []
    for name, meta in manifest["files"].items():
        h = hashlib.sha256()
        with gzip.open(dest / f"{name}.gz", "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != meta["sha256"]:
            problems.append(f"{name}: sha256 differs from MANIFEST.json")
    return problems
