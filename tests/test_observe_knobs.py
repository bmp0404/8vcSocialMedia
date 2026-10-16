"""The observe knobs object: strict overrides, env loading, and that knobs reach the code."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from harness.models import Signal
from harness.orient import signal_heat
from harness.sources import knobs as kn
from harness.sources.common import SourceError, iso_utc
from harness.sources.reply_targets import size_band

NOW = datetime(2026, 10, 15, 21, tzinfo=timezone.utc)


def test_from_dict_overrides_and_round_trips():
    k = kn.ObserveKnobs.from_dict({"news_half_life_h": 6, "max_per_author": 1})
    assert k.news_half_life_h == 6 and k.max_per_author == 1
    assert kn.ObserveKnobs.from_dict(k.to_dict()) == k


@pytest.mark.parametrize(
    "overrides, message",
    [({"news_halflife": 6}, "unknown keys"), ({"news_half_life_h": "6"}, "must be a number"), ({"giant_band": True}, "must be a number")],
)
def test_from_dict_rejects_typos_and_non_numbers(overrides, message):
    with pytest.raises(SourceError, match=message):
        kn.ObserveKnobs.from_dict(overrides)


def test_current_reads_env_file(tmp_path, monkeypatch):
    path = tmp_path / "knobs.json"
    path.write_text(json.dumps({"x_rate_full": 100}), encoding="utf-8")
    monkeypatch.setenv(kn.ENV, str(path))
    kn.current.cache_clear()
    try:
        assert kn.current().x_rate_full == 100
    finally:
        kn.current.cache_clear()


def test_current_names_unreadable_file(tmp_path, monkeypatch):
    monkeypatch.setenv(kn.ENV, str(tmp_path / "missing.json"))
    kn.current.cache_clear()
    try:
        with pytest.raises(SourceError, match="can't read HARNESS_OBSERVE_KNOBS"):
            kn.current()
    finally:
        kn.current.cache_clear()


def test_knobs_change_heat_and_bands():
    news = Signal(source="t", text="n", created_at=iso_utc(NOW - timedelta(hours=6)), kind="news")
    assert signal_heat(news, NOW, kn.ObserveKnobs(news_half_life_h=6)) == pytest.approx(0.3)
    assert size_band(20_000, 30, 5, kn.ObserveKnobs(mid_account=30_000)) == 0.5
