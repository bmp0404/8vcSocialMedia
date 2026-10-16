"""Load the data team's dev-tool dataset and attach the within-account target the backtest ranks against.

The CSV has no follower counts, so raw engagement mostly measures account size. The target here is
how a post did against its own account's median: log1p(engagement) - log1p(account median). It ranks
posts the same way as (engagement + 1) / (median + 1); the +1 keeps accounts whose median is 0 usable.
When the context worker's enriched file exists, its `eng_ratio_author_median` and `boost_suspect`
columns replace the local values (joined on `Post URL`).
"""

import csv
import html
import math
import re
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from harness import config

DATASET_PATH = config.ROOT / "data" / "devtools_virality_30d.csv"
ENRICHED_PATH = config.ROOT / "data" / "context" / "devtools_enriched.csv"
CATEGORIES = ("Super Viral", "Medium Viral", "Deadweight")
_TCO = re.compile(r"\s*https://t\.co/\S+")


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
    target: float = 0.0  # log1p(engagement) - log1p(account median), or the enriched equivalent
    boost_suspect: bool | None = None  # None until the context worker's flags are loaded

    @property
    def clean_text(self) -> str:
        """Text with t.co links removed, so a model can't key on a link and a reader sees the words."""
        return html.unescape(_TCO.sub("", self.text)).strip()


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
            category=r["Category"], segment=r["Segment"],
        )
        for r in rows
    ]
    unknown = {p.category for p in posts} - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unexpected Category values {sorted(unknown)}")
    attach_targets(posts)
    if enriched is not None and enriched.exists():
        apply_enriched(posts, enriched)
    return posts


def attach_targets(posts: list[Post]) -> None:
    """Set each post's target to log1p(engagement) - log1p(median engagement of its account)."""
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


def is_scorable(p: Post, min_chars: int = 40) -> bool:
    """Drop thread fragments ("See the rest here:", bare links, hashtags) and replies.

    A thread's later posts get almost no engagement because of where they sit, not how they read,
    so keeping them would reward a predictor for spotting fragments.
    """
    text = p.clean_text
    return len(text) >= min_chars and not text.startswith("@")

