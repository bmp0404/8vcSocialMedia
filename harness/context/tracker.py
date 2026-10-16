"""Snapshot tracker: poll tracked posts on a schedule so engagement curves exist.

X keeps no history of a post's counts, so the only way to see a curve is to
record it while it happens. The tracker does two things each pass:

1. Watch: read the timelines of watched accounts and start tracking any post
   younger than `max_start_age_h`. Discovering a post within minutes of
   creation is what makes the early part of its curve visible.
2. Poll: for each tracked post whose next scheduled age has passed, fetch its
   counts once and append a `curves.Snapshot` line.

The schedule is dense early and sparse late (see `SCHEDULE`), because most
posts do most of their growth in the first hours and a step jump is only
detectable where polling is at least as fine as the 15-minute curve grid.

Storage, under data/context/tracker/ (gitignored, append-only JSONL):
    tracked.json     {post_id: {"author", "created_at", "added_at", "reason"}}
    watch.json       {"handles": [...], "every_min": 15}
    snapshots.jsonl  one Snapshot per line
    replies.jsonl    reply samples, for amplifier attribution

Any `XReader` works; `FxTwitterReader` is the default until an official reader exists.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.context import snowflake
from harness.context.backfill import CONTEXT_DIR, read_jsonl
from harness.context.curves import Amplifier, Snapshot, detect
from harness.context.http import FetchError
from harness.context.xreader import XReader

TRACKER_DIR = CONTEXT_DIR / "tracker"


def _schedule() -> list[float]:
    """Ages in minutes at which a post is due: every 5 min to 1 h, 15 min to 24 h, hourly to 72 h, 6-hourly to 7 d."""
    ages = [5.0 * k for k in range(1, 13)]
    ages += [60.0 + 15 * k for k in range(1, 93)]
    ages += [1440.0 + 60 * k for k in range(1, 49)]
    ages += [4320.0 + 360 * k for k in range(1, 17)]
    return ages


SCHEDULE = _schedule()
AMPLIFIER_MIN_FOLLOWERS = 100_000


def next_due(created_at: datetime, last_observed: datetime | None, now: datetime) -> bool:
    """True when some scheduled age has passed that no snapshot has covered yet."""
    age = (now - created_at).total_seconds() / 60
    if age > SCHEDULE[-1] + 60:
        return False
    passed = [a for a in SCHEDULE if a <= age]
    if not passed:
        return False
    if last_observed is None:
        return True
    last_age = (last_observed - created_at).total_seconds() / 60
    return last_age < passed[-1]


class Tracker:
    def __init__(self, reader: XReader, root: Path = TRACKER_DIR, max_start_age_h: float = 6.0):
        self.reader = reader
        self.root = root
        self.max_start_age_h = max_start_age_h
        root.mkdir(parents=True, exist_ok=True)
        self.tracked_path = root / "tracked.json"
        self.watch_path = root / "watch.json"
        self.snap_path = root / "snapshots.jsonl"
        self.reply_path = root / "replies.jsonl"

    # ----------------------------------------------------------------- state
    def tracked(self) -> dict:
        return json.loads(self.tracked_path.read_text(encoding="utf-8")) if self.tracked_path.exists() else {}

    def watch(self) -> dict:
        if self.watch_path.exists():
            return json.loads(self.watch_path.read_text(encoding="utf-8"))
        return {"handles": [], "every_min": 15, "last_polled": {}}

    def _save(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)

    def add(self, url_or_id: str, reason: str = "manual", author: str = "") -> str:
        post_id = str(snowflake.status_id_from_url(url_or_id)) if "/" in url_or_id else str(int(url_or_id))
        t = self.tracked()
        if post_id not in t:
            t[post_id] = {
                "author": author,
                "created_at": snowflake.to_datetime(post_id).isoformat(),
                "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reason": reason,
            }
            self._save(self.tracked_path, t)
        return post_id

    def add_watch(self, handles: list[str], every_min: int | None = None) -> None:
        w = self.watch()
        w["handles"] = sorted({*w["handles"], *(h.lstrip("@") for h in handles)})
        if every_min:
            w["every_min"] = every_min
        self._save(self.watch_path, w)

    def snapshots(self) -> dict[str, list[Snapshot]]:
        out = defaultdict(list)
        for d in read_jsonl(self.snap_path):
            out[d["post_id"]].append(Snapshot.from_dict(d))
        return out

    # ----------------------------------------------------------------- passes
    def poll_watch(self, now: datetime) -> int:
        w = self.watch()
        added = 0
        last = w.setdefault("last_polled", {})
        for h in w["handles"]:
            prev = last.get(h)
            if prev and now - datetime.fromisoformat(prev) < timedelta(minutes=w["every_min"]):
                continue
            try:
                posts = self.reader.profile_statuses(h, limit=20)
            except FetchError as e:
                print(f"[tracker] watch @{h} failed: {e}")
                continue
            last[h] = now.isoformat(timespec="seconds")
            for p in posts:
                if p.is_repost or now - p.created_at > timedelta(hours=self.max_start_age_h):
                    continue
                if p.id not in self.tracked():
                    self.add(p.id, reason=f"watch:@{h}", author=p.author_handle)
                    added += 1
        self._save(self.watch_path, w)
        return added

    def poll_posts(self, now: datetime, with_replies: bool = False) -> int:
        snaps = self.snapshots()
        polled = 0
        for post_id, meta in self.tracked().items():
            created = datetime.fromisoformat(meta["created_at"])
            last = max((s.observed_at for s in snaps.get(post_id, [])), default=None)
            if not next_due(created, last, now):
                continue
            try:
                post = self.reader.status(post_id)
            except FetchError as e:
                print(f"[tracker] {post_id} failed: {e}")
                continue
            with self.snap_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(Snapshot.from_xpost(post).to_dict()) + "\n")
            if with_replies:
                self._sample_replies(post_id)
            polled += 1
        return polled

    def _sample_replies(self, post_id: str) -> None:
        try:
            replies = self.reader.conversation(post_id)
        except FetchError as e:
            print(f"[tracker] replies for {post_id} failed: {e}")
            return
        with self.reply_path.open("a", encoding="utf-8") as f:
            for r in replies:
                f.write(json.dumps({"reply_to_post": post_id, **r.to_dict()}) + "\n")

    def poll_once(self, now: datetime | None = None, with_replies: bool = False) -> dict:
        now = now or datetime.now(timezone.utc)
        return {"discovered": self.poll_watch(now), "polled": self.poll_posts(now, with_replies)}

    def loop(self, interval_s: float = 60.0, with_replies: bool = False, max_passes: int | None = None) -> None:
        n = 0
        while max_passes is None or n < max_passes:
            print(f"[tracker] {datetime.now(timezone.utc):%H:%M:%S} {self.poll_once(with_replies=with_replies)}")
            n += 1
            time.sleep(interval_s)

    # ----------------------------------------------------------------- reading
    def amplifiers(self, post_id: str) -> list[Amplifier] | None:
        """Large accounts seen replying, from reply samples. None when no sample was taken."""
        rows = [r for r in read_jsonl(self.reply_path) if r["reply_to_post"] == post_id]
        if not rows:
            return None
        seen = {}
        for r in rows:
            if (r.get("author_followers") or 0) >= AMPLIFIER_MIN_FOLLOWERS and r["id"] not in seen:
                seen[r["id"]] = Amplifier(
                    at=datetime.fromisoformat(r["created_at"]),
                    handle=r["author_handle"],
                    followers=r["author_followers"],
                    kind="reply",
                )
        return list(seen.values())

    def report(self, post_id: str, metric: str = "engagement") -> dict:
        snaps = self.snapshots().get(post_id, [])
        created = snowflake.to_datetime(post_id)
        inf = detect(created, snaps, metric=metric, amplifiers=self.amplifiers(post_id))
        return {"post_id": post_id, "n_snapshots": len(snaps), **inf.to_dict()}
