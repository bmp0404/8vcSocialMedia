"""Decode X (Twitter) Snowflake IDs into creation times.

Every post, reply, quote and user created after 2010-11-04 has a 64-bit
Snowflake ID laid out as:

    bit 63      sign, always 0
    bits 22-62  milliseconds since the Twitter epoch (1288834974657 Unix ms)
    bits 17-21  datacenter id
    bits 12-16  worker id
    bits 0-11   per-worker sequence number

So `(id >> 22) + 1288834974657` is the creation time in Unix ms, with no API
call. That gives every row of the dev-tool dataset an exact timestamp from its
`Post URL`, and gives every reply or quote ID its arrival time, which is how an
engagement curve can be rebuilt after the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

TWITTER_EPOCH_MS = 1288834974657
MAX_ID = (1 << 63) - 1
# IDs issued before Snowflake were small sequential integers (below ~3e10),
# which decode to the first few seconds after the epoch. Anything that decodes
# to the first day is rejected rather than turned into a wrong timestamp.
MIN_OFFSET_MS = 86_400_000

_STATUS_RE = re.compile(r"(?:twitter\.com|x\.com)/[^/]+/status(?:es)?/(\d+)")


@dataclass(frozen=True)
class SnowflakeParts:
    unix_ms: int
    datacenter: int
    worker: int
    sequence: int

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.unix_ms / 1000, tz=timezone.utc)


def _check(snowflake: int | str) -> int:
    try:
        value = int(snowflake)
    except (TypeError, ValueError) as e:
        raise ValueError(f"not a Snowflake ID: {snowflake!r}") from e
    if value < 0 or value > MAX_ID:
        raise ValueError(f"Snowflake ID out of 63-bit range: {value}")
    if (value >> 22) < MIN_OFFSET_MS:
        raise ValueError(
            f"{value} decodes to within a day of the 2010-11-04 Snowflake epoch; "
            "it is a pre-Snowflake sequential ID or not an X ID"
        )
    return value


def decode(snowflake: int | str) -> SnowflakeParts:
    value = _check(snowflake)
    return SnowflakeParts(
        unix_ms=(value >> 22) + TWITTER_EPOCH_MS,
        datacenter=(value >> 17) & 0x1F,
        worker=(value >> 12) & 0x1F,
        sequence=value & 0xFFF,
    )


def to_unix_ms(snowflake: int | str) -> int:
    return (_check(snowflake) >> 22) + TWITTER_EPOCH_MS


def to_datetime(snowflake: int | str) -> datetime:
    """Creation time as a timezone-aware UTC datetime (millisecond precision)."""
    return datetime.fromtimestamp(to_unix_ms(snowflake) / 1000, tz=timezone.utc)


def floor_id(when: datetime | int) -> int:
    """Smallest Snowflake that could have been issued at `when` (UTC datetime or Unix ms).

    Use it as a `since_id`/`until_id` bound to ask an API for posts in a time window.
    """
    unix_ms = when if isinstance(when, int) else round(when.timestamp() * 1000)
    offset = unix_ms - TWITTER_EPOCH_MS
    if offset < MIN_OFFSET_MS:
        raise ValueError(f"{unix_ms} is before Snowflake IDs existed")
    return offset << 22


def status_id_from_url(url: str) -> int:
    """Pull the status ID out of an x.com or twitter.com post URL."""
    m = _STATUS_RE.search(url or "")
    if not m:
        raise ValueError(f"no /status/<id> in URL: {url!r}")
    return int(m.group(1))
