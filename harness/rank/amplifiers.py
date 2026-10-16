"""Mid-size accounts that amplify the dev-tool space, and whether watching them would surface more open,
fresh threads than the current watch lists.

`build` ranks every reposter of a dataset post with 1,000 to 100,000 followers by how many other brands'
posts it reposted (`data/context/reposters.jsonl`; reposts have no timestamps, and the data has no quote
lists). `--pool repliers` ranks the accounts in that band that replied under the most brands' threads
(`data/context/reply_samples.jsonl`) instead, as a second pool. For the top `--top` it reads, through
fxtwitter's shared throttle, one timeline page and the replies under up to four of its recent original
posts, and writes `data/context/amplifiers.jsonl` or `amplifiers-repliers.jsonl`. Those files name
private people, so they stay local and gitignored, like their sources.

`replay` replays the window's original posts through the watcher's polling: one profile read per handle
in turn at POLL_RATE, a post counting as fresh when the first read after it comes within HOT_AGE. It
compares the watch lists with and without the candidates and writes only counts to
`eval/experiments/reports/amplifier-replay.json`. A thread is open when two or more of its top three
replies came from accounts under 1,000 followers. Results are in `eval/experiments/closed.md`.

    uv run python -m harness.rank.amplifiers build --top 75
    uv run python -m harness.rank.amplifiers build --top 25 --pool repliers
    uv run python -m harness.rank.amplifiers replay
"""

import argparse
import json
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import config
from harness.rank import target

CONTEXT = target.CONTEXT
REPOSTERS = CONTEXT / "reposters.jsonl"
TIMELINES = CONTEXT / "author_timelines.jsonl"
POOLS = {"reposters": CONTEXT / "amplifiers.jsonl", "repliers": CONTEXT / "amplifiers-repliers.jsonl"}
REPORT = config.ROOT / "eval" / "experiments" / "reports" / "amplifier-replay.json"
MID = (1_000, 100_000)
WINDOW = (datetime(2026, 10, 2, tzinfo=timezone.utc), datetime(2026, 10, 16, tzinfo=timezone.utc))
# The watcher's read budget and freshness bar, from B3's counsel and harness/watcher on sam/watch.
POLL_RATE = 0.2  # profile reads per second, shared by every watched handle
HOT_AGE = timedelta(minutes=10)
OPEN_MIN_SMALL = 2  # of the top 3
# A post with fewer replies has no top three worth reaching: nobody is reading the thread. Same floor as
# a scored thread in harness/rank/target.py.
MIN_REPLIES = target.TOP
ROOTS_PER_CANDIDATE = 4
# Today's lists: mission/profiles/devtools/observe.toml on sam/platform, and the one sam/watch chose.
PLATFORM_LIST = ("cursor_ai", "supabase", "zeddotdev", "cognition", "cline", "bunjavascript", "tldraw", "ollama",
                 "lovable", "cloudflaredev", "coderabbitai", "greptile", "qodoai", "sourcegraph", "vercel")
WATCH_LIST = ("supabase", "zeddotdev", "resend", "expo", "netlify", "workos", "vllm_project", "cloudflaredev", "cline",
              "coderabbitai", "posthog", "factoryai", "neondatabase", "qodoai", "lovable", "browserbase", "llama_index",
              "vercel", "warpdotdev", "bunjavascript", "cursor_ai", "kirodotdev", "replit", "ollama")
KEYWORDS = ("code review", "coding agent", "devtools", "open source", "pull request", "launch")  # devtools observe.toml


def _read(path: Path) -> list[dict]:
    return target._read_jsonl(path)


def _time(value) -> datetime:
    return value if isinstance(value, datetime) else target._time(value)


# ---------------------------------------------------------------- ranking


def _rank(acts: list[tuple[str, int, str]], mid: tuple[int, int]) -> list[dict]:
    """acts: (handle, followers, brand) per repost or reply. Ranked by distinct other brands, then count, then handle."""
    brands: dict[str, set] = defaultdict(set)
    count: dict[str, int] = defaultdict(int)
    followers: dict[str, int] = {}
    for handle, f, brand in acts:
        h = handle.lower()
        if not mid[0] <= f <= mid[1] or h == brand:  # a brand acting on its own post amplifies nobody else
            continue
        brands[h].add(brand)
        count[h] += 1
        followers[h] = f
    rows = [{"handle": h, "followers": followers[h], "brands": len(brands[h]), "count": count[h]} for h in brands]
    return sorted(rows, key=lambda r: (-r["brands"], -r["count"], r["handle"]))


def rank_reposters(reposts: list[dict], brand_of: dict[str, str], mid: tuple[int, int] = MID) -> list[dict]:
    """[{handle, followers, brands, count}] for reposters inside `mid` followers, by how many other brands'
    posts they reposted, then how many posts, then handle."""
    return _rank([(r["handle"], r.get("followers") or 0, brand_of[r["post_id"]]) for r in reposts
                  if "handle" in r and r["post_id"] in brand_of], mid)


