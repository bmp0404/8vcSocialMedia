"""Attention spikes from the context worker's collectors (Wikipedia pageviews, GDELT, Hacker News).

`harness.context.collectors.attention(term, start, end)` (branch sam/context-dynamics) returns one
time series per source. This source turns each into a single signal saying how far recent
attention sits above that series' own median, so orient can raise the velocity of a narrative
whose subject is spiking even when no post about it has counts yet.
"""

from datetime import datetime, timedelta

from harness.models import Signal
from harness.sources.base import Source
from harness.sources.common import SourceError, iso_utc, now_utc
from harness.sources.watch import load_watch

RECENT = timedelta(hours=24)


def spike(series, now: datetime, baseline_days: int) -> tuple[float, float, float] | None:
    """(ratio, recent level, baseline) for a context Series, or None without enough data.

    Recent level is the mean of the last 24 hours of buckets, or the newest bucket when the
    source lags (Wikipedia's daily views arrive a day late). Baseline is the series' median over
    the `baseline_days` before that.
    """
    if not series.points:
        return None
    recent = [(ts, v) for ts, v in series.points if ts >= now - RECENT] or [series.points[-1]]
    base = series.baseline(recent[0][0], days=baseline_days)
    if not base:
        return None
    level = sum(v for _, v in recent) / len(recent)
    return level / base, level, base


def attention_signal(series, now: datetime, baseline_days: int) -> Signal | None:
    got = spike(series, now, baseline_days)
    if got is None:
        return None
    ratio, level, base = got
    return Signal(
        source="attention",
        text=f"{series.source} attention to '{series.term}' is {ratio:.1f}x its {baseline_days}-day median",
        url=None,
        author=series.source,
        created_at=iso_utc(series.points[-1][0]),
        metrics={"spike_ratio": round(ratio, 2), "level": round(level, 1), "baseline": round(base, 1)},
        kind="attention",
    )


class AttentionSource(Source):
    name = "attention"

    def fetch(self, mission: str) -> list[Signal]:
        cfg = load_watch().attention
        if not cfg.terms:
            raise SourceError("attention: no terms in [attention] of the profile's observe.toml")
        try:
            from harness.context.collectors import attention
        except ImportError as e:
            raise SourceError(
                "attention needs harness.context.collectors.attention from sam/context-dynamics, which isn't merged here"
            ) from e
        now = now_utc()
        start = now - timedelta(days=cfg.baseline_days) - RECENT
        signals = []
        for term in cfg.terms:
            errors: dict[str, str] = {}
            for series in attention(term, start, now, errors=errors).values():
                s = attention_signal(series, now, cfg.baseline_days)
                if s:
                    signals.append(s)
            for source, reason in errors.items():
                print(f"[observe] attention: {source} for {term!r} failed: {reason}")
        if not signals:
            raise SourceError(f"attention: no collector returned usable data for {cfg.terms}")
        return signals
