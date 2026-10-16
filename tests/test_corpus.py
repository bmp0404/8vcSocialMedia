"""harness.corpus: snapshot round trip, Snowflake decode, dedupe and merge rules, all on tiny fixtures."""

import csv
import gzip
import io
import json
from datetime import datetime, timezone

import pytest

from harness.corpus import created_at, load
from harness.corpus.snapshot import FILES, snapshot, verify


def test_snowflake_matches_the_time_x_reports():
    # Deno's acquisition post; fxtwitter reports created_at 2026-10-09T13:01:26.092Z.
    assert created_at("2108543358197207048") == datetime(2026, 10, 9, 13, 1, 26, 92000, tzinfo=timezone.utc)


def row(sid, author="acme", fetched="2026-10-16T01:00:00+00:00", likes=1, **kw):
    base = {"id": sid, "text": f"text {sid}", "author_handle": author, "fetched_at": fetched, "likes": likes,
            "reposts": 0, "replies": 0, "quotes": 0, "bookmarks": 0, "views": 10, "author_followers": 100,
            "author_verified": False, "replying_to": None, "replying_to_status": None, "quoted_id": None,
            "is_repost": False, "reposted_by": None, "media_types": [], "n_links": 0, "n_mentions": 0, "lang": "en"}
    return base | kw


def write_gz(path, text):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)


def jsonl(rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


def csv_text(header, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


# Status IDs chosen so their Snowflake times are 2026-10-01 (posts) and later (replies).
POST, OTHER, REPLY, SELF, REPOST = "2105000000000000000", "2105000000000000001", "2105100000000000000", \
    "2105100000000000001", "2104000000000000000"


@pytest.fixture
def raw(tmp_path):
    write_gz(tmp_path / "x_metrics.jsonl.gz", jsonl([
        row(POST, likes=5, fetched="2026-10-16T01:00:00+00:00"),
        {"id": "1", "error": "HTTP 404", "fetched_at": "2026-10-16T01:00:00+00:00"},
    ]))
    write_gz(tmp_path / "author_timelines.jsonl.gz", jsonl([
        row(POST, likes=9, fetched="2026-10-16T02:00:00+00:00", author_description="we make acme") | {"timeline_of": "acme"},
        row(SELF, replying_to="acme", replying_to_status=POST) | {"timeline_of": "acme"},
        row(REPOST, author="bigco", is_repost=True, reposted_by="acme") | {"timeline_of": "acme"},
        {"timeline_of": "acme", "error": "HTTP 404 cursor", "fetched_at": "2026-10-16T02:00:00+00:00"},
    ]))
    write_gz(tmp_path / "reply_samples.jsonl.gz", jsonl([
        row(REPLY, author="fan", replying_to="acme", replying_to_status=POST,
            fetched="2026-10-16T00:30:00+00:00") | {"reply_to_post": POST, "coverage": 0.5, "reply_count": 4},
        row(SELF, replying_to="acme", replying_to_status=POST, likes=3,
            fetched="2026-10-16T03:00:00+00:00") | {"reply_to_post": POST, "coverage": 0.5, "reply_count": 4},
        {"reply_to_post": OTHER, "coverage": None, "reply_count": 0, "empty": True},
    ]))
    write_gz(tmp_path / "boost_flags.csv.gz", csv_text(
        ["post_id", "boost_suspect", "is_thread_continuation"],
        [{"post_id": POST, "boost_suspect": "True", "is_thread_continuation": "False"}]))
    write_gz(tmp_path / "timeline_posts.csv.gz", csv_text(
        ["post_id", "is_thread_continuation"], [{"post_id": POST, "is_thread_continuation": "False"}]))
    return tmp_path


def test_dedupes_by_status_and_keeps_the_latest_counts(raw):
    c = load(raw)
    assert sorted(i.status_id for i in c.items) == sorted([POST, SELF, REPOST, REPLY])
    post = c.get(POST)
    assert post.sources == ["x_metrics", "author_timelines"]
    assert post.metrics["likes"] == 9 and post.metrics["fetched_at"] == "2026-10-16T02:00:00+00:00"
    assert post.author_description == "we make acme"
    assert post.in_dataset and post.boost_suspect is True and not post.is_thread_continuation
    assert c.get(SELF).metrics["likes"] == 3  # the reply sample read it last


def test_kinds_parents_and_flags(raw):
    c = load(raw)
    assert c.get(POST).kind == "post" and c.get(POST).parent_id is None
    reply = c.get(REPLY)
    assert reply.kind == "reply" and reply.parent_id == POST and reply.parent_author == "acme"
    assert reply.thread_root == POST and reply.thread_coverage == 0.5 and not reply.is_thread_continuation
    assert c.get(SELF).is_thread_continuation  # replying to its own author
    repost = c.get(REPOST)
    assert repost.kind == "post" and repost.author == "bigco" and repost.reposted_by == ["acme"]
    assert repost.boost_suspect is None  # no boost check ran on it
    assert c.skipped == {"x_metrics": 1, "author_timelines": 1, "reply_samples": 1}


def test_item_derived_fields(raw):
    post = load(raw).get(POST)
    assert post.item_id == f"x:{POST}" and post.url == f"https://x.com/acme/status/{POST}"
    assert post.engagement == 9
    assert post.mature  # created 2026-10-01, read 2026-10-16
    d = post.to_dict()
    assert d["created_at"].startswith("2026-") and d["mature"] is True and d["item_id"] == f"x:{POST}"


def test_missing_source_file_is_an_error(raw):
    (raw / "reply_samples.jsonl.gz").unlink()
    with pytest.raises(FileNotFoundError, match="reply_samples"):
        load(raw)


def test_bad_flag_value_is_an_error(raw):
    write_gz(raw / "boost_flags.csv.gz", csv_text(["post_id", "boost_suspect"], [{"post_id": POST, "boost_suspect": "yes"}]))
    with pytest.raises(ValueError, match="True or False"):
        load(raw)


def test_snapshot_round_trip_and_verify(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in FILES:
        (src / name).write_text(f"line one of {name}\nline two\n", encoding="utf-8")
    dest = tmp_path / "raw"
    m = snapshot(src, dest)
    assert m["files"]["x_metrics.jsonl"]["lines"] == 2
    assert verify(dest) == []
    with gzip.open(dest / "x_metrics.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write("tampered\n")
    assert verify(dest) == ["x_metrics.jsonl: sha256 differs from MANIFEST.json"]


def test_snapshot_refuses_an_incomplete_source(tmp_path):
    with pytest.raises(FileNotFoundError, match="lacks"):
        snapshot(tmp_path, tmp_path / "raw")
