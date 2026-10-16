"""Audit: a person labels the frozen 200-item audit set, one item at a time, through the label store.

The store is `harness/label/store.py` on sam/corpus-clean, read only through its contract
(`~/.long-run/8vc-harness/briefs/BRIEF-B2.md`, "The label store contract"): `load_schema`, `queue("audit")`,
`items`, `consensus` and `append_human`. The page builds its controls from the schema.

The LLM votes stay on the server until the person has answered: `GET /api/audit` never includes them,
and `POST /api/audit/<item_id>` returns them with the saved answer, so nobody anchors on a model's label.
"""

import getpass
import os

from harness.review.routes import HTTPError, Request, route


def labeler() -> str:
    """Who `append_human` records as the labeler: HARNESS_LABELER, else the OS user."""
    return os.getenv("HARNESS_LABELER") or getpass.getuser()


def store():
    """The label store, or HTTPError 503 saying what's missing. Imported here because the store comes with
    another branch, and the queue and history work without it."""
    try:
        from harness.label.store import default_store
    except ImportError:
        raise HTTPError(503, "The label store (harness/label/store.py, from sam/corpus-clean) isn't on this "
                             "branch, so there is nothing to audit yet.") from None
    return default_store()


def _post(item: dict) -> dict:
    """The fields the page shows for an item: no counts, which would answer engagement_guess for the person."""
    return {k: item.get(k) for k in ("item_id", "kind", "status_id", "url", "author", "author_followers", "text",
                                     "created_at", "parent_id")}


def next_item(s, offset: int = 0) -> dict:
    try:
        total = len(s.audit_ids())
    except FileNotFoundError as e:
        raise HTTPError(503, str(e)) from None
    left = s.queue("audit", limit=total)
    if offset >= len(left):
        return {"schema": s.load_schema(), "item": None, "parent": None, "remaining": len(left), "total": total}
    item = left[offset]["item"]
    parent = None
    if item.get("parent_id"):
        try:
            parent = _post(s.items([item["parent_id"]])[0])
        except KeyError:
            parent = None  # the parent isn't in the corpus; the item still stands on its own
    return {"schema": s.load_schema(), "item": _post(item), "parent": parent, "remaining": len(left), "total": total}


@route("GET", r"/api/audit")
def get_audit(req: Request):
    """The next unlabeled audit item after skipping `offset` of them, the schema, and how many are left."""
    try:
        offset = int(req.query.get("offset", 0))
    except ValueError:
        raise HTTPError(400, "offset must be a whole number") from None
    if offset < 0:
        raise HTTPError(400, "offset must be 0 or more")
    return next_item(store(), offset)


@route("POST", r"/api/audit/([^/]+)")
def post_answer(req: Request):
    """Save `{labels, note?}` for one item with append_human, then reveal how the models labeled it."""
    item_id, body = req.match[1], req.body
    labels, note = body.get("labels"), body.get("note") or ""
    if not isinstance(labels, dict) or not labels:
        raise HTTPError(400, "labels must be an object with at least one answered field")
    s = store()
    try:
        row = s.append_human(item_id, labeler(), labels, note)
        consensus = s.consensus(item_id)
    except KeyError:
        raise HTTPError(404, f"unknown item {item_id}") from None
    except ValueError as e:
        raise HTTPError(400, str(e)) from None
    return {"ok": True, "saved": row, "consensus": consensus}
