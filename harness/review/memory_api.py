"""The memory view's API: what the harness believes now and the evidence behind each belief.

Lessons, reflections and edit procedures come from the memory package's read views
(harness.learning.views, from sam/memory), whose output keys are the contract with this page.
Without that package the view shows memory/lessons.md and says why. Outcomes are
predicted-versus-actual pairs from the run store.
"""

from pathlib import Path

from harness import config, memory
from harness.review.routes import HTTPError, Request, route

MAX_NOTES = 50  # static/js/views/memory.js asks in chunks of this size (NOTES_PER_REQUEST)


def _memory():
    try:
        from harness.learning import views
        from harness.learning.store import get_store
    except ImportError as e:
        return None, None, f"Structured memory (harness.learning, from sam/memory) isn't on this branch: {e}"
    try:
        return get_store(), views, None
    except Exception as e:  # noqa: BLE001 - a store that won't open is the operator's to fix; say why
        raise HTTPError(500, f"Couldn't open the memory store: {type(e).__name__}: {e}") from e


def _where(store) -> str:
    """The store's file relative to the repo (out/memory-fake.json), or its class for a non-file store."""
    path = getattr(store, "path", None)
    if not path:
        return type(store).__name__
    try:
        return str(Path(path).resolve().relative_to(config.ROOT.resolve()))
    except ValueError:
        return str(path)


@route("GET", r"/api/memory")
def get_memory(req: Request):
    out = {"lessons_md": config.lessons(), "outcomes": memory.scored_history(limit=50)}
    store, views, reason = _memory()
    if store is None:
        return {**out, "available": False, "reason": reason}
    where = _where(store)
    try:
        rows = {
            "lessons": views.lesson_rows(store),
            "reflections": views.reflection_rows(store),
            "procedures": views.procedure_rows(store),
            "counts": {
                k: len(store.list_memories(k)) for k in ("episode", "entity", "fact", "lesson", "reflection", "procedure")
            },
        }
    except Exception as e:  # noqa: BLE001 - name the store so the operator knows which file broke
        raise HTTPError(500, f"Couldn't read the memory store {where}: {type(e).__name__}: {e}") from e
    return {**out, "available": True, "store": where, **rows}


@route("GET", r"/api/memory/notes")
def get_notes(req: Request):
    """Specific notes by id (?ids=a,b,c), for expanding a belief's evidence."""
    ids = [i for i in req.query.get("ids", "").split(",") if i]
    if not ids:
        raise HTTPError(400, "pass ?ids=<id>,<id>")
    if len(ids) > MAX_NOTES:
        raise HTTPError(400, f"ask for at most {MAX_NOTES} notes at a time, not {len(ids)}")
    store, _, reason = _memory()
    if store is None:
        raise HTTPError(501, reason)
    wanted = set(ids)
    found = {m.id: {**m.meta, "id": m.id, "kind": m.kind, "body": m.body} for m in store.list_memories() if m.id in wanted}
    return {"notes": [found[i] for i in ids if i in found], "missing": [i for i in ids if i not in found]}
