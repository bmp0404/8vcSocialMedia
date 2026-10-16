"""The time-series record every attention collector returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from harness.context.http import PoliteClient

# One client per process so every collector shares the per-host rate limit and cache.
_CLIENT: PoliteClient | None = None


def client() -> PoliteClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = PoliteClient(min_interval_s=1.0)
    return _CLIENT


@dataclass
class Series:
    """Attention to one term from one source: (bucket start in UTC, value) pairs."""

    term: str
    source: str  # wikipedia | gdelt | hackernews | google_trends | ...
    resolution: str  # "15min", "1h", "1d"
    points: list[tuple[datetime, float]] = field(default_factory=list)
    fetched_at: str = ""
    note: str = ""  # caveats, such as "values are relative (0-100)"

    def value_at(self, when: datetime) -> float | None:
        """Value of the bucket containing `when`, or None if outside the series."""
        best = None
        for ts, v in self.points:
            if ts <= when:
                best = v
            else:
                break
        return best

    def window_sum(self, start: datetime, end: datetime) -> float:
        return sum(v for ts, v in self.points if start <= ts < end)

    def baseline(self, end: datetime, days: int = 14) -> float | None:
        """Median bucket value over the `days` before `end`."""
        from datetime import timedelta

        vals = sorted(v for ts, v in self.points if end - timedelta(days=days) <= ts < end)
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "source": self.source,
            "resolution": self.resolution,
            "points": [[ts.isoformat(), v] for ts, v in self.points],
            "fetched_at": self.fetched_at,
            "note": self.note,
        }


def utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)
