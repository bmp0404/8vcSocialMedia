"""Render recommendations into an email for the posting bot and send it via SMTP.

Stdlib only. The plain-text body is the source of truth; the HTML part is a
simple rendering of the same content, and the raw JSON rides along as an
attachment.

Env:
    SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = implicit SSL), SMTP_USER,
    SMTP_PASSWORD, HANDOFF_EMAIL_FROM (default SMTP_USER),
    HANDOFF_EMAIL_TO (comma-separated), HANDOFF_EMAIL_CC (optional).
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email import policy as _policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Iterable

ACTION_HELP = [
    ("post", "Publish TEXT as a new original tweet. No target."),
    ("reply", "Reply to the tweet at TARGET_URL with TEXT."),
    ("quote", "Quote-tweet the tweet at TARGET_URL with TEXT as the comment."),
    ("mention", "Publish TEXT as a new tweet; it addresses TARGET_HANDLE."),
    ("follow", "Follow the account TARGET_HANDLE. Nothing is posted; TEXT is empty."),
]

# Reply-lane "do nothing" outcomes; logged to memory, never handed off.
DECLINE_TYPES = {"wait", "no_response", "skip"}

TEXT_OPEN = "<<<BEGIN_TEXT"
TEXT_CLOSE = "END_TEXT>>>"

# RFC 5322 line limit; avoids the stdlib's odd early folding of subjects containing "(...)".
POLICY = _policy.default.clone(max_line_length=998)

Attachment = tuple[str, bytes, str]  # (filename, data, mime type "main/sub")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_recommendations(path: str | Path) -> tuple[list[dict], str | None]:
    """Read a recommendations file. Accepts a bare list of Action dicts or
    {"run_id": ..., "recommendations": [...]} (what cli.py currently writes)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        actions = data.get("recommendations") or data.get("actions") or []
        run_id = data.get("run_id")
    else:
        actions, run_id = data, None
    keep = [a for a in actions if not a.get("vetoed") and (a.get("type") or "").lower() not in DECLINE_TYPES]
    return keep, (str(run_id) if run_id is not None else None)


def _get(a: Any, key: str, default: Any = None) -> Any:
    if isinstance(a, dict):
        return a.get(key, default)
    return getattr(a, key, default)


def handle(h: str | None) -> str | None:
    """Stored handles have no leading @; render them with exactly one."""
    h = (h or "").strip().lstrip("@")
    return f"@{h}" if h else None


def _sentiment(a: Any) -> str:
    s = _get(a, "sentiment") or {}
    if not isinstance(s, dict) or not s:
        return "none"
    return " / ".join(f"{k} {float(s.get(k) or 0):.0%}" for k in ("positive", "neutral", "negative"))


