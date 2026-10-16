"""The watcher's last day, read from its state file (out/watch/state.db) without writing to it."""

import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class Day:
    since: datetime
    running: bool
    polls: int = 0
    poll_errors: int = 0
    handles_polled: int = 0
    posts_seen: int = 0
    fresh_posts: int = 0
    hot: list[dict] = field(default_factory=list)  # each hot target queued since `since`
    kept: dict[str, int] = field(default_factory=dict)  # checkpoint -> threads kept since `since`
    read: dict[str, int] = field(default_factory=dict)  # ingestion outcome -> count since `since`
    next_mature_read: str | None = None  # when the next pending +48h read opens


def _alive(pid_path: Path) -> bool:
    if not pid_path.is_file():
        return False
    pid = pid_path.read_text(encoding="utf-8").strip()
    if not pid.isdigit():
        return False
    out = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output=True, text=True)
    return out.returncode == 0 and "harness" in out.stdout and "watch" in out.stdout


def read(watch_dir: Path, now: datetime, hours: float = 24) -> Day | None:
    """None when the watcher has never run here."""
    db = Path(watch_dir) / "state.db"
    if not db.is_file():
        return None
    since = now - timedelta(hours=hours)
    stamp = since.astimezone(timezone.utc).isoformat(timespec="seconds")
    day = Day(since, _alive(Path(watch_dir) / "watch.pid"))
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        day.polls = q("SELECT COUNT(*) FROM polls WHERE at >= ?", stamp)
        day.poll_errors = q("SELECT COUNT(*) FROM polls WHERE ok = 0 AND at >= ?", stamp)
        day.handles_polled = q("SELECT COUNT(DISTINCT profile || '/' || handle) FROM polls WHERE ok = 1 AND at >= ?", stamp)
        day.posts_seen = q("SELECT COUNT(*) FROM posts WHERE first_seen_at >= ?", stamp)
        day.fresh_posts = q("SELECT COUNT(*) FROM posts WHERE hot = 1 AND first_seen_at >= ?", stamp)
        for r in conn.execute("SELECT h.*, p.handle, p.age_at_first_seen_s FROM hot h LEFT JOIN posts p "
                              "ON p.status_id = h.status_id WHERE h.queued_at >= ? ORDER BY h.queued_at", (stamp,)):
            day.hot.append({"status_id": r["status_id"], "profile": r["profile"], "handle": r["handle"],
                            "state": r["state"], "outcome": r["outcome"], "path": r["path"],
                            "age_s": r["age_at_first_seen_s"], "queued_at": r["queued_at"]})
        for r in conn.execute("SELECT checkpoint, state, COUNT(*) AS n FROM ingest WHERE done_at >= ? "
                              "GROUP BY checkpoint, state", (stamp,)):
            day.read[r["state"]] = day.read.get(r["state"], 0) + r["n"]
            if r["state"] == "kept":
                day.kept[f"+{r['checkpoint']}h"] = r["n"]
        day.next_mature_read = q("SELECT MIN(due_at) FROM ingest WHERE checkpoint = 48 AND state = 'pending'")
    finally:
        conn.close()
    return day


def hot_offer(path: str | None) -> dict:
    """The arm and what a hot file offered the operator, without the post's or draft's text."""
    if not path or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"arm": data.get("arm"), "offer": data.get("offer"), "abstain_reason": data.get("abstain_reason")}
