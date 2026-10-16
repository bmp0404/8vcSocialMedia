"""Turn the data team's post table into episodes and account profiles.

`data/devtools_virality_30d.csv` labels each post Super Viral, Medium Viral or Deadweight by raw
engagement (Deadweight is 0-10, Medium Viral 100-749, Super Viral 753 and up; nothing falls between 11
and 99). Those cutoffs mostly track account size, which is why the reflection job only contrasts posts
from the same account, and why the context worker's per-author ratio is attached when it exists.
"""

import csv
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from harness import config
from harness.learning.kinds import FAILURE, SUCCESS, Entity, Episode, Outcome, save, slug

DATASET_PATH = config.ROOT / "data" / "devtools_virality_30d.csv"
CONTEXT_PATH = config.ROOT / "data" / "context" / "devtools_enriched.csv"
RAW_COLUMNS = {"Category", "Account", "Post Content", "Media", "Likes", "Replies", "Retweets", "Engagement",
               "Post URL", "Date", "Segment"}
CATEGORY_LABELS = {"Super Viral": SUCCESS, "Medium Viral": SUCCESS, "Deadweight": FAILURE}
OUTCOME_DEFINITION = "failure = 10 or fewer total engagements; success = 100 or more (no posts fall between)"

# A post within 3 minutes of the same account's previous post is a thread continuation. The eval research
# report (8vc-context/research/eval.md) found these are 27% of posts and 4% viral, so leaving them in makes
# "why did these fail" rediscover "it was tweet 5 of a thread".
THREAD_GAP_SECONDS = 180
# Engagement on a post younger than this at collection time is still growing, so its label isn't final.
MIN_LABEL_AGE_HOURS = 48
# Launch posts get paid and bot boosts, and within one account their wording ("introducing", "now
# available") separates viral from dead. Reflection pairs launch posts only with launch posts.
LAUNCH_PATTERN = re.compile(
    r"\b(introduc(?:e|es|ed|ing)|announc(?:e|es|ed|ing)|launch(?:es|ed|ing)?|now (?:available|live|in|open)|"
    r"is (?:live|here|out)|out now|available (?:today|now)|just shipped|we shipped|releas(?:e|es|ed|ing)|"
    r"public beta|general availability|v\d+(?:\.\d+)+)\b",
    re.IGNORECASE,
)
TWITTER_EPOCH_MS = 1288834974657  # same constant as harness/context/snowflake.py on sam/context-dynamics


def snowflake_time(tid: str) -> datetime:
    """Creation time encoded in an X status id: (id >> 22) + Twitter epoch, in Unix milliseconds."""
    return datetime.fromtimestamp(((int(tid) >> 22) + TWITTER_EPOCH_MS) / 1000, tz=timezone.utc)


def tweet_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url)
    if not m:
        raise ValueError(f"no status id in post URL {url!r}")
    return m.group(1)


def public_metrics(row: dict) -> dict:
    """X API v2 public_metrics keys. `Engagement` is likes + replies + retweets + quotes, so quotes are the rest."""
    likes, replies, retweets = int(row["Likes"]), int(row["Replies"]), int(row["Retweets"])
    metrics = {"like_count": likes, "reply_count": replies, "retweet_count": retweets}
    quotes = int(row["Engagement"]) - likes - replies - retweets
    if quotes >= 0:
        metrics["quote_count"] = quotes
    return metrics


def row_features(row: dict) -> dict:
    text = row["Post Content"]
    return {
        "media": row["Media"],
        "segment": row["Segment"],
        "engagement": int(row["Engagement"]),
        "has_link": "https://t.co/" in text,
        "starts_with_mention": text.lstrip().startswith("@"),
        "n_chars": len(text),
        "n_hashtags": len(re.findall(r"#\w+", text)),
        "is_launch": bool(LAUNCH_PATTERN.search(text)),
    }


def row_to_episode(row: dict, source: str) -> Episode:
    category = row["Category"]
    if category not in CATEGORY_LABELS:
        raise ValueError(f"unknown Category {category!r} in row {row.get('Post URL')}")
    tid = tweet_id(row["Post URL"])
    metrics = public_metrics(row)
    return Episode(
        id=f"post-{tid}",
        source=source,
        author_username=row["Account"].lstrip("@"),
        text=row["Post Content"],
        created_at=snowflake_time(tid).isoformat(timespec="seconds"),
        tweet_id=tid,
        url=row["Post URL"],
        features=row_features(row),
        outcomes=[Outcome(label=CATEGORY_LABELS[category], source="dataset", raw=category, metrics=metrics)],
    )


def load_dataset(path: Path = DATASET_PATH, thread_gap_seconds: int = THREAD_GAP_SECONDS) -> list[Episode]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = RAW_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing columns {sorted(missing)}")
        episodes = [row_to_episode(row, "dataset") for row in reader]
    for ep in episodes:
        ep.features["dataset"] = path.stem
    mark_threads_and_age(episodes, thread_gap_seconds=thread_gap_seconds)
    return episodes