def _one_line(s: str | None, limit: int = 240) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(actions: Iterable[Any], run_id: str, mission_name: str) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body). `actions` may be Action objects or dicts,
    already ranked best-first."""
    actions = list(actions)
    n = len(actions)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subject = f"[HARNESS] {n} X action{'s' if n != 1 else ''} for {mission_name} | run {run_id}"

    reply_line = (
        "REPLY INSTRUCTIONS: Reply to this email with one line per action you executed, "
        "formatted exactly as `ACTION_ID <id> | <resulting tweet URL>` "
        "(for follows use `ACTION_ID <id> | followed`; for skipped actions use "
        "`ACTION_ID <id> | skipped | <short reason>`). We use your reply to learn what works."
    )

    lines: list[str] = [
        "HARNESS HAND-OFF: RECOMMENDED X ACTIONS",
        f"RUN_ID: {run_id}",
        f"MISSION: {mission_name}",
        f"GENERATED_AT: {generated}",
        f"ACTION_COUNT: {n}",
        "",
        "WHAT THIS IS: The media harness drafted and scored candidate X (Twitter) actions for the",
        "brand account. They are ranked best-first (#1 = highest predicted score, 0.0-1.0).",
        "Vetoed drafts were already removed. Pick which to execute; you decide the final call.",
        "",
        "ALLOWED ACTION TYPES:",
    ]
    width = max(len(t) for t, _ in ACTION_HELP)
    lines += [f"  {t.ljust(width)}  {h}" for t, h in ACTION_HELP]
    lines += [
        "",
        "RULES:",
        f"  - Post TEXT exactly as written between the {TEXT_OPEN} and {TEXT_CLOSE} lines.",
        "    The marker lines themselves are NOT part of the tweet.",
        "  - TARGET_URL / TARGET_HANDLE say which tweet or account the action applies to;",
        "    \"none\" means the action has no target.",
        "  - The raw JSON for every action is attached (recommendations-<run>.json).",
        "",
        reply_line,
        "",
    ]

    for i, a in enumerate(actions, 1):
        aid = _get(a, "id", "")
        atype = (_get(a, "type") or "").lower()
        text = _get(a, "text") or ""
        score = _get(a, "predicted_score") or 0.0
        lines += [
            f"===== ACTION {i} of {n} =====",
            f"ACTION_ID: {aid}",
            f"TYPE: {atype}",
            f"TARGET_URL: {_get(a, 'target_url') or 'none'}",
            f"TARGET_HANDLE: {handle(_get(a, 'target_handle')) or 'none'}",
            f"PREDICTED_SCORE: {float(score):.3f}",
            f"PREDICTED_SENTIMENT: {_sentiment(a)}",
            f"RATIONALE: {_one_line(_get(a, 'rationale')) or 'none'}",
        ]
        lines += ["TEXT:", TEXT_OPEN, text, TEXT_CLOSE] if text else ["TEXT: none (nothing to post)"]
        lines += [
            f"===== END ACTION {i} =====",
            "",
        ]

    lines += [reply_line, ""]
    text_body = "\n".join(lines)
    return subject, text_body, _render_html(actions, run_id, mission_name, generated, reply_line)


def _render_html(actions: list, run_id: str, mission_name: str, generated: str, reply_line: str) -> str:
    e = _html.escape
    n = len(actions)
    mono = "font-family:Menlo,Consolas,monospace;"
    rows = "".join(f"<li><code>{e(t)}</code> &mdash; {e(h)}</li>" for t, h in ACTION_HELP)
    parts = [
        "<!doctype html><html><body style=\"font-family:-apple-system,Segoe UI,Arial,sans-serif;"
        "max-width:680px;color:#111;line-height:1.45\">",
        "<h2 style=\"margin-bottom:4px\">Harness hand-off: recommended X actions</h2>",
        f"<p style=\"{mono}font-size:13px;color:#444\">RUN_ID: {e(run_id)}<br>MISSION: {e(mission_name)}"
        f"<br>GENERATED_AT: {e(generated)}<br>ACTION_COUNT: {n}</p>",
        "<p>The media harness drafted and scored candidate X actions for the brand account. "
        "Ranked best-first (#1 = highest predicted score, 0.0&ndash;1.0); vetoed drafts removed. "
        "The plain-text part of this email is the source of truth; raw JSON is attached.</p>",
        f"<p><b>Allowed action types:</b></p><ul>{rows}</ul>",
        f"<p style=\"background:#fff7d6;padding:8px;border-left:3px solid #e0b000\">{e(reply_line)}</p>",
    ]
    for i, a in enumerate(actions, 1):
        tu, th = _get(a, "target_url"), handle(_get(a, "target_handle"))
        target = []
        if tu:
            target.append(f"<a href=\"{e(tu)}\">{e(tu)}</a>")
        if th:
            target.append(e(th))
        text = _get(a, "text") or ""
        parts.append(
            "<div style=\"border:1px solid #ddd;border-radius:6px;padding:12px;margin:14px 0\">"
            f"<h3 style=\"margin:0 0 6px\">#{i} &middot; {e((_get(a, 'type') or '').upper())}</h3>"
            f"<table style=\"{mono}font-size:13px;border-collapse:collapse\">"
            f"<tr><td style=\"padding-right:10px\">ACTION_ID</td><td><b>{e(str(_get(a, 'id', '')))}</b></td></tr>"
            f"<tr><td>TARGET</td><td>{' &middot; '.join(target) or 'none'}</td></tr>"
            f"<tr><td>SCORE</td><td>{float(_get(a, 'predicted_score') or 0):.3f}</td></tr>"
            f"<tr><td>SENTIMENT</td><td>{e(_sentiment(a))}</td></tr>"
            "</table>"
            f"<p style=\"margin:8px 0 4px\"><b>Rationale:</b> {e(_one_line(_get(a, 'rationale')) or 'none')}</p>"
            + (
                f"<pre style=\"{mono}white-space:pre-wrap;background:#f4f4f4;padding:10px;"
                f"border-radius:4px;margin:6px 0 0\">{e(text)}</pre>"
                if text
                else "<p style=\"color:#666;margin:6px 0 0\"><i>No text (nothing to post).</i></p>"
            )
            + "</div>"
        )
    parts.append("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Message + transport
# --------------------------------------------------------------------------- #
def _split(v: str | Iterable[str] | None) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = v.split(",")
    return [x.strip() for x in v if x and x.strip()]


def build_message(
    subject: str,
    text: str,
    html: str | None,
    attachments: Iterable[Attachment] = (),
    to: str | Iterable[str] | None = None,
    cc: str | Iterable[str] | None = None,
    sender: str | None = None,
) -> EmailMessage:
    to_l = _split(to if to is not None else os.getenv("HANDOFF_EMAIL_TO"))
    cc_l = _split(cc if cc is not None else os.getenv("HANDOFF_EMAIL_CC"))
    sender = sender or os.getenv("HANDOFF_EMAIL_FROM") or os.getenv("SMTP_USER") or "harness@localhost"

    msg = EmailMessage(policy=POLICY)
    msg["Subject"] = subject
    msg["From"] = sender
    if to_l:
        msg["To"] = ", ".join(to_l)
    if cc_l:
        msg["Cc"] = ", ".join(cc_l)
    msg["Date"] = formatdate(localtime=True)
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
    return msg


def smtp_configured() -> bool:
    return all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "HANDOFF_EMAIL_TO"))


def send(
    subject: str,
    text: str,
    html: str | None,
    attachments: Iterable[Attachment] = (),
    to: str | Iterable[str] | None = None,
    cc: str | Iterable[str] | None = None,
    sender: str | None = None,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    timeout: float = 30.0,
) -> EmailMessage:
    """Send via SMTP. Port 465 = implicit SSL, anything else = STARTTLS (default 587).
    Works with Gmail (smtp.gmail.com + an app password). Returns the sent message."""
    msg = build_message(subject, text, html, attachments, to, cc, sender)
    host = host or os.getenv("SMTP_HOST")
    port = int(port or os.getenv("SMTP_PORT") or 587)
    user = user or os.getenv("SMTP_USER")
    # Gmail displays app passwords with spaces; strip them.
    password = (password or os.getenv("SMTP_PASSWORD") or "").replace(" ", "")
    if not host:
        raise RuntimeError("SMTP_HOST is not set")
    rcpts = _split(msg.get("To")) + _split(msg.get("Cc"))
    if not rcpts:
        raise RuntimeError("No recipients: set HANDOFF_EMAIL_TO")

    ctx = ssl.create_default_context()
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=ctx, timeout=timeout)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    with server:
        server.ehlo()
        if port != 465:
            server.starttls(context=ctx)
            server.ehlo()
        if user and password:
            server.login(user, password)
        server.send_message(msg, to_addrs=rcpts)
    return msg


def dry_run(
    subject: str,
    text: str,
    html: str | None,
    attachments: Iterable[Attachment],
    out_dir: str | Path,
    stem: str,
    to: str | Iterable[str] | None = None,
    cc: str | Iterable[str] | None = None,
    sender: str | None = None,
) -> tuple[Path, Path]:
    """Write <stem>.eml and <stem>.txt (plain-text preview) to out_dir instead of sending."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    msg = build_message(subject, text, html, attachments, to, cc, sender)
    eml = out_dir / f"{stem}.eml"
    txt = out_dir / f"{stem}.txt"
    eml.write_bytes(msg.as_bytes())
    header = f"Subject: {msg['Subject']}\nFrom: {msg['From']}\nTo: {msg.get('To', '(unset)')}\n"
    if msg.get("Cc"):
        header += f"Cc: {msg['Cc']}\n"
    txt.write_text(header + "\n" + text, encoding="utf-8")
    return eml, txt
