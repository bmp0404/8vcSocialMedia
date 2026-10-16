"""The two repo inputs sync needs: the approved hand-off files and the account's X handle."""

import json
import os
import re
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from harness import config

APPROVED_GLOB = "approved-run*.json"
OBSERVE_PATH = config.ROOT / "mission" / "observe.toml"
ACCOUNT_PATH = config.ROOT / "action" / "account.json"
# sam/action ships action/account.json with this name so the reply lane has something to write as.
# It isn't the brand's account, so reading its timeline would label someone else's posts.
PLACEHOLDER_HANDLES = {"clarkfanaccount"}
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


def _observe_handle(path: Path) -> str:
    if not path.exists():
        return ""
    return str((tomllib.loads(path.read_text(encoding="utf-8")).get("brand") or {}).get("x_handle") or "")


def _account_handle(path: Path) -> str:
    if not path.exists():
        return ""
    return str(json.loads(path.read_text(encoding="utf-8")).get("handle") or "")


def _clean(handle: str) -> str:
    return handle.strip().lstrip("@").strip()


def resolve_handle(override: str | None = None) -> tuple[str, str]:
    """(handle, where it came from). Raises HandleError naming both config keys when neither is usable."""
    if override:
        handle = _clean(override)
        if handle.lower() in PLACEHOLDER_HANDLES:
            raise HandleError(f"--handle {handle} is the placeholder from action/account.json, not the brand account")
        return handle, "--handle"
    observe_path = Path(os.getenv("HARNESS_OBSERVE_CONFIG") or OBSERVE_PATH)
    account_path = Path(os.getenv("HARNESS_ACCOUNT") or ACCOUNT_PATH)
    observed, account = _clean(_observe_handle(observe_path)), _clean(_account_handle(account_path))
    real = {name: h for name, h in ((observe_path, observed), (account_path, account))
            if h and h.lower() not in PLACEHOLDER_HANDLES}
    if len({h.lower() for h in real.values()}) > 1:
        raise HandleError(f"{observe_path} [brand] x_handle is {observed!r} but {account_path} handle is "
                          f"{account!r}; both must name the brand account")
    if not real:
        raise HandleError(
            "No real X handle for the brand account. Set [brand] x_handle in "
            f"{observe_path} (found {observed!r}) or \"handle\" in {account_path} (found {account!r}). "
            f"{sorted(PLACEHOLDER_HANDLES)} are placeholders. HARNESS_OBSERVE_CONFIG and HARNESS_ACCOUNT "
            "point at other files; --handle overrides both."
        )
    path, handle = next(iter(real.items()))
    return handle, str(path)