def mark_threads_and_age(episodes: list[Episode], collected_at: datetime | None = None,
                         thread_gap_seconds: int = THREAD_GAP_SECONDS) -> None:
    """Set `thread_continuation` and `label_age_hours` from snowflake times.

    Collection time defaults to the newest post in the set, the only collection time the CSV implies.
    """
    times = {ep.id: datetime.fromisoformat(ep.created_at) for ep in episodes}
    collected = collected_at or max(times.values())
    by_author: dict[str, list[Episode]] = defaultdict(list)
    for ep in episodes:
        by_author[ep.author_username].append(ep)
    for eps in by_author.values():
        eps.sort(key=lambda e: times[e.id])
        for prev, ep in zip([None, *eps], eps):
            gap = (times[ep.id] - times[prev.id]).total_seconds() if prev else None
            ep.features["thread_continuation"] = gap is not None and gap < thread_gap_seconds
    for ep in episodes:
        ep.features["label_age_hours"] = round((collected - times[ep.id]).total_seconds() / 3600, 1)


def _parse(value: str):
    v = value.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return float(v) if any(c in v for c in ".eE") else int(v)
    except ValueError:
        return v


def attach_context(episodes: list[Episode], path: Path = CONTEXT_PATH) -> int:
    """Copy the context worker's per-post columns (eng_ratio_author_median, boost_suspect...) into features.

    Joins on `Post URL`. Columns that repeat the raw dataset are skipped. Returns how many episodes matched.
    """
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "Post URL" not in (reader.fieldnames or []):
            raise ValueError(f"{path} has no 'Post URL' column to join on; columns: {reader.fieldnames}")
        extra = [c for c in reader.fieldnames if c not in RAW_COLUMNS]
        by_url = {row["Post URL"]: row for row in reader}
    matched = 0
    for ep in episodes:
        row = by_url.get(ep.url or "")
        if row is None:
            continue
        matched += 1
        for col in extra:
            if row[col] != "":
                ep.features[col] = _parse(row[col])
    return matched


def account_profiles(episodes: list[Episode], domain: str) -> list[Entity]:
    """One Entity per author: post count, outcome mix, engagement median and range, dates, media share."""
    by_author: dict[str, list[Episode]] = defaultdict(list)
    for ep in episodes:
        by_author[ep.author_username].append(ep)
    profiles = []
    for author in sorted(by_author):
        eps = by_author[author]
        engagement = [ep.features.get("engagement", 0) for ep in eps]
        raw = Counter(o.raw for ep in eps for o in ep.outcomes if o.source == "dataset")
        labels = Counter(ep.label() for ep in eps)
        dates = sorted(ep.created_at for ep in eps if ep.created_at)
        with_media = sum(1 for ep in eps if ep.features.get("media", "none") != "none")
        stats = {
            "posts": len(eps),
            "successes": labels[SUCCESS],
            "failures": labels[FAILURE],
            "categories": dict(sorted(raw.items())),
            "median_engagement": statistics.median(engagement),
            "max_engagement": max(engagement),
            "media_share": round(with_media / len(eps), 3),
            "first_post": dates[0] if dates else "",
            "last_post": dates[-1] if dates else "",
            "segments": sorted({ep.features.get("segment", "") for ep in eps} - {""}),
        }
        summary = (
            f"@{author} ({domain}): {stats['posts']} posts from {stats['first_post']} to {stats['last_post']}, "
            f"{stats['successes']} succeeded and {stats['failures']} failed; median engagement "
            f"{stats['median_engagement']}, max {stats['max_engagement']}."
        )
        profiles.append(
            Entity(
                id=f"entity-{slug(author)}",
                name=f"@{author}",
                summary=summary,
                entity_type="account",
                aliases=[author],
                stats=stats,
                updated=stats["last_post"][:10],
            )
        )
    return profiles


def exemplars(episodes: list[Episode], per_category: int = 5) -> list[Episode]:
    """The `per_category` highest- and lowest-engagement posts of each Category, thread continuations excluded.

    Krish's store takes exemplars rather than every row, so 1,665 posts don't flood keyword search; the
    rest of the table reaches memory as lessons from the reflection job.
    """
    by_cat: dict[str, list[Episode]] = defaultdict(list)
    for ep in episodes:
        if ep.features.get("thread_continuation") is not True:
            raw = next(o.raw for o in ep.outcomes if o.source == "dataset")
            by_cat[raw].append(ep)
    picked: dict[str, Episode] = {}
    for raw in sorted(by_cat):
        eps = sorted(by_cat[raw], key=lambda e: (e.features.get("engagement", 0), e.id))
        for ep in eps[:per_category] + eps[-per_category:]:
            picked[ep.id] = ep
    return sorted(picked.values(), key=lambda e: e.id)


def ingest_dataset(store, path: Path = DATASET_PATH, context_path: Path | None = CONTEXT_PATH,
                   domain: str = "devtools", per_category: int = 5) -> dict:
    """Write exemplar posts as episodes and every account as an entity profile."""
    episodes = load_dataset(path)
    matched = 0
    if context_path is not None and context_path.exists():
        matched = attach_context(episodes, context_path)
    for ep in episodes:
        ep.features["domain"] = domain
    chosen = exemplars(episodes, per_category)
    for ep in chosen:
        save(store, ep)
    profiles = account_profiles(episodes, domain)
    for profile in profiles:
        save(store, profile)
    summary = {"rows": len(episodes), "exemplar_episodes": len(chosen), "entities": len(profiles),
               "context_matched": matched}
    print(f"[ingest] {path.name}: {summary}")
    return summary
