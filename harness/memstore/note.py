"""Note format for the memory store: types, the Note dataclass, parse() and dump().

The format is pinned in memory/IMPLEMENTATION.md. Validation of a note's
content (required keys, ids, dates, evidence) belongs to write_note in store.py;
parse() only rejects text it can't read as a note.
"""

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from harness import config

TYPES = {
    "entity": "entities",       # accounts, competitors, narratives
    "fact": "facts",            # brand facts; meta.quote must appear verbatim in meta.source
    "lesson": "lessons",        # what worked or flopped; tags mark pattern / calibration
    "procedure": "procedures",  # rules: voice, safety, never-do-this
    "episode": "episodes",      # one action or dataset post
    "reflection": "reflections" # why a group of episodes shared an outcome
}

REQUIRED = ("id", "type", "title", "aliases", "created", "updated")
LIST_KEYS = {"aliases", "evidence", "tags"}
FLOAT_KEYS = {"confidence"}
NEEDS_QUOTES = (",", "[", "]", ":")


class MemoryValidationError(ValueError):
    """A note that can't be parsed or doesn't satisfy the store's rules."""


@dataclass
class Note:
    id: str
    type: str
    title: str
    aliases: list[str]
    created: str            # YYYY-MM-DD
    updated: str            # YYYY-MM-DD
    body: str
    meta: dict = field(default_factory=dict)   # every optional or unknown key
    path: Path | None = None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_list(inner: str) -> list[str]:
    """Split an inline list on commas, honouring quoted items."""
    items, buf, quote = [], "", ""
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'" and not buf.strip():
            buf += ch
            quote = ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        else:
            buf += ch
    items.append(buf)
    return [_unquote(i.strip()) for i in items if i.strip()]


def _parse_value(key: str, raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return _split_list(raw[1:-1])
    value = _unquote(raw)
    if key in LIST_KEYS:
        return [value] if value else []
    if key in FLOAT_KEYS and value:
        try:
            return float(value)
        except ValueError:
            raise MemoryValidationError(f"{key} must be a number, got {value!r}") from None
    return value


def parse(text: str, path: Path | None = None) -> Note:
    where = f" in {path}" if path else ""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise MemoryValidationError(f"note must start with a '---' line{where}")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise MemoryValidationError(f"frontmatter has no closing '---' line{where}") from None

    fields: dict = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, raw = line.partition(":")
        key = key.strip()
        if not sep or not key:
            raise MemoryValidationError(f"frontmatter line is not 'key: value': {line!r}{where}")
        if key in REQUIRED and key != "aliases":
            fields[key] = _unquote(raw.strip())
        else:
            fields[key] = _parse_value(key, raw)

    return Note(
        id=fields.pop("id", ""),
        type=fields.pop("type", ""),
        title=fields.pop("title", ""),
        aliases=fields.pop("aliases", []),
        created=fields.pop("created", ""),
        updated=fields.pop("updated", ""),
        body="\n".join(lines[end + 1:]),
        meta=fields,
        path=path,
    )


def _quote(value: str) -> str:
    plain = (
        value
        and value == value.strip()
        and not any(c in value for c in NEEDS_QUOTES)
        and value[0] not in "\"'"
    )
    if plain:
        return value
    return f"'{value}'" if '"' in value else f'"{value}"'


def _dump_value(value) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_quote(str(v)) for v in value) + "]"
    if isinstance(value, float):
        return repr(value)
    return _quote(str(value))


def dump(note: Note) -> str:
    out = ["---"]
    for key in REQUIRED:
        out.append(f"{key}: {_dump_value(getattr(note, key))}")
    for key in sorted(note.meta):
        out.append(f"{key}: {_dump_value(note.meta[key])}")
    out.append("---")
    return "\n".join(out) + "\n" + note.body


_mock_root: Path | None = None


def store_root() -> Path:
    """Where the active mission profile's store lives: MEMSTORE_PATH when set, a temp store under mock,
    else memory/store/<profile>/. Seeding and locking are store.py's job."""
    global _mock_root
    if os.getenv("MEMSTORE_PATH"):
        return Path(os.environ["MEMSTORE_PATH"])
    if config.PROVIDER == "mock":
        if _mock_root is None:
            _mock_root = Path(tempfile.mkdtemp(prefix="memstore-"))
        return _mock_root / config.profile_name()
    return config.memory_store_dir()


def selftest_note() -> None:
    text = (
        "---\n"
        "id: launch-posts-with-demo-video\n"
        "type: lesson\n"
        'title: "Launch posts: demo video beats text"\n'
        "aliases: [launch, ship, \"GA, general availability\"]\n"
        "created: 2026-10-15\n"
        "updated: 2026-10-15\n"
        "confidence: 0.6\n"
        "evidence: [post-1845532211, post-1846010042]\n"
        "owner_branch: sam/memory\n"
        "tags: [pattern]\n"
        "---\n"
        "- **Finding:** Café launches — naïve 日本語 text kept; stated on 2026-10-15.\n"
        "- Related: [[ai-coding-agents]]\n"
    )
    note = parse(text)
    assert note.id == "launch-posts-with-demo-video"
    assert note.title == "Launch posts: demo video beats text"
    assert note.aliases == ["launch", "ship", "GA, general availability"]
    assert note.meta["confidence"] == 0.6
    assert note.meta["evidence"] == ["post-1845532211", "post-1846010042"]
    assert note.meta["owner_branch"] == "sam/memory", "unknown key kept"
    assert "日本語" in note.body and "Café" in note.body, "non-ASCII kept"

    again = parse(dump(note))
    assert again == note, "round trip"
    assert dump(again) == dump(note) == text, "dump is stable and canonical"

    assert parse(text.replace("\n", "\r\n")) == note, "CRLF input reads the same"

    tmp = Path(tempfile.mkdtemp(prefix="memstore-selftest-")) / "note.md"
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(dump(note))
    raw = tmp.read_bytes()
    assert b"\r\n" not in raw, "\\n line endings on disk"
    with tmp.open(encoding="utf-8", newline="\n") as f:
        assert parse(f.read(), tmp) == Note(**{**note.__dict__, "path": tmp})

    for bad in ("no frontmatter", "---\nid: x\n", "---\njust words\n---\n"):
        try:
            parse(bad)
        except MemoryValidationError:
            pass
        else:
            raise AssertionError(f"parse accepted {bad!r}")

    assert set(TYPES) == {"entity", "fact", "lesson", "procedure", "episode", "reflection"}
    os.environ["MEMSTORE_PATH"] = str(tmp.parent)
    assert store_root() == tmp.parent
    print("selftest_note: ok")
