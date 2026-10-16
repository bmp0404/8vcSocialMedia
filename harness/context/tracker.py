"""Snapshot tracker: poll tracked posts on a schedule so engagement curves exist.

X keeps no history of a post's counts, so the only way to see a curve is to
record it while it happens. The tracker does two things each pass:

1. Watch: read the timelines of watched accounts and start tracking any post
   younger than `max_start_age_h`. Discovering a post within minutes of
   creation is what makes the early part of its curve visible.
2. Poll: for each tracked post whose next scheduled age has passed, fetch its
   counts once and append a `curves.Snapshot` line.

The default schedule is log-spaced (5 min to 7 days, 10 polls per post,
`TrackerParams.log_schedule`), because early speed predicts final size. Dense
mode (`TrackerParams(dense=True)`) polls every 5-15 minutes for the first day;
use it for the brand's own posts, because a step jump is only detectable where
polling is at least as fine as the 15-minute curve grid.

Storage, under data/context/tracker/ (gitignored, append-only JSONL):
    tracked.json     {post_id: {"author", "created_at", "added_at", "reason"}}
    watch.json       {"handles": [...], "every_min": 15}
    snapshots.jsonl  one Snapshot per line
    replies.jsonl    reply samples (both sort orders, with coverage)
    reposters.jsonl  the largest reposters at each poll; a big account's first
                     appearance brackets when it reposted, which is the only
                     repost timing fxtwitter exposes

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
from harness.context.settings import TrackerParams
from harness.context.xreader import XReader

TRACKER_DIR = CONTEXT_DIR / "tracker"


SCHEDULE = TrackerParams().schedule()


def next_due(created_at: datetime, last_observed: datetime | None, now: datetime, schedule: list[float] = SCHEDULE) -> bool:
    """True when some scheduled age has passed that no snapshot has covered yet."""
    age = (now - created_at).total_seconds() / 60
    if age > schedule[-1] + 60:
        return False
    passed = [a for a in schedule if a <= age]
    if not passed:
        return False
    if last_observed is None:
        return True
    # One second of slack: fetch times and creation times are rounded differently.
    last_age = (last_observed - created_at).total_seconds() / 60 + 1 / 60
    return last_age < passed[-1]


class Tracker:
    def __init__(self, reader: XReader, root: Path = TRACKER_DIR, params: TrackerParams = TrackerParams()):
        self.reader = reader
        self.root = root
        self.params = params
        self.schedule = params.schedule()
        root.mkdir(parents=True, exist_ok=True)
        self.tracked_path = root / "tracked.json"
        self.watch_path = root / "watch.json"
        self.snap_path = root / "snapshots.jsonl"
        self.reply_path = root / "replies.jsonl"
        self.repost_path = root / "reposters.jsonl"

    # ----------------------------------------------------------------- state
    def tracked(self) -> dict:
        return json.loads(self.tracked_path.read_text(encoding="utf-8")) if self.tracked_path.exists() else {}

    def watch(self) -> dict:
        if self.watch_path.exists():
            return json.loads(self.watch_path.read_text(encoding="utf-8"))
        return {"handles": [], "every_min": self.params.watch_every_min, "last_polled": {}}

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
                if p.is_repost or now - p.created_at > timedelta(hours=self.params.max_start_age_h):
                    continue
                if p.id not in self.tracked():
                    self.add(p.id, reason=f"watch:@{h}", author=p.author_handle)
                    added += 1
        self._save(self.watch_path, w)
        return added

    def poll_posts(self, now: datetime, with_replies: bool = False, with_reposters: bool = False) -> int:
        snaps = self.snapshots()
        polled = 0
        for post_id, meta in self.tracked().items():
            created = datetime.fromisoformat(meta["created_at"])
            last = max((s.observed_at for s in snaps.get(post_id, [])), default=None)
            if not next_due(created, last, now, self.schedule):
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
            if with_reposters and post.reposts:
                self._sample_reposters(post_id, post.fetched_at)
            polled += 1
        return polled

    def _sample_replies(self, post_id: str) -> None:
        try:
            sample = self.reader.replies(post_id)
        except FetchError as e:
            print(f"[tracker] replies for {post_id} failed: {e}")
            return
        with self.reply_path.open("a", encoding="utf-8") as f:
            for r in sample.replies:
                f.write(json.dumps({"reply_to_post": post_id, "coverage": sample.coverage, **r.to_dict()}) + "\n")

    def _sample_reposters(self, post_id: str, observed_at: str) -> None:
        try:
            users = self.reader.reposters(post_id, max_pages=1)
        except FetchError as e:
            print(f"[tracker] reposters for {post_id} failed: {e}")
            return
        with self.repost_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"post_id": post_id, "observed_at": observed_at,
                                "users": [u.to_dict() for u in users]}) + "\n")

    def poll_once(self, now: datetime | None = None, with_replies: bool = False, with_reposters: bool = False) -> dict:
        now = now or datetime.now(timezone.utc)
        return {"discovered": self.poll_watch(now), "polled": self.poll_posts(now, with_replies, with_reposters)}

    def loop(
        self,
        interval_s: float | None = None,
        with_replies: bool = False,
        max_passes: int | None = None,
        with_reposters: bool = False,
    ) -> None:
        interval_s = interval_s or self.params.loop_interval_s
        n = 0
        while max_passes is None or n < max_passes:
            result = self.poll_once(with_replies=with_replies, with_reposters=with_reposters)
            print(f"[tracker] {datetime.now(timezone.utc):%H:%M:%S} {result}")
            n += 1
            time.sleep(interval_s)

    # ----------------------------------------------------------------- reading
    def amplifiers(self, post_id: str) -> list[Amplifier] | None:
        """Large accounts that reposted (first poll they appeared in) or replied (reply time).

        None unless reposters were sampled: a quote or repost by a big account
        is the usual cause of a step, and replies alone cannot rule it out, so
        an empty list would wrongly flag every step as unexplained.
        """
        snaps = sorted(
            (r for r in read_jsonl(self.repost_path) if r["post_id"] == post_id), key=lambda r: r["observed_at"]
        )
        if not snaps:
            return None
        big = self.params.amplifier_min_followers
        found: dict[str, Amplifier] = {}
        for snap in snaps:
            for u in snap["users"]:
                if (u.get("followers") or 0) >= big and u["handle"] not in found:
                    found[u["handle"]] = Amplifier(
                        at=datetime.fromisoformat(snap["observed_at"]), handle=u["handle"],
                        followers=u["followers"], kind="repost",
                    )
        for r in read_jsonl(self.reply_path):
            if r["reply_to_post"] == post_id and (r.get("author_followers") or 0) >= big:
                found.setdefault(r["author_handle"], Amplifier(
                    at=datetime.fromisoformat(r["created_at"]), handle=r["author_handle"],
                    followers=r["author_followers"], kind="reply",
                ))
        return list(found.values())

    def early_metrics(self, post_id: str, snaps: list[Snapshot], followers: int | None) -> dict:
        """Minutes to the k-th reply, and the like-per-view rate X's cold-start ranking sees."""
        created = snowflake.to_datetime(post_id)
        out: dict = {}
        replies = {r["id"]: r for r in read_jsonl(self.reply_path) if r["reply_to_post"] == post_id and "id" in r}
        times = sorted((datetime.fromisoformat(r["created_at"]) - created).total_seconds() / 60 for r in replies.values())
        coverage = [r.get("coverage") for r in replies.values() if r.get("coverage") is not None]
        out["reply_coverage"] = max(coverage) if coverage else None
        for k in self.params.reply_ks:
            out[f"t_to_reply_{k}_min"] = round(times[k - 1], 1) if len(times) >= k else None
        early = [
            s for s in snaps
            if s.views and (s.observed_at - created).total_seconds() / 60 <= self.params.cold_start_window_min
        ]
        last = max(early, key=lambda s: s.observed_at) if early else None
        out["early_like_rate"] = round(last.likes / last.views, 5) if last else None
        out["cold_start_eligible"] = followers is not None and followers <= self.params.cold_start_max_followers
        return out

    def report(self, post_id: str, metric: str = "engagement") -> dict:
        snaps = self.snapshots().get(post_id, [])
        created = snowflake.to_datetime(post_id)
        inf = detect(created, snaps, metric=metric, amplifiers=self.amplifiers(post_id))
        followers = None
        if snaps:
            try:
                followers = self.reader.status(post_id).author_followers
            except FetchError:
                followers = None
        return {"post_id": post_id, "n_snapshots": len(snaps), **self.early_metrics(post_id, snaps, followers), **inf.to_dict()}
