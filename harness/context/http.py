"""Polite, cached HTTP GETs for the keyless context sources.

Every collector goes through `PoliteClient` so that free APIs see one
User-Agent, at most one request per `min_interval_s` per host, bounded retries
with backoff on 429 and 5xx, and a disk cache under `data/context/cache/`.
The cache makes reruns free and makes a backfill reproducible from what was
fetched, which matters because some sources (X metrics) change over time.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness import config

USER_AGENT = "8vc-media-harness/0.1 (context research; github.com/bmp0404/8vcSocialMedia)"
CACHE_DIR = config.ROOT / "data" / "context" / "cache"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """A source could not be fetched. The message says which host and why."""


@dataclass
class CachedResponse:
    url: str
    status: int
    fetched_at: str  # ISO-8601 UTC
    body: str
    from_cache: bool

    def json(self):
        return json.loads(self.body)


class PoliteClient:
    def __init__(
        self,
        min_interval_s: float = 1.0,
        cache_dir: Path | None = CACHE_DIR,
        timeout_s: float = 25.0,
        max_retries: int = 3,
        user_agent: str = USER_AGENT,
    ):
        self.min_interval_s = min_interval_s
        self.cache_dir = cache_dir
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.user_agent = user_agent
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        host = urllib.parse.urlsplit(url).netloc.replace(":", "_")
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / host / f"{digest}.json"

    def _wait_turn(self, host: str) -> None:
        with self._lock:
            last = self._last_request.get(host)
            now = time.monotonic()
            if last is not None and now - last < self.min_interval_s:
                time.sleep(self.min_interval_s - (now - last))
            self._last_request[host] = time.monotonic()

    def get(self, url: str, max_age_s: float | None = None, headers: dict | None = None) -> CachedResponse:
        """GET `url`, serving from cache when the cached copy is younger than `max_age_s`.

        `max_age_s=None` means any cached copy is good (use it for history that
        cannot change, like a past hour of pageviews). Raises FetchError on a
        non-2xx status after retries.
        """
        path = self._cache_path(url)
        if path is not None and path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - datetime.fromisoformat(cached["fetched_at"]).timestamp()
            if max_age_s is None or age <= max_age_s:
                return CachedResponse(cached["url"], cached["status"], cached["fetched_at"], cached["body"], True)

        host = urllib.parse.urlsplit(url).netloc
        req_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        req_headers.update(headers or {})
        delay = max(self.min_interval_s, 2.0)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self._wait_turn(host)
            try:
                req = urllib.request.Request(url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    status = resp.status
                break
            except urllib.error.HTTPError as e:
                status = e.code
                last_error = f"HTTP {e.code} from {host}"
                if e.code not in RETRY_STATUSES or attempt == self.max_retries:
                    raise FetchError(f"{last_error} for {url}") from e
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                time.sleep(min(wait, 120.0))
                delay *= 2
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = f"{type(e).__name__} reaching {host}: {e}"
                if attempt == self.max_retries:
                    raise FetchError(f"{last_error} for {url}") from e
                time.sleep(delay)
                delay *= 2
        else:  # pragma: no cover - loop always breaks or raises
            raise FetchError(last_error)

        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"url": url, "status": status, "fetched_at": fetched_at, "body": body}),
                encoding="utf-8",
            )
        return CachedResponse(url, status, fetched_at, body, False)

    def get_json(self, url: str, max_age_s: float | None = None, headers: dict | None = None):
        resp = self.get(url, max_age_s=max_age_s, headers=headers)
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise FetchError(f"{urllib.parse.urlsplit(url).netloc} returned non-JSON for {url}: {resp.body[:200]!r}") from e
