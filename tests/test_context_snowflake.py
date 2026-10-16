from datetime import datetime, timezone

import pytest

from harness.context import snowflake

# Ground truth from X itself: the post's created_at as served by the platform.
# supabase "Love that guy": Thu Sep 17 13:00:52 +0000 2026
# deno_land "Deno is joining Cloudflare": created_timestamp 1791550886
REAL = [
    (2100570682593546310, 1789650052),
    (2108543358197207048, 1791550886),
]


@pytest.mark.parametrize("post_id,unix_s", REAL)
def test_decodes_real_posts_to_the_second_x_reports(post_id, unix_s):
    assert snowflake.to_unix_ms(post_id) // 1000 == unix_s


def test_decode_matches_hand_computed_value():
    # (2100570682593546310 >> 22) == 500815077446; + epoch 1288834974657
    assert snowflake.to_unix_ms(2100570682593546310) == 1789650052103


def test_accepts_string_ids():
    assert snowflake.to_unix_ms("2100570682593546310") == snowflake.to_unix_ms(2100570682593546310)


def test_datetime_is_utc_aware():
    dt = snowflake.to_datetime(2100570682593546310)
    assert dt.tzinfo is timezone.utc
    assert dt == datetime(2026, 9, 17, 13, 0, 52, 103000, tzinfo=timezone.utc)


def test_bit_fields_round_trip_from_a_synthetic_id():
    unix_ms, dc, worker, seq = 1789650052103, 17, 9, 4000
    sid = ((unix_ms - snowflake.TWITTER_EPOCH_MS) << 22) | (dc << 17) | (worker << 12) | seq
    parts = snowflake.decode(sid)
    assert (parts.unix_ms, parts.datacenter, parts.worker, parts.sequence) == (unix_ms, dc, worker, seq)


def test_floor_id_is_the_smallest_id_at_that_millisecond():
    when = datetime(2026, 10, 1, tzinfo=timezone.utc)
    lo = snowflake.floor_id(when)
    assert snowflake.to_unix_ms(lo) == int(when.timestamp() * 1000)
    assert snowflake.to_unix_ms(lo - 1) == int(when.timestamp() * 1000) - 1
    assert lo & ((1 << 22) - 1) == 0


def test_floor_id_accepts_unix_ms():
    assert snowflake.floor_id(1789650052103) == (1789650052103 - snowflake.TWITTER_EPOCH_MS) << 22


@pytest.mark.parametrize("bad", [-1, 1 << 63, "abc", None, 20, 29_700_000_000])
def test_rejects_non_snowflakes(bad):
    # 20 is jack's first tweet; 29.7e9 is a late pre-Snowflake sequential ID.
    with pytest.raises(ValueError):
        snowflake.to_unix_ms(bad)


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/supabase/status/2100570682593546310",
        "https://twitter.com/supabase/status/2100570682593546310?s=20",
        "https://x.com/supabase/status/2100570682593546310/photo/1",
        "https://mobile.twitter.com/supabase/statuses/2100570682593546310",
    ],
)
def test_status_id_from_url(url):
    assert snowflake.status_id_from_url(url) == 2100570682593546310


@pytest.mark.parametrize("url", ["https://x.com/supabase", "", "https://example.com/status/123"])
def test_status_id_from_url_rejects_non_post_urls(url):
    with pytest.raises(ValueError):
        snowflake.status_id_from_url(url)
