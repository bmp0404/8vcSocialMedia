"""harness.watcher: the freshness cutoff, ingestion windows, restarts, the arm coin, thread rows and the loop.

Every read goes through FakeReader; nothing touches the network.
"""

import gzip
import json
import os
import signal
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from harness.context import snowflake
from harness.context.http import FetchError
from harness.context.xreader import XPost
from harness.llm import MockLLM
from harness.watcher import arms, hot, ingest, loop, state as st
from harness.watcher.watchlist import Watched, Watchlist, WatchlistError, load as load_watchlist

T0 = datetime(2026, 10, 16, 18, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def sid(at: datetime, n: int = 0) -> str:
    """A real Snowflake id for a post made at `at`."""
    return str(snowflake.floor_id(at) + n)


def xpost(at: datetime, author="WNBA", text="tip-off soon", fetched_at: datetime | None = None, n=0, **kw) -> XPost:
    pid = kw.pop("id", None) or sid(at, n)
    return XPost(id=pid, text=text, author_handle=author, created_at=snowflake.to_datetime(pid),
                 fetched_at=(fetched_at or at).isoformat(), **kw)


class FakeReader:
    """An XReader over fixed timelines and threads. `now` stamps every read; `fail` makes a handle's read raise."""

    def __init__(self, timelines=None, threads=None, now=T0):
        self.timelines = timelines or {}  # handle -> [XPost], newest first
        self.threads = threads or {}  # status id -> [XPost] replies, likes order
        self.now = now
        self.fail: set[str] = set()
        self.calls = Counter()

    def _stamp(self, p: XPost) -> XPost:
        return XPost(**(p.__dict__ | {"fetched_at": self.now.isoformat()}))

    def profile_statuses(self, handle, limit=20, with_replies=False):
        self.calls["profile"] += 1
        if handle in self.fail:
            raise FetchError(f"HTTP 404 from api.fxtwitter.com for /2/profile/{handle}/statuses")
        return [self._stamp(p) for p in self.timelines.get(handle, []) if p.created_at <= self.now][:limit]

    def conversation(self, status_id, ranking_mode="likes"):
        self.calls[f"conversation:{ranking_mode}"] += 1
        replies = [self._stamp(r) for r in self.threads.get(status_id, []) if r.created_at <= self.now]
        return replies if ranking_mode == "likes" else sorted(replies, key=lambda r: -int(r.id))

    def status(self, status_id):
        self.calls["status"] += 1
        p = next(p for ps in self.timelines.values() for p in ps if p.id == status_id)
        return self._stamp(p)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def config_for(tmp_path, **kw) -> loop.WatchConfig:
    return loop.WatchConfig(profiles=("wnba",), watch_dir=tmp_path / "watch", hot_dir=tmp_path / "hot",
                            threads_dir=tmp_path / "threads", sync_every_s=None, **kw)


def watcher(tmp_path, reader, clock, **kw) -> loop.Watcher:
    return loop.Watcher(config_for(tmp_path, **kw), MockLLM(), reader, clock=clock, echo=False)


WNBA = Watched("wnba", "WNBA", broad=False)


def rows(db, table):
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


# ------------------------------------------------------------------ freshness cutoff


def test_a_post_is_hot_only_while_under_ten_minutes_old():
    assert hot.is_hot(T0, T0 + 10 * M - timedelta(seconds=1))
    assert not hot.is_hot(T0, T0 + 10 * M)
    assert not hot.is_hot(T0, T0 + 3 * 60 * M)
    assert hot.is_hot(T0, T0 - timedelta(seconds=5))  # a post dated after the read (clock skew) is fresh


def test_poll_queues_a_hot_job_only_for_a_post_under_the_cutoff(tmp_path):
    fresh, old = xpost(T0 - 9 * M, text="fresh one"), xpost(T0 - 11 * M, text="older one")
    w = watcher(tmp_path, FakeReader({"WNBA": [fresh, old]}), Clock())
    assert w.poll(WNBA) == 2
    queued = {r["status_id"] for r in w.state.hot_rows()}
    assert queued == {fresh.id}
    posts = {r["status_id"]: r for r in rows(w.state.path, "posts")}
    assert posts[fresh.id]["hot"] == 1 and posts[old.id]["hot"] == 0
    assert posts[fresh.id]["age_at_first_seen_s"] == pytest.approx(540, abs=1)


# ------------------------------------------------------------------ ingestion at +1h and +24h


def test_checkpoints_are_planned_for_the_windows_still_open_when_a_post_is_first_seen():
    H = timedelta(hours=1)
    assert ingest.plan_checkpoints(T0, T0 + 2 * M) == ([(1, T0 + H, T0 + 2 * H), (24, T0 + 24 * H, T0 + 48 * H)], [])
    assert ingest.plan_checkpoints(T0, T0 + 90 * M)[0][0] == (1, T0 + H, T0 + 2 * H)  # due at once, still inside
    assert ingest.plan_checkpoints(T0, T0 + 2 * H) == ([(24, T0 + 24 * H, T0 + 48 * H)], [1])
    assert ingest.plan_checkpoints(T0, T0 + 30 * H) == ([(24, T0 + 24 * H, T0 + 48 * H)], [1])
    assert ingest.plan_checkpoints(T0, T0 + 48 * H) == ([], [1, 24])


def thread_for(root: XPost, n: int, author="fan", likes=lambda i: 10 - i) -> list[XPost]:
    return [xpost(root.created_at + (i + 1) * M, author=f"{author}{i}", text=f"@{root.author_handle} what a reply number {i}",
                  replying_to=root.author_handle, replying_to_status=root.id, likes=likes(i), author_followers=50 + i,
                  author_description="a fan's bio")
            for i in range(n)]


def test_ingestion_runs_inside_each_window_and_marks_a_closed_one_missed(tmp_path):
    post = xpost(T0, replies=5)
    reader = FakeReader({"WNBA": [post]}, {post.id: thread_for(post, 5)}, now=T0 + 2 * M)
    clock = Clock(T0 + 2 * M)
    w = watcher(tmp_path, reader, clock)
    w.poll(WNBA)
    clock.t = reader.now = T0 + 59 * M
    assert not w.ingest_one()  # +1h not open yet
    clock.t = reader.now = T0 + 61 * M
    assert w.ingest_one()
    assert not w.ingest_one()  # +24h not open yet
    clock.t = reader.now = T0 + 49 * 60 * M  # the watcher was down through all of [24h, 48h)
    assert not w.ingest_one()
    states = {r["checkpoint"]: r["state"] for r in rows(w.state.path, "ingest")}
    assert states == {1: st.KEPT, 24: st.MISSED}
    assert reader.calls["conversation:likes"] == reader.calls["conversation:recency"] == reader.calls["status"] == 1


def test_low_coverage_and_empty_threads_are_recorded_but_not_kept(tmp_path):
    sparse, empty = xpost(T0, replies=10, n=1), xpost(T0, replies=0, n=2)
    reader = FakeReader({"WNBA": [sparse, empty]}, {sparse.id: thread_for(sparse, 7)}, now=T0 + 61 * M)
    w = watcher(tmp_path, reader, Clock(T0 + 61 * M))
    w.poll(WNBA)
    assert w.ingest_one() and w.ingest_one()
    by_id = {(r["status_id"], r["checkpoint"]): r for r in rows(w.state.path, "ingest")}
    assert by_id[(sparse.id, 1)]["state"] == st.LOW_COVERAGE and by_id[(sparse.id, 1)]["coverage"] == 0.7
    assert by_id[(empty.id, 1)]["state"] == st.NO_REPLIES
    assert not (tmp_path / "threads" / ingest.ROOTS_FILE).exists()


def test_a_failed_thread_read_is_retried_then_failed(tmp_path):
    post = xpost(T0, replies=5)
    reader = FakeReader({"WNBA": [post]}, now=T0 + 61 * M)
    clock = Clock(T0 + 61 * M)
    w = watcher(tmp_path, reader, clock)
    w.poll(WNBA)
    reader.conversation = lambda *a, **k: (_ for _ in ()).throw(FetchError("HTTP 503 from api.fxtwitter.com"))
    for i in range(loop.INGEST_ATTEMPTS):
        assert w.ingest_one()
        clock.t += loop.INGEST_RETRY
    job = next(r for r in rows(w.state.path, "ingest") if r["checkpoint"] == 1)
    assert job["state"] == st.FAILED and job["attempts"] == loop.INGEST_ATTEMPTS and "HTTP 503" in job["error"]


# ------------------------------------------------------------------ restarts


def test_a_restart_neither_requeues_seen_posts_nor_reschedules_their_threads(tmp_path):
    post = xpost(T0 - 3 * M)
    reader = FakeReader({"WNBA": [post]})
    first = watcher(tmp_path, reader, Clock())
    first.poll(WNBA)
    before = (rows(first.state.path, "hot"), rows(first.state.path, "ingest"))
    second = watcher(tmp_path, reader, Clock(T0 + M))
    assert second.poll(WNBA) == 0
    assert (rows(second.state.path, "hot"), rows(second.state.path, "ingest")) == before


def test_recover_requeues_an_interrupted_hot_job_and_finishes_one_whose_file_was_written(tmp_path):
    a, b = xpost(T0 - 3 * M, n=1), xpost(T0 - 2 * M, n=2)
    w = watcher(tmp_path, FakeReader({"WNBA": [a, b]}), Clock())
    w.poll(WNBA)
    assert w.state.claim_hot(T0)["status_id"] == a.id and w.state.claim_hot(T0)["status_id"] == b.id
    w.cfg.hot_dir.mkdir()
    hot.hot_path(w.cfg.hot_dir, b.id).write_text("{}", encoding="utf-8")  # b crashed after writing its file
    moved = st.State(w.state.path).recover(lambda s: hot.hot_path(w.cfg.hot_dir, s))
    assert moved == {"hot_done": 1, "hot_requeued": 1, "ingest_requeued": 0}
    assert {r["status_id"]: r["state"] for r in w.state.hot_rows()} == {a.id: st.HOT_QUEUED, b.id: st.HOT_DONE}


def test_a_queued_job_older_than_the_lane_cutoff_goes_stale_instead_of_drafting(tmp_path):
    post = xpost(T0 - 3 * M)
    clock = Clock()
    w = watcher(tmp_path, FakeReader({"WNBA": [post]}), clock)
    w.poll(WNBA)
    clock.t = T0 + 40 * M  # the watcher was stopped with the job queued
    w.run_hot_job(w.state.claim_hot(clock.t))
    assert w.state.hot_rows()[0]["state"] == st.HOT_STALE
    assert not hot.hot_path(w.cfg.hot_dir, post.id).exists()


# ------------------------------------------------------------------ the arm coin


def test_the_arm_coin_is_a_pure_function_of_seed_and_status_id():
    ids = [str(snowflake.floor_id(T0) + i) for i in range(3000)]
    picks = [arms.arm_for(i) for i in ids]
    assert picks == [arms.arm_for(i) for i in ids]
    # Pinned, so a change to the hash or the arm order shows up as a failure, not a silent reassignment.
    assert picks[:6] == ["human", "continuation", "continuation", "human", "human", "styled"]
    assert arms.arm_for("2110064336911597749") == "continuation"
    assert all(900 < n < 1100 for n in Counter(picks).values()) and set(picks) == set(arms.ARMS)
    other = [arms.arm_for(i, seed="another-seed") for i in ids]
    assert sum(a != b for a, b in zip(picks, other)) > 1500
    with pytest.raises(ValueError, match="status id"):
        arms.arm_for("https://x.com/WNBA/status/1")


# ------------------------------------------------------------------ thread items


def ingest_one_thread(tmp_path, n_replies=5):
    post = xpost(T0, replies=n_replies, author_followers=1_000_000, author_description="the league")
    replies = thread_for(post, n_replies)
    nested = xpost(T0 + 9 * M, author="fan9", text="@fan0 agreed", replying_to="fan0", replying_to_status=replies[0].id)
    reader = FakeReader({"WNBA": [post]}, {post.id: replies[:-1] + [nested]}, now=T0 + 61 * M)
    reader.timelines["WNBA"][0].replies = n_replies + 1
    w = watcher(tmp_path, reader, Clock(T0 + 61 * M))
    w.poll(WNBA)
    assert w.ingest_one()
    return post, replies, nested


def read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# The fields harness/corpus/load.py (sam/corpus) reads from a row, with the types it expects.
LOADER_FIELDS = {"id": str, "author_handle": str, "fetched_at": str, "text": str, "likes": int, "reposts": int,
                 "replies": int, "quotes": int, "media_types": list, "n_links": int, "n_mentions": int}
LOADER_OPTIONAL = ("bookmarks", "views", "author_followers", "author_verified", "replying_to", "replying_to_status",
                   "quoted_id", "lang", "is_repost", "reposted_by")


def test_thread_rows_have_the_shape_the_corpus_loader_reads(tmp_path):
    post, replies, nested = ingest_one_thread(tmp_path)
    threads = tmp_path / "threads"
    assert sorted(p.name for p in threads.iterdir()) == sorted(
        [ingest.ROOTS_FILE, ingest.REPLIES_FILE, *ingest.EMPTY_INPUTS])
    assert read_gz(threads / "x_metrics.jsonl.gz") == []
    roots, reps = read_gz(threads / ingest.ROOTS_FILE), read_gz(threads / ingest.REPLIES_FILE)
    for row in roots + reps:
        for key, kind in LOADER_FIELDS.items():
            assert isinstance(row[key], kind), (key, row[key])
        assert set(LOADER_OPTIONAL) <= set(row)
        assert datetime.fromisoformat(row["fetched_at"]).tzinfo is not None
        assert row["checkpoint"] == "+1h" and row["watch_profile"] == "wnba"
    (root,) = roots
    assert root["id"] == post.id and root["replying_to_status"] is None and root["is_repost"] is False
    assert root["coverage"] == pytest.approx(5 / 6) and root["reply_count"] == 6 and root["sampled"] == 5
    assert [r["id"] for r in reps] == sorted([r.id for r in replies[:-1]] + [nested.id], key=int)
    for r in reps:
        assert r["reply_to_post"] == post.id and r["coverage"] == root["coverage"]
        assert "author_description" not in r
    by_id = {r["id"]: r for r in reps}
    assert by_id[replies[0].id]["minutes_after_parent"] == pytest.approx(1.0)
    assert by_id[replies[0].id]["author_followers"] == 50 and by_id[replies[0].id]["likes"] == 10
    # The nested reply's parent is the reply it answers, not the post.
    assert by_id[nested.id]["minutes_after_parent"] == pytest.approx(8.0)
    assert by_id[nested.id]["minutes_after_post"] == pytest.approx(9.0)


def test_appends_are_gzip_members_read_back_as_one_stream(tmp_path):
    threads = tmp_path / "threads"
    ingest.append_thread(threads, {"id": "1"}, [{"id": "2"}])
    ingest.append_thread(threads, {"id": "3"}, [])
    assert [r["id"] for r in read_gz(threads / ingest.ROOTS_FILE)] == ["1", "3"]
    assert [r["id"] for r in read_gz(threads / ingest.REPLIES_FILE)] == ["2"]


def test_the_corpus_loader_reads_the_threads_directory(tmp_path):
    """Runs once sam/corpus is merged; this branch doesn't have harness.corpus."""
    corpus = pytest.importorskip("harness.corpus")
    post, replies, nested = ingest_one_thread(tmp_path)
    c = corpus.load(tmp_path / "threads")
    assert c.get(post.id).kind == "post" and not c.get(post.id).in_dataset
    for r in replies[:-1]:
        item = c.get(r.id)
        assert item.kind == "reply" and item.thread_root == post.id and item.parent_id == post.id
        assert item.thread_coverage == pytest.approx(5 / 6)
    assert c.get(nested.id).parent_id == replies[0].id


# ------------------------------------------------------------------ the watch list and polling


def test_only_the_accounts_own_root_posts_count_and_broad_accounts_need_a_keyword():
    wl = Watchlist("wnba", (Watched("wnba", "WNBA", False), Watched("wnba", "espnW", True)), ("Fever",))
    assert wl.counts("WNBA", xpost(T0))
    assert not wl.counts("WNBA", xpost(T0, is_repost=True, reposted_by="WNBA"))
    assert not wl.counts("WNBA", xpost(T0, replying_to="someone", replying_to_status="1"))
    assert not wl.counts("WNBA", xpost(T0, author="someone_else"))
    assert wl.counts("espnW", xpost(T0, author="espnW", text="The Fever win"))
    assert not wl.counts("espnW", xpost(T0, author="espnW", text="NFL Sunday"))
    with pytest.raises(WatchlistError, match="isn't on the wnba watch list"):
        wl.counts("nobody", xpost(T0, author="nobody"))


def test_both_profiles_have_a_watch_list_of_the_size_the_brief_asked_for():
    wnba, devtools = load_watchlist("wnba"), load_watchlist("devtools")
    assert len(wnba.handles) == 22 and sum(w.broad for w in wnba.handles) == 11
    assert 20 <= len(devtools.handles) <= 30 and not any(w.broad for w in devtools.handles)


def test_a_failed_handle_is_logged_and_skipped_and_the_rest_still_poll(tmp_path):
    reader = FakeReader({"IndianaFever": [xpost(T0 - M, author="IndianaFever")]})
    reader.fail.add("WNBA")
    w = watcher(tmp_path, reader, Clock())
    assert w.poll(WNBA) is None
    assert w.poll(Watched("wnba", "IndianaFever", False)) == 1
    polls = rows(w.state.path, "polls")
    assert [(p["handle"], p["ok"]) for p in polls] == [("WNBA", 0), ("IndianaFever", 1)]
    assert "FetchError: HTTP 404" in polls[0]["error"]
    assert "@WNBA skipped this cycle: FetchError: HTTP 404" in w.log_path.read_text(encoding="utf-8")


def test_the_schedule_interleaves_profiles(tmp_path):
    w = loop.Watcher(loop.WatchConfig(watch_dir=tmp_path / "watch", sync_every_s=None), MockLLM(), FakeReader(),
                     echo=False)
    order = w.schedule()
    assert len(order) == len(set(order)) == 46
    assert [x.profile for x in order[:4]] == ["wnba", "devtools", "wnba", "devtools"]


# ------------------------------------------------------------------ the hot path


def post_with_arm(arm: str, at: datetime) -> XPost:
    n = next(i for i in range(100) if arms.arm_for(sid(at, i)) == arm)
    return xpost(at, n=n, text="Caitlin Clark is back at practice today", replies=0, author_followers=900_000)


@pytest.mark.parametrize("arm,method", [("continuation", "continuation"), ("styled", "styled")])
def test_a_drafting_arm_runs_plan_writer_gate_and_veto_and_writes_its_file(tmp_path, arm, method):
    post = post_with_arm(arm, T0 - 2 * M)
    record = hot.run_hot(MockLLM(), "wnba", post, reader=FakeReader(), out_dir=tmp_path, seed=arms.DEFAULT_SEED,
                         detected_at=T0 - M, clock=Clock())
    saved = json.loads(hot.hot_path(tmp_path, post.id).read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(record, default=str))
    assert saved["arm"] == arm and saved["offer"] == "draft" and saved["method"] == method
    assert saved["draft"].startswith(f"mock {'continuation' if arm == 'continuation' else 'styled reply'}")
    assert saved["profile"] == "wnba" and saved["plan"]["tool"] == "reply"
    assert saved["decision"]["tool"] == "reply" and saved["draft_meta"]["judge"]["passed"]
    assert set(saved["timings"]) == {"plan_s", "draft_s", "total_s", "detected_to_finished_s",
                                     "post_age_at_detection_s", "post_age_at_finish_s"}
    assert saved["timings"]["post_age_at_detection_s"] == 60.0 and saved["timings"]["post_age_at_finish_s"] == 120.0


def test_the_human_arm_asks_for_a_one_liner_and_shows_no_draft(tmp_path):
    post = post_with_arm("human", T0 - 2 * M)
    record = hot.run_hot(MockLLM(), "wnba", post, reader=FakeReader(), out_dir=tmp_path, seed=arms.DEFAULT_SEED,
                         detected_at=T0 - M, clock=Clock())
    assert record["offer"] == "human_prompt" and record["draft"] is None and record["decision"] is None
    assert record["prompt"].startswith("Reply to @WNBA's post from @_sgu_ with a one-liner of your own")


def test_a_planned_abstention_is_written_with_its_reason(tmp_path, monkeypatch):
    post = post_with_arm("continuation", T0 - 2 * M)
    monkeypatch.setattr(hot.lanes, "plan_targets", lambda *a: (
        {post.url: {"target_url": post.url, "tool": "abstain", "reason": "mute_risk", "detail": "injury news"}}, ""))
    record = hot.run_hot(MockLLM(), "wnba", post, reader=FakeReader(), out_dir=tmp_path, seed=arms.DEFAULT_SEED,
                         detected_at=T0 - M, clock=Clock())
    assert (record["offer"], record["abstain_reason"], record["abstain_detail"]) == ("abstain", "mute_risk", "injury news")


def test_the_thread_is_read_through_the_watchers_reader():
    post = xpost(T0)
    replies = thread_for(post, 3, likes=lambda i: i)
    replies.append(xpost(T0 + 5 * M, author="x", text="@fan0 ok sure thing", replying_to_status=replies[0].id))
    replies.append(xpost(T0 + 6 * M, author="y", text="@WNBA lol", replying_to_status=post.id))  # too few words
    got = hot.reader_fetch(FakeReader(threads={post.id: replies}, now=T0 + 7 * M))(post.url)
    assert [(r["author"], r["text"], r["likes"]) for r in got] == [
        ("fan2", "what a reply number 2", 2), ("fan1", "what a reply number 1", 1), ("fan0", "what a reply number 0", 0)]


def test_an_unreadable_thread_raises_the_lanes_thread_unavailable():
    reader = FakeReader()
    reader.conversation = lambda *a: (_ for _ in ()).throw(FetchError("HTTP 404 from api.fxtwitter.com"))
    with pytest.raises(hot.thread.ThreadUnavailable, match="HTTP 404"):
        hot.reader_fetch(reader)(xpost(T0).url)


# ------------------------------------------------------------------ the loop


def live_reader():
    now = datetime.now(timezone.utc)
    return FakeReader({"WNBA": [xpost(now - M, text="Caitlin Clark is back at practice today")]}, now=now)


def test_run_drafts_a_fresh_post_then_stops_and_releases_the_pid_file(tmp_path):
    w = loop.Watcher(config_for(tmp_path, interval_s=2.0, duration_s=1.5), MockLLM(), live_reader(), echo=False)
    reason = w.run()
    assert reason.startswith("duration")
    assert not w.pid_path.exists()
    (row,) = w.state.hot_rows()
    assert row["state"] == st.HOT_DONE and hot.hot_path(w.cfg.hot_dir, row["status_id"]).exists()
    assert rows(w.state.path, "runs")[0]["stop_reason"].startswith("duration")


def test_sigterm_stops_the_loop_cleanly(tmp_path):
    w = loop.Watcher(config_for(tmp_path, interval_s=2.0), MockLLM(), live_reader(), echo=False)
    original = signal.getsignal(signal.SIGTERM)

    def send_when_installed():
        deadline = time.monotonic() + 10
        while signal.getsignal(signal.SIGTERM) is original and time.monotonic() < deadline:
            time.sleep(0.02)
        if signal.getsignal(signal.SIGTERM) is not original:
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=send_when_installed, daemon=True).start()
    assert w.run() == "SIGTERM"
    assert signal.getsignal(signal.SIGTERM) is original and not w.pid_path.exists()


def test_a_second_watcher_refuses_while_the_first_is_alive(tmp_path):
    w = watcher(tmp_path, FakeReader(), Clock())
    w.pid_path.write_text(str(os.getppid()), encoding="utf-8")  # a live process that isn't this one
    with pytest.raises(loop.AlreadyRunning, match=f"pid {os.getppid()}"):
        w.run()


def test_a_handle_that_keeps_failing_is_polled_less_often(tmp_path):
    reader = live_reader()
    reader.fail.add("WNBA")
    w = loop.Watcher(config_for(tmp_path, interval_s=0.2, duration_s=2.0), MockLLM(), reader, echo=False)
    w.watchlists = {"wnba": Watchlist("wnba", (WNBA, Watched("wnba", "IndianaFever", False)), ())}
    w.run()
    polls = Counter(p["handle"] for p in rows(w.state.path, "polls"))
    # Failures at 0, 0.2, 0.6 and 1.4 s: the second and later failures wait 2, then 4 intervals.
    assert polls["WNBA"] == 4 and polls["IndianaFever"] >= 8
