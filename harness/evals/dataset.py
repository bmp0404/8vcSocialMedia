"""Load the data team's dev-tool dataset and decide which posts the backtest may score.

The CSV keeps only the two tails of engagement: no post has 11 to 99. Every post is therefore
either viral (Medium or Super Viral, 100 or more) or Deadweight (10 or less), and the backtest
compares those two tiers inside one account. Raw engagement mostly measures account size because
the file has no follower counts.

Eligibility removes posts whose outcome says little about their text:
- thread continuations and replies, which sit under another post and get its leftovers;
- posts younger than 48 hours at scrape time, whose engagement was still growing.
"""

import csv
import html
import math
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from harness import config
from harness.evals.settings import BacktestConfig

DATASET_PATH = config.ROOT / "data" / "devtools_virality_30d.csv"
ENRICHED_PATH = config.ROOT / "data" / "context" / "devtools_enriched.csv"
CATEGORIES = ("Super Viral", "Medium Viral", "Deadweight")
TWITTER_EPOCH_MS = 1288834974657  # same decoding as harness/context/snowflake.py on sam/context-dynamics
# Thresholds live in settings.BacktestConfig. Their evidence:
# - thread_gap_minutes=3: checked against fxtwitter's replying_to on 796 fetched posts, 82 of 83 posts
#   under 3 minutes were self-replies, and the rule caught 82 of 115 self-replies (the rest were
#   follow-ups posted 10 minutes to hours later).
# - min_age_hours=48: the research audit (8vc-context/research/eval.md, finding 4) found viral rates of
#   19% at 0-12 h and 33% at 24-48 h against 42% at 2-7 days, so younger posts were still accruing.

_TCO = re.compile(r"\s*https://t\.co/\S+")
# Launch language, from the memory research report (8vc-context/research/memory.md, finding 2): within one
# account, the words that predict engagement are launch words, and launches draw paid and bot boosts.
_LAUNCH = re.compile(r"\b(introducing|now available|is live|is here|launch(es|ed|ing)?|ships?|shipped|"
                     r"announc(e|es|ed|ing)|v?\d+\.\d+)\b", re.I)


@dataclass
class Post:
    id: str  # the Post URL; unique in the CSV
    account: str
    text: str
    media: str
    likes: int
    replies: int
    retweets: int
    engagement: int  # likes + replies + retweets + quotes (verified by the context worker via fxtwitter)
    date: date
    category: str
    segment: str
    created_at: datetime | None = None  # decoded from the status id
    minutes_after_previous: float | None = None  # gap to the same account's previous post in the CSV
    age_hours: float | None = None  # at the newest post in the CSV, which stands in for scrape time
    target: float = 0.0  # log1p(engagement) - log1p(account median), or the enriched equivalent
    boost_suspect: bool | None = None  # None until the context worker's flags are loaded

    @property
    def clean_text(self) -> str:
        """Text with t.co links removed, so a model can't key on a link and a reader sees the words."""
        return html.unescape(_TCO.sub("", self.text)).strip()

    @property
    def viral(self) -> bool:
        return self.category != "Deadweight"


def created_at(url: str) -> datetime:
    status = url.rstrip("/").rsplit("/", 1)[1]
    if not status.isdigit():
        raise ValueError(f"no status id in {url}")
    return datetime.fromtimestamp(((int(status) >> 22) + TWITTER_EPOCH_MS) / 1000, timezone.utc)


def load(path: Path = DATASET_PATH, enriched: Path | None = ENRICHED_PATH) -> list[Post]:
    if not path.exists():
        raise FileNotFoundError(f"dataset not found at {path}; pull main, where David pushed it")
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    missing = {"Category", "Account", "Post Content", "Media", "Engagement", "Post URL", "Date"} - set(rows[0])
    if missing:
        raise ValueError(f"{path} is missing columns {sorted(missing)}")
    posts = [
        Post(
            id=r["Post URL"], account=r["Account"], text=r["Post Content"], media=r["Media"],
            likes=int(r["Likes"]), replies=int(r["Replies"]), retweets=int(r["Retweets"]),
            engagement=int(r["Engagement"]), date=date.fromisoformat(r["Date"]),
            category=r["Category"], segment=r["Segment"], created_at=created_at(r["Post URL"]),
        )
        for r in rows
    ]
    unknown = {p.category for p in posts} - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unexpected Category values {sorted(unknown)}")
    attach_timing(posts)
    attach_targets(posts)
    if enriched is not None and enriched.exists():
        apply_enriched(posts, enriched)
    return posts


def attach_timing(posts: list[Post]) -> None:
    newest = max(p.created_at for p in posts)
    by_account: dict[str, list[Post]] = {}
    for p in posts:
        p.age_hours = (newest - p.created_at).total_seconds() / 3600
        by_account.setdefault(p.account, []).append(p)
    for group in by_account.values():
        group.sort(key=lambda p: p.created_at)
        for prev, p in zip(group, group[1:]):
            p.minutes_after_previous = (p.created_at - prev.created_at).total_seconds() / 60


def attach_targets(posts: list[Post]) -> None:
    """Set each post's target to log1p(engagement) - log1p(median engagement of its account).

    Only the secondary viral-vs-viral metric reads this, and within one account it orders posts
    exactly as raw engagement does. With the middle of the distribution missing, where an account's
    median lands depends on its mix of tails, so the headline compares tiers instead.
    """
    by_account: dict[str, list[int]] = {}
    for p in posts:
        by_account.setdefault(p.account, []).append(p.engagement)
    medians = {a: statistics.median(es) for a, es in by_account.items()}
    for p in posts:
        p.target = math.log1p(p.engagement) - math.log1p(medians[p.account])


def apply_enriched(posts: list[Post], path: Path) -> None:
    """Take the context worker's normalized target and boost flags, keyed by Post URL."""
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    need = {"Post URL", "eng_ratio_author_median", "boost_suspect"}
    if not rows or need - set(rows[0]):
        raise ValueError(f"{path} must have columns {sorted(need)}; found {sorted(rows[0]) if rows else 'no rows'}")
    by_id = {r["Post URL"]: r for r in rows}
    for p in posts:
        r = by_id.get(p.id)
        if r is None:
            raise ValueError(f"{path} has no row for {p.id}; regenerate it from the current dataset")
        # log keeps the same ordering as the ratio and puts it on the scale attach_targets uses.
        p.target = math.log(max(float(r["eng_ratio_author_median"]), 1e-9))
        p.boost_suspect = r["boost_suspect"].strip().lower() in {"1", "true", "yes"}


def is_scorable(p: Post, cfg: BacktestConfig = BacktestConfig()) -> bool:
    """Drop fragments that are mostly a link or a hashtag, and @-replies."""
    text = p.clean_text
    return len(text) >= cfg.min_chars and not text.startswith("@")


def is_thread_continuation(p: Post, cfg: BacktestConfig = BacktestConfig()) -> bool:
    return p.minutes_after_previous is not None and p.minutes_after_previous < cfg.thread_gap_minutes


def is_young(p: Post, cfg: BacktestConfig = BacktestConfig()) -> bool:
    return (p.age_hours or 0.0) < cfg.min_age_hours


def is_eligible(p: Post, cfg: BacktestConfig = BacktestConfig()) -> bool:
    return is_scorable(p, cfg) and not is_thread_continuation(p, cfg) and not is_young(p, cfg)


def is_launch(p: Post) -> bool:
    """A keyword rule for launch and release posts. Crude on purpose: it only has to split pairs."""
    return bool(_LAUNCH.search(p.clean_text))
