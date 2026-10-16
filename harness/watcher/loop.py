"""`harness watch`: poll every watched account, draft for fresh posts, ingest threads as they age.

One process watches every profile. The main thread polls: each handle once per `interval_s`, the
handles spread evenly over the interval and the profiles interleaved, so the requests come at a
steady rate rather than in bursts. Between polls it runs at most one due ingestion job every
`ingest_every_s` and, every `sync_every_s`, `harness outcomes sync`. One worker thread drafts hot
targets, one at a time, so a slow LLM call never delays a poll. Every fxtwitter request goes
through the shared throttle in `harness/context/http.py` (one request per second across every
process on the machine), with no disk cache, because the watcher wants live counts.

A failed read on one handle is logged and that handle is skipped until its next turn; nothing a
single account does stops the watcher. SIGTERM or SIGINT stops it after the hot job in flight.
"""

import json
import os
import signal
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import config
from harness.context.http import PoliteClient
from harness.context.xreader import FxTwitterReader, XPost
from harness.minds import settings as action_settings
from harness.watcher import hot, ingest, state as st
from harness.watcher.arms import DEFAULT_SEED
from harness.watcher.watchlist import Watched, load as load_watchlist

WATCH_DIR = config.OUT_DIR / "watch"
HOT_DIR = config.OUT_DIR / "hot"
DEFAULT_PROFILES = ("wnba", "devtools")
# 22 to 24 handles per list every 2 minutes is about 0.2 requests per second per list, the brief's
# target, which leaves room under the 1 request per second shared throttle for drafts and ingestion.
DEFAULT_INTERVAL_S = 120.0
# One ingestion job is three requests, so one every 15 seconds is at most 0.2 requests per second.
DEFAULT_INGEST_EVERY_S = 15.0
INGEST_RETRY = timedelta(minutes=5)
INGEST_ATTEMPTS = 3
SYNC_EVERY_S = 1800.0  # outcomes' own TIMELINE_EVERY: half the +1h window
HEARTBEAT_S = 300.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CountingClient(PoliteClient):
    """PoliteClient without the disk cache that counts every network attempt, retries included."""

    def __init__(self):
        super().__init__(min_interval_s=1.0, cache_dir=None)
        self.requests = 0
        self._count_lock = threading.Lock()
        self._mine = threading.local()

    def _wait_turn(self, host: str) -> None:
        super()._wait_turn(host)
        with self._count_lock:
            self.requests += 1
        self._mine.n = getattr(self._mine, "n", 0) + 1

    def thread_requests(self) -> int:
        """Requests this thread has made, so a poll's count leaves out the hot thread's."""
        return getattr(self._mine, "n", 0)


def xpost_from_dict(d: dict) -> XPost:
    return XPost(**(d | {"created_at": datetime.fromisoformat(d["created_at"])}))


@dataclass
class WatchConfig:
    profiles: tuple[str, ...] = DEFAULT_PROFILES
    interval_s: float = DEFAULT_INTERVAL_S
    timeline_limit: int = 10  # one fxtwitter page holds about 20 entries, so this is one request
    ingest_every_s: float = DEFAULT_INGEST_EVERY_S
    sync_every_s: float | None = SYNC_EVERY_S  # None turns the outcomes sync off
    duration_s: float | None = None  # stop after this long; None runs until a signal
    arm_seed: str = DEFAULT_SEED
    watch_dir: Path = WATCH_DIR
    hot_dir: Path = HOT_DIR
    threads_dir: Path = ingest.THREADS_DIR
    hot_max_age: timedelta = hot.HOT_MAX_AGE
    action: action_settings.ActionConfig = field(default_factory=action_settings.load)


class AlreadyRunning(RuntimeError):
    pass


