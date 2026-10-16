"""HTTP and time helpers shared by the source plugins.

Every fetch failure becomes a `SourceError` whose message names the source, the host and the
cause, because `observe()` prints that message and skips the source.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

USER_AGENT = "media-harness/0.1 (+https://github.com/bmp0404/8vcSocialMedia)"


class SourceError(RuntimeError):
    """A source could not fetch. The message says which source and why."""


def get_text(
    url: str,
    *,
    source: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 15,
) -> str:
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    host = urllib.parse.urlsplit(url).netloc
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200].replace("\n", " ")
        raise SourceError(f"{source}: {host} returned HTTP {e.code} {e.reason}: {body}") from e
    except urllib.error.URLError as e:
        raise SourceError(f"{source}: could not reach {host}: {e.reason}") from e
    except TimeoutError as e:
        raise SourceError(f"{source}: {host} timed out after {timeout}s") from e


def get_json(url: str, *, source: str, **kwargs) -> Any:
    text = get_text(url, source=source, **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise SourceError(f"{source}: {url.split('?')[0]} did not return JSON: {text[:120]!r}") from e


def parse_time(value: str | int | float | None) -> datetime | None:
    """Parse the timestamp formats our sources emit into an aware UTC datetime.

    Handles ISO 8601 with or without seconds ("2026-10-17T19:30Z"), RFC 2822 RSS dates
    ("Thu, 15 Oct 2026 20:58:22 GMT") and Unix epoch seconds. Returns None for empty or
    unparseable input rather than guessing.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Format as ISO 8601 UTC with a trailing Z, the form `Signal.created_at` uses."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
