"""Cache rules of PoliteClient, without network: a refused local port stands in for a fetch."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from harness.context import http
from harness.context.http import FetchError, PoliteClient

URL = "http://127.0.0.1:9/data"  # discard port: any real fetch is refused at once


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "THROTTLE_DIR", tmp_path / "throttle")
    return PoliteClient(min_interval_s=0, cache_dir=tmp_path / "cache", max_retries=0, timeout_s=2)


def seed(client, fetched_at: datetime, body="cached"):
    path = client._cache_path(URL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": URL, "status": 200, "fetched_at": fetched_at.isoformat(), "body": body}), encoding="utf-8")
    return path


def now():
    return datetime.now(timezone.utc)


def test_fresh_copy_is_served(client):
    seed(client, now() - timedelta(seconds=30))
    assert client.get(URL, max_age_s=60).body == "cached"


def test_old_copy_refetches(client):
    seed(client, now() - timedelta(hours=2))
    with pytest.raises(FetchError):
        client.get(URL, max_age_s=60)


def test_copy_fetched_after_settling_is_permanent(client):
    seed(client, now() - timedelta(days=30))
    assert client.get(URL, max_age_s=60, settled_after=now() - timedelta(days=31)).body == "cached"


def test_copy_fetched_before_settling_is_not_made_permanent(client):
    # Cached while the window was open; the window has since closed. Must refetch.
    seed(client, now() - timedelta(days=30))
    with pytest.raises(FetchError):
        client.get(URL, max_age_s=60, settled_after=now() - timedelta(days=29))


def test_no_rules_means_any_copy(client):
    seed(client, now() - timedelta(days=365))
    assert client.get(URL).body == "cached"


def test_truncated_cache_file_is_a_miss_not_a_crash(client):
    path = seed(client, now())
    path.write_text('{"url": "http://127.0.0.1:9/da', encoding="utf-8")
    with pytest.raises(FetchError):
        client.get(URL)
