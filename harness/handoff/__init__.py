"""Hand-off: email ranked recommendations to the posting bot (8VC's Grok)."""

from .mailer import (
    build_message,
    dry_run,
    load_recommendations,
    render,
    send,
    smtp_configured,
)

__all__ = [
    "build_message",
    "dry_run",
    "load_recommendations",
    "render",
    "send",
    "smtp_configured",
]
