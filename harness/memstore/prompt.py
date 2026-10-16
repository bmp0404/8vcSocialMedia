"""Memory rendered as prompt text for the minds. Read-only."""

from harness.memstore.note import Note
from harness.memstore.search import iter_notes, search

CONTEXT_CHARS = 2000
CONTEXT_BULLETS = 5
LESSON_MIN_CONFIDENCE = 0.6
LESSON_MIN_EVIDENCE = 3


def _bullets(body: str, limit: int | None = None) -> list[str]:
    lines = [line.rstrip() for line in body.splitlines() if line.lstrip().startswith("- ")]
    return lines[:limit] if limit else lines


def _join_capped(blocks: list[str], max_chars: int) -> str:
    """Join whole blocks while they fit. A first block that alone is too long is cut."""
    out = ""
    for block in blocks:
        candidate = f"{out}\n\n{block}" if out else block
        if len(candidate) > max_chars:
            return out or block[:max_chars]
        out = candidate
    return out


def context_for(text: str, k: int = 5, types: list[str] | None = None) -> str:
    """The top matching notes for a narrative, as '### <title> (<type>, <id>)' plus their first bullets."""
    if not text.strip():
        return ""
    by_id = {n.id: n for n in iter_notes(types)}
    blocks = []
    for hit in search(text, k, types):
        note = by_id.get(hit["id"])
        bullets = _bullets(note.body, CONTEXT_BULLETS) if note else []
        blocks.append("\n".join([f"### {hit['title']} ({hit['type']}, {hit['id']})", *bullets]))
    return _join_capped(blocks, CONTEXT_CHARS)


def _confidence(note: Note) -> float:
    try:
        return float(note.meta.get("confidence", 0))
    except (TypeError, ValueError):
        return 0.0


def prompt_block(max_chars: int = 6000) -> str:
    """All procedures, then confident well-evidenced lessons, capped. Returns "" when empty; never raises."""
    try:
        procedures = sorted(iter_notes(["procedure"]), key=lambda n: n.id)
        lessons = [
            n
            for n in iter_notes(["lesson"])
            if _confidence(n) >= LESSON_MIN_CONFIDENCE and len(n.meta.get("evidence") or []) >= LESSON_MIN_EVIDENCE
        ]
        lessons.sort(key=_confidence, reverse=True)
        blocks = [f"### {n.title} ({n.type}, {n.id})\n{n.body.strip()}" for n in procedures + lessons]
        return _join_capped(blocks, max_chars) if blocks else ""
    except Exception:
        return ""
