"""Live smoke test for harness/sources/x_api.py.

    python scripts/smoke_x_api.py [handle]

Handle defaults to env X_HANDLE, else XDevelopers. Never prints the token.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from harness.sources import x_api  # noqa: E402


def short(text: str, n: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "..."


def run(label: str, fn):
    try:
        result = fn()
        print(f"[OK]   {label}")
        return result
    except x_api.XAPIError as e:
        print(f"[FAIL] {label}: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {label}: {type(e).__name__}: {e}")
    return None


def main() -> int:
    if not os.getenv("X_BEARER_TOKEN", "").strip():
        print("X_BEARER_TOKEN not set (env or .env); skipping live X API smoke test.")
        return 0
    handle = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("X_HANDLE", "XDevelopers")).lstrip("@")
    print(f"X API smoke test (base {x_api.BASE_URL}, handle @{handle})")
    print(f"query: {x_api.build_query()}")

    sigs = run("recent search  GET /2/tweets/search/recent", lambda: x_api.search_recent(max_results=5))
    for s in sigs or []:
        m = s.metrics
        print(f"       {m['reply_score']:6.2f} {s.author} ({m['author_followers']} fol) "
              f"L{m['likes']} R{m['reposts']} Re{m['replies']} | {short(s.text)}")

    user = run("user lookup    GET /2/users/by/username/:u", lambda: x_api.lookup_user(handle))
    if user:
        print(f"       id={user['id']} followers={user.get('public_metrics', {}).get('followers_count')}")

    posts = run("user timeline  GET /2/users/:id/tweets", lambda: x_api.fetch_own_posts(handle, max_results=5))
    for p in (posts or [])[:5]:
        m = p["metrics"]
        print(f"       {p['id']} L{m['likes']} R{m['reposts']} V{m['views']} | {short(p['text'])}")

    ids = [p["id"] for p in posts or []] or [s.id for s in sigs or []]
    if ids:
        got = run("post lookup    GET /2/tweets?ids=", lambda: x_api.fetch_metrics(ids[:5]))
        if got is not None:
            print(f"       {len(got)}/{len(ids[:5])} ids returned metrics")
    else:
        print("[SKIP] post lookup: no ids from earlier steps")

    plugin = run("source plugin  XAPISource.fetch", lambda: x_api.XAPISource().fetch(""))
    if plugin is not None:
        print(f"       {len(plugin)} signals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