def rank_repliers(replies: list[dict], brand_of: dict[str, str], mid: tuple[int, int] = MID) -> list[dict]:
    """The same ranking over the accounts that replied under the brands' threads, each reply counted once."""
    seen, acts = set(), []
    for r in replies:
        if "id" in r and r["id"] not in seen and str(r["reply_to_post"]) in brand_of:
            seen.add(r["id"])
            acts.append((r["author_handle"], r.get("author_followers") or 0, brand_of[str(r["reply_to_post"])]))
    return _rank(acts, mid)


def is_original(post: dict, handle: str) -> bool:
    """The account's own post that starts a thread: not a repost and not a reply."""
    return (not post.get("is_repost") and str(post.get("author_handle", "")).lower() == handle.lower()
            and not post.get("replying_to") and not post.get("replying_to_status"))


def on_topic(post: dict, keywords: tuple[str, ...] = KEYWORDS) -> bool:
    """What the watcher counts from a broad account: English, with one of the profile's keywords."""
    text = (post.get("text") or "").lower()
    return post.get("lang") == "en" and any(k in text for k in keywords)


# ---------------------------------------------------------------- fetching (build)


def thread_record(root, sample) -> dict | None:
    """Counts only (no reply text or handles) for one candidate thread with 3+ direct replies by others."""
    t = target.Thread(root.id, root.author_handle.lower(), root.created_at, root.author_followers or 0, tuple(
        target.Reply(r.id, r.author_handle, r.author_followers or 0, r.likes, r.created_at) for r in sample.replies
        if r.replying_to_status in (None, root.id) and r.author_handle.lower() != root.author_handle.lower()
        and target.LEADING_MENTIONS.sub("", r.text).strip()))
    small = target.small_in_top(t)
    if small is None:
        return None
    return {"root_id": root.id, "created_at": root.created_at.isoformat(), "small_in_top3": small,
            "top3_minutes": round(statistics.median(target.top_minutes(t)), 1), "coverage": sample.coverage}


def _post(p) -> dict:
    return {"id": p.id, "created_at": p.created_at.isoformat(), "author_handle": p.author_handle, "is_repost": p.is_repost,
            "replying_to": p.replying_to, "replying_to_status": p.replying_to_status, "lang": p.lang, "text": p.text,
            "replies": p.replies}


def fetch_candidate(reader, row: dict, now: datetime) -> dict:
    """One timeline page, and the replies under up to ROOTS_PER_CANDIDATE original posts that are a day old
    and have 3 or more replies (newest first)."""
    posts = reader.profile_statuses(row["handle"], limit=20)
    roots = [p for p in posts if is_original(_post(p), row["handle"]) and p.replies >= 3
             and now - p.created_at >= timedelta(days=1)]
    threads = [rec for p in roots[:ROOTS_PER_CANDIDATE] if (rec := thread_record(p, reader.replies(p.id)))]
    return {**row, "fetched_at": now.isoformat(), "timeline": [_post(p) for p in posts], "threads": threads}


def build(top: int, pool: str = "reposters", reader=None, now: datetime | None = None) -> list[dict]:
    """Rank the pool, fetch its top `top`, and write POOLS[pool]. Reuses rows already fetched."""
    now = now or datetime.now(timezone.utc)
    roots = {r["id"]: r["author_handle"].lower() for r in _read(target.ROOTS) if "id" in r and "error" not in r}
    ranked = rank_reposters(_read(REPOSTERS), roots) if pool == "reposters" else rank_repliers(_read(target.REPLY_SAMPLES), roots)
    watched = set(PLATFORM_LIST) | set(WATCH_LIST)
    ranked = [r for r in ranked if r["handle"] not in watched]
    path = POOLS[pool]
    done = {r["handle"]: r for r in (_read(path) if path.exists() else [])}
    if reader is None:
        from harness.context.http import PoliteClient
        from harness.context.xreader import FxTwitterReader

        # Twice the shared one-second floor, so a running watcher keeps most of fxtwitter's budget.
        reader = FxTwitterReader(PoliteClient(min_interval_s=2.0), max_age_s=None)
    out = []
    for row in ranked[:top]:
        if row["handle"] not in done:
            try:
                done[row["handle"]] = fetch_candidate(reader, row, now)
            except RuntimeError as e:  # xreader's FetchError: record why, keep going
                done[row["handle"]] = {**row, "fetched_at": now.isoformat(), "error": str(e)[:200]}
            print(f"[amplifiers] {pool} {len(out) + 1}/{top}: {row['brands']} brands")
        out.append(done[row["handle"]])
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    return out