class Watcher:
    def __init__(self, cfg: WatchConfig, llm, reader=None, *, clock=utcnow, echo: bool = True):
        self.cfg, self.llm, self.clock, self.echo = cfg, llm, clock, echo
        self.client = None
        if reader is None:
            self.client = CountingClient()
            reader = FxTwitterReader(self.client, max_age_s=0)
        self.reader = reader
        self.watchlists = {p: load_watchlist(p) for p in cfg.profiles}
        cfg.watch_dir.mkdir(parents=True, exist_ok=True)
        self.state = st.State(cfg.watch_dir / "state.db")
        self.log_path = cfg.watch_dir / "watch.log"
        self.pid_path = cfg.watch_dir / "watch.pid"
        self._hot_event = threading.Event()
        self._log_lock = threading.Lock()

    # ------------------------------------------------------------ logging

    def log(self, message: str) -> None:
        line = f"{self.clock().isoformat(timespec='seconds')} {message}"
        with self._log_lock:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if self.echo:
                print(line, flush=True)

    def requests(self) -> int | None:
        return self.client.requests if self.client else None

    # ------------------------------------------------------------ polling

    def schedule(self) -> list[Watched]:
        """Every watched handle, the profiles interleaved, in the order the first cycle polls them."""
        lists = [list(self.watchlists[p].handles) for p in self.cfg.profiles]
        out = []
        for i in range(max(len(x) for x in lists)):
            out.extend(x[i] for x in lists if i < len(x))
        return out

    def poll(self, w: Watched) -> int:
        """Read one handle's timeline and record what's new. Returns the number of new watched posts."""
        start, before = time.monotonic(), self._thread_requests()
        try:
            posts = self.reader.profile_statuses(w.handle, limit=self.cfg.timeline_limit)
        except Exception as e:  # noqa: BLE001 - one account's failure skips that account, never the watcher
            error = f"{type(e).__name__}: {e}"
            self.state.log_poll(self.clock(), w.profile, w.handle, ok=False, requests=self._thread_requests() - before,
                                seconds=time.monotonic() - start, error=error)
            self.log(f"[poll] {w.profile} @{w.handle} skipped this cycle: {error}")
            return 0
        wl, new = self.watchlists[w.profile], 0
        for p in posts:
            if not wl.counts(w.handle, p) or self.state.known(p.id):
                continue
            seen_at = datetime.fromisoformat(p.fetched_at)  # the read's own time, so queueing delay doesn't age it
            fresh = hot.is_hot(p.created_at, seen_at, self.cfg.hot_max_age)
            if not self.state.add_post(w.profile, p, seen_at, fresh):
                continue
            new += 1
            jobs, missed = ingest.plan_checkpoints(p.created_at, seen_at)
            self.state.schedule(p.id, w.profile, jobs, missed)
            if fresh and self.state.queue_hot(w.profile, p, seen_at):
                age = (seen_at - p.created_at).total_seconds()
                self.log(f"[hot] queued {w.profile} @{w.handle} {p.id}, {age:.0f}s old: {p.text[:80]!r}")
                self._hot_event.set()
        self.state.log_poll(self.clock(), w.profile, w.handle, ok=True, n_posts=len(posts), new_posts=new,
                            requests=self._thread_requests() - before, seconds=time.monotonic() - start)
        return new

    def _thread_requests(self) -> int:
        return self.client.thread_requests() if self.client else 0

    # ------------------------------------------------------------ hot jobs

    def run_hot_job(self, job) -> None:
        post = xpost_from_dict(json.loads(job["post"]))
        now = self.clock()
        age_min = (now - post.created_at).total_seconds() / 60
        cutoff = self.cfg.action.reply_max_age_minutes
        if age_min > cutoff:
            # Only after a long stop: the lane's own cutoff says a reply this late isn't seen.
            self.state.finish_hot(post.id, st.HOT_STALE, now, error=f"{age_min:.0f} min old at start; cutoff {cutoff:.0f}")
            self.log(f"[hot] {post.id} went stale in the queue ({age_min:.0f} min old)")
            return
        start = time.monotonic()
        try:
            record = hot.run_hot(self.llm, job["profile"], post, reader=self.reader, out_dir=self.cfg.hot_dir,
                                 cfg=self.cfg.action, seed=self.cfg.arm_seed,
                                 detected_at=datetime.fromisoformat(job["queued_at"]), clock=self.clock)
        except Exception as e:  # noqa: BLE001 - a failed draft is recorded; the next post still gets one
            seconds = round(time.monotonic() - start, 2)
            self.state.finish_hot(post.id, st.HOT_FAILED, self.clock(), seconds=seconds, error=f"{type(e).__name__}: {e}")
            self.log(f"[hot] {post.id} failed after {seconds}s: {type(e).__name__}: {e}\n"
                     + "".join(traceback.format_exception(e)[-3:]))
            return
        path = hot.hot_path(self.cfg.hot_dir, post.id)
        what = record["offer"] if record["offer"] != "abstain" else f"abstain ({record['abstain_reason']})"
        self.state.finish_hot(post.id, st.HOT_DONE, self.clock(), seconds=record["timings"]["total_s"], path=str(path),
                              outcome=what)
        self.log(f"[hot] {post.id} {record['profile']} arm={record['arm']} -> {what} in {record['timings']['total_s']}s"
                 f" (post {record['timings']['post_age_at_finish_s']:.0f}s old at finish)"
                 + (f": {record['draft']!r}" if record["draft"] else ""))

    def hot_worker(self, stop: threading.Event) -> None:
        while not stop.is_set():
            job = self.state.claim_hot(self.clock())
            if job is None:
                self._hot_event.wait(1.0)
                self._hot_event.clear()
                continue
            self.run_hot_job(job)

    # ------------------------------------------------------------ ingestion and outcomes

    def ingest_one(self) -> bool:
        """Run the earliest due ingestion job. False when none is due."""
        now = self.clock()
        job = self.state.due_ingest(now)
        if job is None:
            return False
        sid, cp, profile = job["status_id"], job["checkpoint"], job["profile"]
        try:
            root, sample = ingest.fetch_thread(self.reader, sid)
        except Exception as e:  # noqa: BLE001 - a failed read is retried, then recorded as failed
            error = f"{type(e).__name__}: {e}"
            new_state = self.state.retry_ingest(sid, cp, error, now + INGEST_RETRY, INGEST_ATTEMPTS)
            self.log(f"[ingest] {sid} +{cp}h read failed ({new_state}): {error}")
            return True
        coverage = sample.coverage
        if coverage is None:
            new_state = st.NO_REPLIES
        elif coverage < ingest.MIN_COVERAGE:
            new_state = st.LOW_COVERAGE
        else:
            root_row, reply_rows = ingest.rows_for(root, sample, profile, cp)
            ingest.append_thread(self.cfg.threads_dir, root_row, reply_rows)
            new_state = st.KEPT
        self.state.finish_ingest(sid, cp, new_state, self.clock(), coverage=coverage, sampled=len(sample.replies),
                                 reply_count=sample.reply_count)
        shown = "no replies" if coverage is None else f"coverage {coverage:.2f} ({len(sample.replies)}/{sample.reply_count})"
        self.log(f"[ingest] {sid} +{cp}h {new_state}: {shown}")
        return True

    def sync_outcomes(self) -> None:
        """`harness outcomes sync` for the account the watched profiles name. Errors are logged, never raised."""
        from harness.outcomes.handoff import resolve_handle
        from harness.outcomes.sync import sync

        try:
            handles = set()
            for p in self.cfg.profiles:
                with config.use_profile(p):
                    handles.add(resolve_handle()[0].lower())
            if len(handles) != 1:
                self.log(f"[outcomes] not synced: the profiles name {sorted(handles)}; run harness outcomes sync per account")
                return
            handle = handles.pop()
            r = sync(handle, config.OUT_DIR, hot_dir=self.cfg.hot_dir)
        except Exception as e:  # noqa: BLE001 - outcomes is a side job of the watcher
            self.log(f"[outcomes] sync failed: {type(e).__name__}: {e}")
            return
        self.log(f"[outcomes] @{handle}: {r.actions} actions, timeline {r.timeline}; {len(r.matched)} new matches, "
                 f"{len(r.ranks)} ranks" + (f"; errors: {r.errors}" if r.errors else ""))

    # ------------------------------------------------------------ the loop

    def claim_pid(self) -> None:
        if self.pid_path.exists():
            try:
                other = int(self.pid_path.read_text(encoding="utf-8").strip())
            except ValueError:
                other = None
            if other and other != os.getpid() and _alive(other):
                raise AlreadyRunning(f"a watcher is already running as pid {other} ({self.pid_path}); "
                                     "stop it with `python -m harness watch stop`")
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def release_pid(self) -> None:
        if self.pid_path.exists() and self.pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            self.pid_path.unlink()

    def run(self, stop: threading.Event | None = None) -> str:
        """Watch until `stop` is set, a signal arrives or `duration_s` passes. Returns why it stopped."""
        stop = stop or threading.Event()
        self.claim_pid()
        reason = "stopped"
        signals = {}
        if threading.current_thread() is threading.main_thread():
            def on_signal(signum, _frame):
                nonlocal reason
                reason = signal.Signals(signum).name
                stop.set()

            signals = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
        run_id = self.state.start_run(os.getpid(), self.clock())
        moved = self.state.recover(lambda sid: hot.hot_path(self.cfg.hot_dir, sid))
        order = self.schedule()
        self.log(f"[watch] pid {os.getpid()} watching {len(order)} handles in {', '.join(self.cfg.profiles)} every "
                 f"{self.cfg.interval_s:.0f}s ({len(order) / self.cfg.interval_s:.2f} timeline requests/s); "
                 f"recovered {moved}")
        worker = threading.Thread(target=self.hot_worker, args=(stop,), name="hot-path", daemon=True)
        worker.start()
        t0 = time.monotonic()
        due = {w: t0 + i * self.cfg.interval_s / len(order) for i, w in enumerate(order)}
        next_ingest = next_sync = t0
        next_beat = t0 + HEARTBEAT_S
        try:
            while not stop.is_set():
                now_m = time.monotonic()
                if self.cfg.duration_s is not None and now_m - t0 >= self.cfg.duration_s:
                    reason = f"duration {self.cfg.duration_s:.0f}s reached"
                    break
                w = min(due, key=due.get)
                if due[w] <= now_m:
                    self.poll(w)
                    # Behind schedule (a busy shared throttle), wait half an interval rather than polling again at once.
                    due[w] = max(due[w] + self.cfg.interval_s, now_m + self.cfg.interval_s / 2)
                    continue
                if now_m >= next_ingest and self.ingest_one():
                    next_ingest = now_m + self.cfg.ingest_every_s
                    continue
                if self.cfg.sync_every_s is not None and now_m >= next_sync:
                    self.sync_outcomes()
                    next_sync = now_m + self.cfg.sync_every_s
                    continue
                if now_m >= next_beat:
                    self._heartbeat(t0)
                    next_beat = now_m + HEARTBEAT_S
                stop.wait(max(0.05, min(due[w] - now_m, 1.0)))
        finally:
            stop.set()
            self._hot_event.set()
            worker.join(timeout=120)  # a hot draft takes under a minute; a stuck one is requeued next start
            for s, handler in signals.items():
                signal.signal(s, handler)
            self._heartbeat(t0)
            self.state.stop_run(run_id, self.clock(), self.requests() or 0, reason)
            self.log(f"[watch] stopped: {reason}")
            self.release_pid()
        return reason

    def _heartbeat(self, t0: float) -> None:
        elapsed = time.monotonic() - t0
        n = self.requests()
        rate = f", {n} requests ({n / elapsed:.2f}/s)" if n is not None and elapsed > 0 else ""
        s = self.state.summary()
        self.log(f"[watch] {elapsed / 60:.1f} min{rate}; posts seen {s['posts_seen']}, fresh {s['fresh_posts']}, "
                 f"hot {s['hot']}, ingest {s['ingest']}, poll errors {s['poll_errors']}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
