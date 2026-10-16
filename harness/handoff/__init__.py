"""Hand-off: email ranked recommendations to the posting bot (8VC's Grok)."""

from .deliver import deliver
from .mailer import (
    build_message,
    dry_run,
    handle,
    load_recommendations,
    render,
    send,
    smtp_configured,
)

__all__ = [
    "build_message",
    "deliver",
    "dry_run",
    "handle",
    "load_recommendations",
    "render",
    "send",
    "smtp_configured",
]
