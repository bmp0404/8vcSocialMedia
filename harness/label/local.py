"""Restore the gitignored annotator rationales from a local git ref that still holds the full rows.

The public branch commits LLM answers with an empty `rationale`. The unfiltered rows exist only in
the local, never-pushed branch `sam/corpus`; this copies their rationales into `data/labels/rationales/`.
"""

import json
import subprocess
from pathlib import Path

from harness import config
from harness.label.annotate import RATIONALES_DIR


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(config.ROOT), *args], capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def rationales_from_ref(ref: str, out_dir: Path = RATIONALES_DIR) -> dict[str, int]:
    """Run file name -> rationales written. Raises CalledProcessError if the ref is unknown."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for path in _git("ls-tree", "-r", "--name-only", ref, "data/labels/llm", "data/labels/screening").split():
        if not path.endswith(".jsonl"):
            continue
        rows = [json.loads(line) for line in _git("show", f"{ref}:{path}").splitlines() if line.strip()]
        keep = [{k: r[k] for k in ("item_id", "run_id", "at", "rationale")} for r in rows if r.get("rationale")]
        if keep:
            (out_dir / Path(path).name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
                                                   encoding="utf-8")
            written[Path(path).name] = len(keep)
    return written
