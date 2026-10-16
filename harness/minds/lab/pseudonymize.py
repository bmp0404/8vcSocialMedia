"""Replace private people's @handles in committed experiment data with pseudonyms.

The thread fixtures and the experiment caches hold real replies, and their text mentions other X users.
A handle stays only when the repo already names the account in public config: a mission profile's watch
list, a company account in the dev-tool dataset (data/context/accounts.csv), or the author of a fixture
thread. Every other handle, public figure or not, becomes @personNN,
numbered by first appearance across the files in the order given, so a handle keeps the same pseudonym
everywhere and the gate's handle rules still line up. No mapping is written. Pseudonyms can't be checked
against a guessed handle, as a hash could. The continuation caches' `raw` field (the model's whole page
continuation, kept for debugging and read by nothing) is dropped.

    uv run python -m harness.minds.lab.pseudonymize action/fixtures/x_threads.json ...

Rerun it after fetch_x_threads.py rebuilds a fixture.
"""

import csv
import json
import re
import sys
from pathlib import Path

from harness import config
from harness.sources.watch import load_watch

HANDLE = re.compile(r"(?<![\w@])@(\w{1,15})")
# The dataset's companies stay public when a watch list drops them (sam/watch dropped @cognition).
DATASET_ACCOUNTS = config.ROOT / "data" / "context" / "accounts.csv"


def public_handles(paths: list[Path]) -> set[str]:
    keep = set()
    for name in sorted(p.name for p in config.PROFILES_DIR.iterdir() if p.is_dir()):
        rt = load_watch(config.profile_file("observe.toml", name)).reply_targets
        keep |= {h.lower() for h in rt.accounts + rt.broad_accounts}
    with DATASET_ACCOUNTS.open(encoding="utf-8", newline="") as f:
        keep |= {row["account"].lstrip("@").lower() for row in csv.DictReader(f)}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("threads"), list):
            keep |= {str(t.get("author", "")).lower() for t in data["threads"]}
    return keep - {""}


def pseudonymize(paths: list[Path]) -> dict[Path, int]:
    """Rewrite each file in place; return how many handles each lost."""
    keep, names, changed = public_handles(paths), {}, {}

    def swap(m: re.Match) -> str:
        handle = m.group(1)
        if handle.lower() in keep:
            return m.group(0)
        names.setdefault(handle.lower(), f"person{len(names) + 1:02d}")
        return "@" + names[handle.lower()]

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items() if k != "raw"}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return HANDLE.sub(swap, x) if isinstance(x, str) else x

    for path in paths:
        before = len(names)
        text = path.read_text(encoding="utf-8")
        out = json.dumps(walk(json.loads(text)), indent=1, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")
        path.write_text(out, encoding="utf-8")
        changed[path] = len(names) - before
    return changed


if __name__ == "__main__":
    for path, n in pseudonymize([Path(p) for p in sys.argv[1:]]).items():
        print(f"{path}: {n} new pseudonyms")
