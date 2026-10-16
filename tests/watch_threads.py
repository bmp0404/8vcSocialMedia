"""A data/threads directory in the watcher's format, for the improve tests. Not collected: no test_ prefix."""

import gzip
import json
from datetime import datetime, timedelta

from harness.corpus.load import TWITTER_EPOCH_MS


def sid(t: datetime, n: int = 0) -> str:
    return str(((int(t.timestamp() * 1000) - TWITTER_EPOCH_MS) << 22) + n)


def row(status_id: str, author: str, read_at: datetime, likes=0, **extra) -> dict:
    return {"id": status_id, "author_handle": author, "text": extra.pop("text", f"words from {author} here"),
            "likes": likes, "reposts": 0, "replies": 0, "quotes": 0, "bookmarks": 0, "views": 0,
            "fetched_at": read_at.isoformat(), "author_followers": 10, "author_verified": False, **extra}


class Threads:
    """A data/threads directory in the watcher's format, built one thread at a time."""

    def __init__(self, path):
        self.path, self.roots, self.replies = path, [], []

    def thread(self, root_at: datetime, read_after: timedelta, likes: list[int], author="brand", profile="devtools",
               reply_delay=timedelta(minutes=5), root_likes=0, texts: list[str] | None = None,
               root_text: str | None = None):
        root = sid(root_at)
        read = root_at + read_after
        extra = {"text": root_text} if root_text else {}
        self.roots.append(row(root, author, read, likes=root_likes, watch_profile=profile, replies=len(likes), **extra))
        for i, n in enumerate(likes):
            rid = sid(root_at + reply_delay * (i + 1), i)
            text = {"text": f"@{author} {texts[i]}"} if texts else {}
            self.replies.append(row(rid, f"fan{i}", read, likes=n, replying_to=author, replying_to_status=root,
                                    reply_to_post=root, coverage=1.0, watch_profile=profile, **text))
        return root

    def write(self):
        self.path.mkdir(exist_ok=True)
        files = {"author_timelines.jsonl.gz": self.roots, "reply_samples.jsonl.gz": self.replies,
                 "x_metrics.jsonl.gz": []}
        for name, rows in files.items():
            with gzip.open(self.path / name, "wt", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
        for name, header in (("boost_flags.csv.gz", "post_id,is_thread_continuation,boost_suspect\n"),
                             ("timeline_posts.csv.gz", "post_id,is_thread_continuation\n")):
            with gzip.open(self.path / name, "wt", encoding="utf-8") as f:
                f.write(header)
        return self.path
