"""One call from ranked recommendations to a hand-off email: sent, or written to disk as a dry run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .mailer import DECLINE_TYPES, _get, dry_run, render, send, smtp_configured


def _as_dict(a: Any) -> dict:
    return a if isinstance(a, dict) else a.to_dict()


def deliver(
    actions: Iterable[Any],
    run_id: str | int,
    mission_name: str,
    out_dir: str | Path,
    send_mail: bool = False,
    top: int = 10,
) -> dict:
    """Email the top `top` non-vetoed, non-decline actions (Action objects or dicts, best first).

    Sends only when `send_mail` is true AND SMTP is configured; otherwise writes
    out_dir/handoff-<run>.eml + .txt. Returns {"mode": "sent"|"dry_run"|"empty", "reason", "count", paths...}.
    """
    keep = [_as_dict(a) for a in actions
            if not _get(a, "vetoed") and (_get(a, "type") or "").lower() not in DECLINE_TYPES][: max(top, 0)]
    if not keep:
        print("[handoff] nothing to hand off (no non-vetoed recommendations)")
        return {"mode": "empty", "reason": "no recommendations", "count": 0}
    run_id = str(run_id)
    subject, text, html = render(keep, run_id, mission_name)
    attachments = [(f"recommendations-{run_id}.json", json.dumps(keep, indent=2).encode("utf-8"), "application/json")]

    if send_mail and smtp_configured():
        msg = send(subject, text, html, attachments)
        rcpts = ", ".join(x for x in (msg.get("To"), msg.get("Cc")) if x)
        print(f"[handoff] sent {len(keep)} actions to {rcpts}: {subject}")
        return {"mode": "sent", "reason": "", "count": len(keep), "to": rcpts, "subject": subject}

    reason = "SMTP not configured" if not smtp_configured() else "dry run (pass --send to email it)"
    if send_mail and not smtp_configured():
        reason = "--send given but SMTP not configured (need SMTP_HOST, SMTP_USER, SMTP_PASSWORD, HANDOFF_EMAIL_TO)"
    eml, txt = dry_run(subject, text, html, attachments, out_dir, f"handoff-{run_id}")
    print(f"[handoff] dry run ({reason}); {len(keep)} actions")
    print(f"[handoff] eml:     {eml}")
    print(f"[handoff] preview: {txt}")
    return {"mode": "dry_run", "reason": reason, "count": len(keep), "eml": str(eml), "txt": str(txt)}