# ---------------------------------------------------------------- replay


def detection_delays(posts: dict[str, list[datetime]], handles: list[str], start: datetime,
                     rate: float = POLL_RATE) -> dict[str, list[float]]:
    """Seconds from each post to the first read of its handle, with handles read in turn at `rate` reads per
    second from `start`: handle i is read at start + i/rate, then every len(handles)/rate seconds."""
    period, out = len(handles) / rate, {}
    for i, h in enumerate(handles):
        first = start + timedelta(seconds=i / rate)
        delays = []
        for t in posts.get(h, []):
            waited = max((t - first).total_seconds(), 0.0)
            k = -(-waited // period)  # reads needed after the first one to reach t
            delays.append((first + timedelta(seconds=k * period) - t).total_seconds())
        out[h] = delays
    return out


@dataclass(frozen=True)
class Account:
    handle: str
    posts: tuple[datetime, ...]  # original posts the watcher would act on, with MIN_REPLIES+ replies, in the window
    covered_days: float  # how much of the window the timeline covers
    threads: tuple[int, ...]  # small accounts in the top 3, per scored thread
    quiet: int = 0  # original posts in the window left out for drawing fewer than MIN_REPLIES replies
    fallback: tuple[int, ...] = ()  # thread counts to use when the account has no scored thread of its own


def open_rate(threads: tuple[int, ...] | list[int]) -> float | None:
    return sum(k >= OPEN_MIN_SMALL for k in threads) / len(threads) if threads else None


def yield_per_day(accounts: list[Account], rate: float = POLL_RATE, window: tuple[datetime, datetime] = WINDOW,
                  resample=None) -> dict:
    """Expected open threads per day the watcher would catch fresh from `accounts`, read in turn: fresh posts
    per covered day times the account's open rate, from its own scored threads or else its `fallback`.
    `resample` maps a list of thread counts to a resampled one, for the bootstrap."""
    pick = resample or (lambda xs: xs)
    delays = detection_delays({a.handle: list(a.posts) for a in accounts}, [a.handle for a in accounts], window[0], rate)
    total = fresh_total = posts_total = quiet_total = 0.0
    for a in accounts:
        if not a.covered_days:
            continue
        fresh = sum(d < HOT_AGE.total_seconds() for d in delays[a.handle])
        r = open_rate(pick(list(a.threads or a.fallback)))
        total += fresh / a.covered_days * (r or 0.0)
        fresh_total += fresh / a.covered_days
        posts_total += len(a.posts) / a.covered_days
        quiet_total += a.quiet / a.covered_days
    return {"handles": len(accounts), "read_every_s": round(len(accounts) / rate, 1),
            "quiet_posts_per_day": round(quiet_total, 2), "posts_per_day": round(posts_total, 2),
            "fresh_per_day": round(fresh_total, 2), "open_fresh_per_day": round(total, 2)}


def _split(posts: list[dict], handle: str, start: datetime, end: datetime, keep) -> tuple[tuple[datetime, ...], int]:
    """(times of the handle's kept originals in [start, end) with MIN_REPLIES+ replies, how many had fewer)."""
    mine = [p for p in posts if is_original(p, handle) and start <= _time(p["created_at"]) < end and keep(p)]
    busy = tuple(sorted(_time(p["created_at"]) for p in mine if (p.get("replies") or 0) >= MIN_REPLIES))
    return busy, len(mine) - len(busy)


def brand_accounts(handles, threads_by_author: dict[str, list[int]], timelines: list[dict],
                   window: tuple[datetime, datetime] = WINDOW) -> list[Account]:
    out = []
    for h in handles:
        mine = [p for p in timelines if p.get("timeline_of", "").lower() == h and "author_handle" in p]
        posts, quiet = _split(mine, h, window[0], window[1], lambda p: True)
        covered = (window[1] - max(window[0], min((_time(p["created_at"]) for p in mine), default=window[1]))).total_seconds() / 86400
        out.append(Account(h, posts, covered, tuple(threads_by_author.get(h, [])), quiet))
    return out


def candidate_accounts(rows: list[dict], window: tuple[datetime, datetime] = WINDOW, topical: bool = True) -> list[Account]:
    out = []
    for r in rows:
        if "error" in r:
            continue
        fetched = _time(r["fetched_at"])
        times = [_time(p["created_at"]) for p in r["timeline"]]
        end = min(window[1], fetched)
        start = max(window[0], min(times, default=end))
        posts, quiet = _split(r["timeline"], r["handle"], start, end, lambda p: on_topic(p) or not topical)
        out.append(Account(r["handle"], posts, max((end - start).total_seconds() / 86400, 0.0),
                           tuple(t["small_in_top3"] for t in r["threads"]), quiet))
    return out


def _ci(values: list[float]) -> list[float]:
    values = sorted(values)
    return [round(values[int(0.025 * (len(values) - 1))], 2), round(values[int(0.975 * (len(values) - 1))], 2)]


def _pool_report(rows: list[dict], lists: dict[str, list[Account]], pooled: tuple[int, ...], rng, n_boot: int) -> dict:
    """Yields with and without one pool's candidates, and their per-handle yield against each list. The
    bootstrap resamples accounts within each group, then each account's scored threads."""
    topical, everything = candidate_accounts(rows), candidate_accounts(rows, topical=False)
    # A candidate with busy posts but no scored thread (its posts were under a day old) takes the rate of the
    # pool's own scored threads, or the brands' when the pool has none.
    own = tuple(k for a in everything for k in a.threads) or pooled
    topical, everything = ([replace(a, fallback=own) for a in accs] for accs in (topical, everything))
    yields = {"candidates_on_topic": yield_per_day(topical), "candidates_any_post": yield_per_day(everything)}
    resample = lambda xs: [rng.choice(xs) for _ in xs] if xs else xs  # noqa: E731
    for name, accs in lists.items():
        base = yield_per_day(accs)["open_fresh_per_day"] / len(accs)
        for label, cands in (("on_topic", topical), ("any_post", everything)):
            if not cands:
                continue
            yields[f"{name}+candidates_{label}"] = yield_per_day(accs + cands)
            diffs = []
            for _ in range(n_boot):
                cs, bs = rng.choices(cands, k=len(cands)), rng.choices(accs, k=len(accs))
                diffs.append(yield_per_day(cs, resample=resample)["open_fresh_per_day"] / len(cs)
                             - yield_per_day(bs, resample=resample)["open_fresh_per_day"] / len(bs))
            point = yields[f"candidates_{label}"]["open_fresh_per_day"] / len(cands) - base
            yields[f"per_handle_candidates_{label}_minus_{name}"] = {"diff": round(point, 3), "ci95": _ci(diffs)}
    scored = [a for a in everything if a.threads]
    return {
        "fetched": len(rows), "errors": sum("error" in r for r in rows),
        "timelines_with_no_original_post": sum(not any(is_original(p, r["handle"]) for p in r["timeline"])
                                               for r in rows if "error" not in r),
        "timeline_posts_read": sum(len(r["timeline"]) for r in rows if "error" not in r),
        "with_scored_threads": len(scored), "scored_threads": sum(len(a.threads) for a in scored),
        "open_rate_of_scored_threads": open_rate([k for a in scored for k in a.threads]),
        "busy_on_topic_posts_in_window": sum(len(a.posts) for a in topical),
        "busy_posts_in_window": sum(len(a.posts) for a in everything),
        "quiet_posts_in_window": sum(a.quiet for a in everything), "yields": yields,
    }


def replay(n_boot: int = 2000, seed: int = 0) -> dict:
    threads = target.load_threads()
    by_author: dict[str, list[int]] = defaultdict(list)
    for t in threads:
        k = target.small_in_top(t)
        if k is not None:
            by_author[t.author].append(k)
    pooled = tuple(k for ks in by_author.values() for k in ks)
    timelines = _read(TIMELINES)
    lists = {name: [replace(a, fallback=pooled) for a in brand_accounts(handles, by_author, timelines)]
             for name, handles in (("platform", PLATFORM_LIST), ("watch", WATCH_LIST))}
    rng = random.Random(seed)
    return {
        "question": "Would watching mid-size amplifiers have surfaced more open, fresh threads than the current lists?",
        "window": [WINDOW[0].date().isoformat(), WINDOW[1].date().isoformat()], "poll_rate_per_s": POLL_RATE,
        "hot_age_minutes": HOT_AGE.total_seconds() / 60, "open_rule": f"{OPEN_MIN_SMALL}+ of the top 3 under 1,000 followers",
        "brand_open_rate_pooled": round(open_rate(pooled), 3),
        "lists": {name: yield_per_day(accs) for name, accs in lists.items()},
        "pools": {pool: _pool_report(_read(path), lists, pooled, rng, n_boot) for pool, path in POOLS.items() if path.exists()},
        "bootstrap": {"draws": n_boot, "seed": seed, "resamples": "accounts within each group, then their scored threads"},
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m harness.rank.amplifiers")
    p.add_argument("command", choices=["build", "replay"])
    p.add_argument("--top", type=int, default=75)
    p.add_argument("--pool", choices=sorted(POOLS), default="reposters")
    args = p.parse_args()
    if args.command == "build":
        rows = build(args.top, args.pool)
        print(f"wrote {POOLS[args.pool]} with {len(rows)} candidates ({sum('error' in r for r in rows)} failed)")
        return
    report = replay()
    REPORT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
