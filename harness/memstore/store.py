"""The memory store on disk: reads, the one validated write path, seeding and the lock.

Notes live at <store>/<folder>/<id>.md. Reads take no lock. Every write goes
through write_note() or delete_note(), which hold <store>/.lock while they
validate and change files.
"""

import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path

from harness import config
from harness.memstore.note import TYPES, MemoryValidationError, Note, dump, parse, store_root

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_BODY = 8000
LOCK_TIMEOUT = 5.0
LOCK_RETRY = 0.1

_seeded: set[Path] = set()

# ---------- store path, lock, seeding ----------


@contextmanager
def _lock(root: Path):
    path = root / ".lock"
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"memstore lock {path} still held after {LOCK_TIMEOUT:.0f}s; "
                    "delete it if no other process is writing"
                ) from None
            time.sleep(LOCK_RETRY)
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def _is_empty(root: Path) -> bool:
    return not any(p.name != ".lock" for p in root.iterdir())


def _seed(root: Path) -> None:
    """Copy the profile's memory/seed/<profile>/ into an empty store: copy to <store>.tmp, then os.replace
    into place. A profile without a seed directory starts empty."""
    seed_dir = config.memory_seed_dir()
    root.mkdir(parents=True, exist_ok=True)
    if not _is_empty(root) or not seed_dir.is_dir():
        return
    with _lock(root):
        if not _is_empty(root):  # another process seeded while we waited
            return
        tmp = root.with_name(root.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(seed_dir, tmp)
        # The store dir already holds .lock, so move the copied entries in one by one.
        for entry in tmp.iterdir():
            os.replace(entry, root / entry.name)
        shutil.rmtree(tmp, ignore_errors=True)


def _root() -> Path:
    root = store_root()
    if root not in _seeded:
        _seed(root)
        _seeded.add(root)
    return root


# ---------- reads ----------


def _note_path(root: Path, type: str, id: str) -> Path:
    return root / TYPES[type] / f"{id}.md"


def _find(root: Path, id: str) -> Path | None:
    for folder in TYPES.values():
        path = root / folder / f"{id}.md"
        if path.is_file():
            return path
    return None


def _read(path: Path) -> Note:
    return parse(path.read_text(encoding="utf-8"), path)


def load(id: str) -> Note | None:
    """Return the note with this id, or None if there is none."""
    if not isinstance(id, str) or not ID_RE.match(id):
        return None
    path = _find(_root(), id)
    return _read(path) if path else None


def list_notes(type: str | None = None) -> list[Note]:
    """All notes, or all notes of one type, sorted by id. Unreadable files are reported and skipped."""
    if type is not None and type not in TYPES:
        raise ValueError(f"unknown note type {type!r}; expected one of {sorted(TYPES)}")
    root = _root()
    folders = [TYPES[type]] if type else list(TYPES.values())
    notes = []
    for folder in folders:
        for path in sorted((root / folder).glob("*.md")):
            try:
                notes.append(_read(path))
            except Exception as e:
                print(f"memstore: skipping {path}: {e}", file=sys.stderr)
    return sorted(notes, key=lambda n: n.id)


# ---------- validation ----------


def _check_date(name: str, value) -> None:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise MemoryValidationError(f"{name} must be YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise MemoryValidationError(f"{name} is not a real date: {value!r}") from None


def _check_evidence(root: Path, note: Note) -> None:
    evidence = note.meta.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    for eid in evidence:
        if not isinstance(eid, str) or not ID_RE.match(eid):
            continue
        path = _find(root, eid)
        if not path:
            continue
        ep = _read(path)
        if ep.type != "episode":
            continue
        source = ep.meta.get("source")
        if source == "dataset":
            return
        if source == "run" and (ep.meta.get("outcome") or ep.meta.get("review_status")):
            return
    raise MemoryValidationError(
        f"{note.type} {note.id!r} needs evidence citing at least one episode with "
        "source: dataset, or source: run with an outcome or review_status; "
        f"got evidence={list(evidence)}. Predicted panel scores alone don't count."
    )


def _validate(root: Path, note: Note) -> Path:
    """Raise MemoryValidationError if the note breaks a rule. Return the path it belongs at."""
    for key in ("id", "type", "title", "created", "updated"):
        value = getattr(note, key, None)
        if not isinstance(value, str) or not value.strip():
            raise MemoryValidationError(f"missing required key {key!r}")
    if not isinstance(note.aliases, list) or not note.aliases:
        raise MemoryValidationError(f"note {note.id!r} needs at least one alias")
    if not all(isinstance(a, str) and a.strip() for a in note.aliases):
        raise MemoryValidationError(f"note {note.id!r} has an empty or non-string alias")
    for key, value in [("title", note.title), *(("alias", a) for a in note.aliases)]:
        if "\n" in value or "\r" in value:
            raise MemoryValidationError(f"{key} {value!r} must be one line")

    if note.type not in TYPES:
        raise MemoryValidationError(f"unknown type {note.type!r}; expected one of {sorted(TYPES)}")
    if not ID_RE.match(note.id):
        raise MemoryValidationError(f"bad id {note.id!r}; ids match {ID_RE.pattern}")

    target = _note_path(root, note.type, note.id)
    if note.path is not None and Path(note.path).resolve() != target.resolve():
        raise MemoryValidationError(f"note {note.id!r} has path {note.path}, expected {target}")
    existing = _find(root, note.id)
    if existing and existing.resolve() != target.resolve():
        raise MemoryValidationError(f"id {note.id!r} is already used by {existing}")

    _check_date("created", note.created)
    _check_date("updated", note.updated)
    if note.meta.get("valid_until"):
        _check_date("valid_until", note.meta["valid_until"])

    if "confidence" in note.meta:
        try:
            confidence = float(note.meta["confidence"])
        except (TypeError, ValueError):
            raise MemoryValidationError(f"confidence must be a number, got {note.meta['confidence']!r}") from None
        if not 0 <= confidence <= 1:
            raise MemoryValidationError(f"confidence must be between 0 and 1, got {confidence}")

    if len(note.body or "") > MAX_BODY:
        raise MemoryValidationError(f"body is {len(note.body)} characters; the cap is {MAX_BODY}")

    if note.type == "episode" and note.meta.get("source") not in ("run", "dataset"):
        raise MemoryValidationError(
            f"episode {note.id!r} needs source: run or source: dataset, got {note.meta.get('source')!r}"
        )
    if note.type in ("lesson", "reflection"):
        _check_evidence(root, note)
    return target


# ---------- writes ----------


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_note(note: Note) -> Path:
    """Validate and write one note. The only way to add or change memory.

    Raises MemoryValidationError and leaves the store unchanged if the note
    breaks a rule. Rewriting an existing id replaces it and sets updated to today.
    """
    root = _root()
    with _lock(root):
        target = _validate(root, note)
        if target.exists():
            note = replace(note, updated=date.today().isoformat())
        _write_atomic(target, dump(replace(note, path=target)))
    return target


def delete_note(id: str) -> bool:
    """Delete the note with this id. Return False if there was none."""
    if not isinstance(id, str) or not ID_RE.match(id):
        return False
    root = _root()
    with _lock(root):
        path = _find(root, id)
        if not path:
            return False
        path.unlink()
        return True


# ---------- selftest ----------


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != ".lock"
    }


def selftest_store() -> None:
    """Acceptance checks 1-3, run against a throwaway MEMSTORE_PATH."""
    old_env = os.environ.get("MEMSTORE_PATH")
    tmp = Path(tempfile.mkdtemp(prefix="memstore-selftest-"))
    os.environ["MEMSTORE_PATH"] = str(tmp / "store")
    try:
        root = _root()
        assert root.resolve() == (tmp / "store").resolve(), f"store root ignored MEMSTORE_PATH: {root}"
        today = date.today().isoformat()

        def make(id, type, **meta):
            return Note(
                id=id, type=type, title=f"Selftest {id}", aliases=[id, "selftest"],
                created="2026-10-15", updated="2026-10-15",
                body="- **Finding:** selftest note; stated on 2026-10-15.", meta=meta,
            )

        # 1. Round trip, with an unknown key and non-ASCII text.
        rt = make("selftest-roundtrip", "fact", source="selftest", mood="cheerful")
        rt.body = "- **Quote:** Clark's logo three, café über 💯; stated on 2026-10-15."
        path = write_note(rt)
        back = load(rt.id)
        assert back is not None, "round trip: load returned None"
        for key in ("id", "type", "title", "aliases", "created", "updated"):
            assert getattr(back, key) == getattr(rt, key), f"round trip: {key} {getattr(back, key)!r}"
        assert back.body.strip() == rt.body.strip(), f"round trip: body {back.body!r}"
        assert back.meta.get("mood") == "cheerful", f"round trip: unknown key lost {back.meta}"
        assert back.meta.get("source") == "selftest", f"round trip: source lost {back.meta}"
        assert back.path is not None and Path(back.path).resolve() == path.resolve()
        assert b"\r\n" not in path.read_bytes(), "round trip: file has CRLF line endings"
        assert any(n.id == rt.id for n in list_notes("fact")), "list_notes(fact) missed the note"

        # 2. Rejections leave the store unchanged.
        write_note(make("ep-selftest-panel", "episode", source="run", predicted_score="8"))
        before = _snapshot(root)
        missing = make("selftest-missing", "fact")
        missing.title = ""
        no_alias = make("selftest-noalias", "fact")
        no_alias.aliases = []
        bad_path = make("selftest-badpath", "fact")
        bad_path.path = tmp / "elsewhere" / "selftest-badpath.md"
        bad_date = make("selftest-baddate", "fact")
        bad_date.created = "10/15/2026"
        bad_body = make("selftest-bigbody", "fact")
        bad_body.body = "x" * (MAX_BODY + 1)
        rejects = {
            "missing field": missing,
            "empty aliases": no_alias,
            "duplicate id": make("selftest-roundtrip", "entity"),
            "bad id": make("Selftest_Bad", "fact"),
            "bad path": bad_path,
            "unknown type": make("selftest-type", "rumor"),
            "bad date": bad_date,
            "confidence": make("selftest-conf", "fact", confidence=1.5),
            "big body": bad_body,
            "episode source": make("ep-selftest-nosource", "episode"),
            "panel-only lesson": make("selftest-panel-lesson", "lesson", evidence=["ep-selftest-panel"]),
            "no-evidence reflection": make("selftest-reflection", "reflection"),
        }
        for label, bad in rejects.items():
            try:
                write_note(bad)
            except MemoryValidationError:
                pass
            else:
                raise AssertionError(f"rejections: {label} was accepted")
            assert _snapshot(root) == before, f"rejections: {label} changed the store"

        # 3. Accepted: a lesson citing a dataset episode, then an in-place update.
        write_note(make("post-selftest-1", "episode", source="dataset"))
        lesson = make("selftest-lesson", "lesson", evidence=["ep-selftest-panel", "post-selftest-1"], confidence=0.6)
        lesson.created = lesson.updated = "2026-01-01"
        write_note(lesson)
        reviewed = make("ep-selftest-reviewed", "episode", source="run", review_status="approved")
        write_note(reviewed)
        write_note(make("selftest-reflect", "reflection", evidence=["ep-selftest-reviewed"]))
        lesson.title = "Selftest lesson, revised"
        write_note(lesson)
        lessons = [n for n in list_notes("lesson") if n.id == "selftest-lesson"]
        assert len(lessons) == 1, f"accepted: rewrite duplicated the lesson ({len(lessons)})"
        assert lessons[0].title == "Selftest lesson, revised", "accepted: rewrite lost the new title"
        assert lessons[0].updated == today, f"accepted: updated not bumped ({lessons[0].updated})"
        assert lessons[0].created == "2026-01-01", "accepted: rewrite changed created"

        assert delete_note("selftest-reflect") and load("selftest-reflect") is None
        assert delete_note("selftest-reflect") is False
        assert not (root / ".lock").exists(), "lock file left behind"
        print("selftest_store: ok")
    finally:
        if old_env is None:
            os.environ.pop("MEMSTORE_PATH", None)
        else:
            os.environ["MEMSTORE_PATH"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)
