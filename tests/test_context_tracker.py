from datetime import datetime, timedelta, timezone

from harness.context import snowflake
from harness.context.tracker import SCHEDULE, Tracker, next_due
from harness.context.xreader import XPost

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_schedule_density():
    assert SCHEDULE[:3] == [5.0, 10.0, 15.0]
    assert SCHEDULE[11] == 60.0
    assert SCHEDULE[12] == 75.0
    assert 1440.0 in SCHEDULE and 1500.0 in SCHEDULE
    assert SCHEDULE[-1] == 10080.0
    assert SCHEDULE == sorted(set(SCHEDULE))


def test_not_due_before_first_scheduled_age():
    assert not next_due(T0, None, at(4))


def test_due_once_first_age_passes():
    assert next_due(T0, None, at(5))


def test_not_due_again_until_next_age():
    assert not next_due(T0, at(6), at(9))
    assert next_due(T0, at(6), at(10))


def test_one_snapshot_covers_every_missed_age():
    # Tracker was down from minute 6 to minute 47; one poll at 47 catches up.
    assert next_due(T0, at(6), at(47))
    assert not next_due(T0, at(47), at(49))


def test_stops_after_seven_days():
    assert not next_due(T0, at(10079), at(10080 + 61))


class FakeReader:
    """Serves a post whose likes grow by 10 a minute; records every call."""

    def __init__(self, post_id, now_fn):
        self.post_id, self.now_fn, self.calls = post_id, now_fn, []

    def _post(self, pid):
        now = self.now_fn()
        minutes = (now - snowflake.to_datetime(pid)).total_seconds() / 60
        return XPost(id=pid, text="t", author_handle="acct", created_at=snowflake.to_datetime(pid),
                     fetched_at=now.isoformat(), likes=int(10 * minutes))

    def status(self, status_id):
        self.calls.append(("status", status_id))
        return self._post(status_id)

    def profile_statuses(self, handle, limit=20):
        self.calls.append(("profile", handle))
        return [self._post(self.post_id)]

    def conversation(self, status_id):
        return []


def test_watch_discovers_then_polls_on_schedule(tmp_path):
    pid = str(snowflake.floor_id(T0) | 1)
    clock = {"now": at(2)}
    reader = FakeReader(pid, lambda: clock["now"])
    tr = Tracker(reader, root=tmp_path)
    tr.add_watch(["acct"], every_min=15)

    assert tr.poll_once(now=clock["now"]) == {"discovered": 1, "polled": 0}  # age 2 min: not due yet
    clock["now"] = at(5)
    assert tr.poll_once(now=clock["now"])["polled"] == 1
    clock["now"] = at(7)
    assert tr.poll_once(now=clock["now"]) == {"discovered": 0, "polled": 0}  # watch not due, post not due
    clock["now"] = at(10)
    assert tr.poll_once(now=clock["now"])["polled"] == 1

    snaps = tr.snapshots()[pid]
    assert [s.likes for s in snaps] == [50, 100]
    assert sum(1 for c in reader.calls if c[0] == "profile") == 1


def test_watch_skips_posts_older_than_start_age(tmp_path):
    pid = str(snowflake.floor_id(T0) | 1)
    reader = FakeReader(pid, lambda: at(7 * 60))
    tr = Tracker(reader, root=tmp_path, max_start_age_h=6)
    tr.add_watch(["acct"])
    assert tr.poll_watch(at(7 * 60)) == 0


def test_add_accepts_url_and_decodes_creation_time(tmp_path):
    tr = Tracker(FakeReader("1", lambda: T0), root=tmp_path)
    pid = tr.add("https://x.com/supabase/status/2100570682593546310")
    assert pid == "2100570682593546310"
    assert tr.tracked()[pid]["created_at"].startswith("2026-09-17T13:00:52")
