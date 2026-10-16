"""Build action/fixtures/x_threads.json: real X posts and their direct replies, read through
api.fxtwitter.com. Read-only, one request per 1.1 s.

Replier handles are dropped; each reply keeps its text, likes, the replier's follower count and
how many minutes after the root it landed. That is what the timing, targeting and lineup
experiments read.

    uv run python -m harness.minds.lab.fetch_x_threads IndianaFever WNBA CaitlinClark22
"""

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from harness import config

API = "https://api.fxtwitter.com"
UA = {"User-Agent": "media-harness-research/0.1 (read-only)"}
OUT = config.ROOT / "action" / "fixtures" / "x_threads.json"
LEADING_MENTIONS = re.compile(r"^(?:@\w+\s+)+")


def _get(path: str) -> dict:
    time.sleep(1.1)
    req = urllib.request.Request(API + path, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"fxtwitter {path} returned HTTP {e.code}") from e


def _ts(status: dict) -> int:
    return int(status["created_timestamp"])


def timeline(handle: str, count: int = 20) -> list[dict]:
    return _get(f"/2/profile/{handle}/statuses?count={count}").get("results") or []


def thread(root: dict) -> dict:
    data = _get(f"/2/conversation/{root['id']}")
    t0 = _ts(root)
    replies = []
    for r in data.get("replies") or []:
        # Only direct replies to the root; nested replies answer someone else.
        if (r.get("replying_to") or {}).get("status") != root["id"]:
            continue
        if r["author"]["screen_name"].lower() == root["author"]["screen_name"].lower():
            continue  # the author threading their own post
        replies.append(
            {
                "text": LEADING_MENTIONS.sub("", r.get("text", "")).strip(),
                "likes": r.get("likes", 0),
                "replies": r.get("replies", 0),
                "reposts": r.get("reposts", 0),
                "views": r.get("views"),
                "replier_followers": r["author"].get("followers"),
                "minutes_after_root": round((_ts(r) - t0) / 60, 1),
            }
        )
    replies.sort(key=lambda r: r["likes"], reverse=True)
    return {
        "id": root["id"],
        "url": root["url"],
        "author": root["author"]["screen_name"],
        "author_followers": root["author"].get("followers"),
        "text": root.get("text", ""),
        "created_at": datetime.fromtimestamp(t0, timezone.utc).isoformat(),
        "likes": root.get("likes", 0),
        "replies": root.get("replies", 0),
        "reposts": root.get("reposts", 0),
        "quotes": root.get("quotes", 0),
        "views": root.get("views"),
        "direct_replies": replies,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("handles", nargs="+")
    p.add_argument("--per-account", type=int, default=5, help="threads kept per account, most replies first")
    p.add_argument("--min-replies", type=int, default=8)
    args = p.parse_args()

    roots = []
    for h in args.handles:
        try:
            posts = [s for s in timeline(h) if not s.get("reposted_by") and s.get("replies", 0) >= args.min_replies]
        except RuntimeError as e:
            print(f"[fetch] {h}: {e}")
            continue
        posts.sort(key=lambda s: s.get("replies", 0), reverse=True)
        roots += posts[: args.per_account]
        print(f"[fetch] {h}: kept {min(len(posts), args.per_account)} roots")

    threads = []
    for root in roots:
        try:
            t = thread(root)
        except RuntimeError as e:
            print(f"[fetch] {root['url']}: {e}")
            continue
        if len(t["direct_replies"]) >= 5:
            threads.append(t)
            print(f"[fetch] {t['author']} {t['id']}: {len(t['direct_replies'])} direct replies")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "threads": threads},
                   indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[fetch] wrote {len(threads)} threads to {OUT}")


if __name__ == "__main__":
    main()
