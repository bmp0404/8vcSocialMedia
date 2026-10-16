"""The two repo inputs sync needs: the approved hand-off files and the account's X handle."""

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from harness.sources.common import SourceError
from harness.sources.watch import ACCOUNT_HANDLE_ENV, account_handle

APPROVED_GLOB = "approved-run*.json"
REQUIRED_KEYS = ("id", "type", "text", "approved_at")
TEXTLESS_TYPES = {"follow"}  # the review page shows no text box for a follow
_STATUS_ID = re.compile(r"/status(?:es)?/(\d+)")


class HandoffError(ValueError):
    """An approved hand-off file is missing a field sync needs."""


class HandleError(ValueError):
    """No usable X handle for the brand account."""


@dataclass(frozen=True)
class ApprovedAction:
    id: str
    run_id: int | None
    type: str
    text: str
    original_text: str | None  # the mind's draft when a reviewer edited it
    target_status: str | None  # status id from target_url, for replies and quotes
    approved_at: datetime
    source: str  # the hand-off file name


def status_id(url: str | None) -> str | None:
    m = _STATUS_ID.search(url or "")
    return m.group(1) if m else None


def _parse_time(value: str, where: str) -> datetime:
    try:
        t = datetime.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise HandoffError(f"{where}: approved_at {value!r} isn't an ISO 8601 timestamp") from e
    if t.tzinfo is None:
        raise HandoffError(f"{where}: approved_at {value!r} has no timezone; memory.now() writes UTC with +00:00")
    return t.astimezone(timezone.utc)


def load_approved(directory: Path) -> list[ApprovedAction]:
    """Every action in `directory/approved-run*.json`.

    An id in several files keeps the text of its latest approval and the time of its first, because
    the bot may have posted it any time after the first. memory.set_review restamps approved_at on
    every re-save.
    """
    if not directory.is_dir():
        raise HandoffError(f"{directory} isn't a directory; approved-run<id>.json files go in out/")
    found: dict[str, ApprovedAction] = {}
    for path in sorted(directory.glob(APPROVED_GLOB)):
        data = json.loads(path.read_text(encoding="utf-8"))
        for i, a in enumerate(data.get("actions") or []):
            where = f"{path.name} action {a.get('id') or i}"
            required = [k for k in REQUIRED_KEYS if not (k == "text" and a.get("type") in TEXTLESS_TYPES)]
            missing = [k for k in required if not a.get(k)]
            if missing:
                raise HandoffError(f"{where} has no {', '.join(missing)}; harness/review export_approved writes them")
            action = ApprovedAction(
                id=str(a["id"]),
                run_id=data.get("run_id"),
                type=a["type"],
                text=a.get("text") or "",
                original_text=a.get("original_text"),
                target_status=status_id(a.get("target_url")),
                approved_at=_parse_time(a["approved_at"], where),
                source=path.name,
            )
            prev = found.get(action.id)
            if prev is not None:
                newer, older = sorted((prev, action), key=lambda x: x.approved_at, reverse=True)
                action = replace(newer, approved_at=older.approved_at)
            found[action.id] = action
    return sorted(found.values(), key=lambda a: (a.approved_at, a.id))


def resolve_handle(override: str | None = None) -> tuple[str, str]:
    """(handle, where it came from): --handle, else watch.account_handle(). Raises HandleError when unset."""
    if override:
        return override.strip().lstrip("@").strip(), "--handle"
    try:
        handle, source = account_handle()
    except SourceError as e:
        raise HandleError(str(e)) from e
    if not handle:
        raise HandleError(f"No X handle for the brand account: {source} is empty. Set it, set "
                          f"{ACCOUNT_HANDLE_ENV}, or pass --handle.")
    return handle, source
