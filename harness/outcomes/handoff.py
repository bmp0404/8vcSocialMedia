"""The repo inputs sync needs: the approved hand-off files, the watcher's hot drafts and the account's X handle."""

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from harness.sources.common import SourceError
from harness.sources.watch import ACCOUNT_HANDLE_ENV, account_handle

APPROVED_GLOB = "approved-run*.json"
# The watcher's hot targets (harness/watcher/hot.py). The operator answers them by hand from the
# account, so nothing approves them and the posted text may be the operator's own: they match on
# X's reply link alone.
HOT_GLOB = "*.json"
HOT_SOURCE = "hot/"
HOT_REQUIRED_KEYS = ("status_id", "offer", "arm", "profile", "finished_at")
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
    source: str  # the hand-off file name, or hot/<status_id>.json
    anchor_only: bool = False  # matches only a reply to target_status, whatever its text (hot targets)


@dataclass(frozen=True)
class HotTarget:
    """What the outcomes ledger keeps about one hot target beyond its action row."""

    action_id: str
    status_id: str
    arm: str
    profile: str
    method: str | None  # the writer that actually ran; None for the human arm


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


def hot_action_id(status_id: str) -> str:
    return f"hot-{status_id}"


def load_hot(directory: Path) -> list[tuple[ApprovedAction, HotTarget]]:
    """The hot targets in `directory` that offered the operator something: a draft or a prompt for a one-liner.

    A missing directory means the watcher hasn't run, so it gives nothing. A hot file that abstained
    offered nothing to post and is skipped. The clock starts when the file was written.
    """
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob(HOT_GLOB)):
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = [k for k in HOT_REQUIRED_KEYS if not data.get(k)]
        if missing:
            raise HandoffError(f"{path} has no {', '.join(missing)}; harness/watcher/hot.py run_hot writes them")
        if data["offer"] == "abstain":
            continue
        sid = str(data["status_id"])
        action = ApprovedAction(
            id=hot_action_id(sid), run_id=None, type="reply", text=data.get("draft") or "", original_text=None,
            target_status=sid, approved_at=_parse_time(data["finished_at"], str(path)), source=HOT_SOURCE + path.name,
            anchor_only=True,
        )
        out.append((action, HotTarget(action.id, sid, data["arm"], data["profile"], data.get("method"))))
    return out


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
