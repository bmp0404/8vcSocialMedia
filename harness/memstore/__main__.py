"""Memory store CLI: python -m harness.memstore <selftest|search|show|round-end|snapshot>."""

import argparse
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

from harness import config
from harness.memstore.note import TYPES

SEED_DIR = config.ROOT / "memory" / "seed"
MODULES = ("note", "store", "search", "prompt", "episodes")


def selftest(args) -> None:
    # Every selftest runs against a seeded temp store, never memory/store/.
    root = Path(tempfile.mkdtemp(prefix="memstore-selftest-")) / "store"
    if SEED_DIR.is_dir():
        shutil.copytree(SEED_DIR, root)
    else:
        root.mkdir()
    for name in MODULES:
        try:
            module = importlib.import_module(f"harness.memstore.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"harness.memstore.{name}":
                raise
            print(f"selftest_{name}: skipped, module not built yet")
            continue
        for attr in sorted(a for a in dir(module) if a.startswith("selftest_")):
            fn = getattr(module, attr)
            if callable(fn) and fn.__module__ == module.__name__:
                os.environ["MEMSTORE_PATH"] = str(root)
                fn()
    print("selftest: ok")


def search(args) -> None:
    from harness import memstore

    types = args.types.split(",") if args.types else None
    for hit in memstore.search(args.query, k=args.k, types=types):
        print(f"{hit['score']:>6.1f}  {hit['id']}  ({hit['type']})  {hit['title']}")
        for line in hit["snippets"]:
            print(f"        {line}")


def show(args) -> None:
    from harness import memstore

    note = memstore.load(args.id)
    if note is None:
        raise SystemExit(f"no note with id {args.id!r}")
    print(memstore.dump(note))


def round_end(args) -> None:
    from harness.memstore.episodes import round_end

    ids = round_end(args.run_id, provider=args.provider)
    print(f"[round-end] wrote {len(ids)} episode notes" + (": " + ", ".join(ids) if ids else ""))


def snapshot(args) -> None:
    # The live store, never the mock temp store: snapshotting a throwaway store would be a mistake.
    src = Path(os.environ["MEMSTORE_PATH"]) if os.getenv("MEMSTORE_PATH") else config.ROOT / "memory" / "store"
    if not src.is_dir():
        raise SystemExit(f"no store at {src}")
    n = 0
    for folder in TYPES.values():
        for path in sorted((src / folder).glob("*.md")):
            dest = SEED_DIR / folder / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            n += 1
    print(f"[snapshot] copied {n} notes from {src} to {SEED_DIR}; review and commit memory/seed/")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m harness.memstore", description="Markdown memory store")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("selftest", help="run every memstore selftest against a temp store")
    p.set_defaults(func=selftest)

    p = sub.add_parser("search", help="ranked keyword search")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--types", help="comma-separated note types")
    p.set_defaults(func=search)

    p = sub.add_parser("show", help="print one note")
    p.add_argument("id")
    p.set_defaults(func=show)

    p = sub.add_parser("round-end", help="write episode notes for a run's actions")
    p.add_argument("run_id", type=int)
    p.add_argument("--provider", default=config.PROVIDER, help="mock writes to a temp store unless MEMSTORE_PATH is set")
    p.set_defaults(func=round_end)

    p = sub.add_parser("snapshot", help="copy the live store into memory/seed/ for a deliberate commit")
    p.set_defaults(func=snapshot)

    args = parser.parse_args()
    try:
        args.func(args)
    except NotImplementedError as e:
        sys.exit(f"not built yet: {e}")


if __name__ == "__main__":
    main()
