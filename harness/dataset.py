"""The data team's dev-tool virality dataset (data/devtools_virality_30d.csv).

~1.7k X posts from ~90 dev-tool company accounts over 30 days, each labelled Super Viral / Medium Viral /
Deadweight, with a Segment for account size. "Smaller/closest" accounts are nearest to our own size,
so they're the fairest comparison; "Other" is mostly huge accounts whose reach we can't copy.
"""

import csv
import html
import math
import re
from dataclasses import dataclass
from datetime import date

from harness import config

VIRAL = ("Super Viral", "Medium Viral")
COMPARABLE_SEGMENTS = ("Smaller/closest", "Mid-size")
MIN_EXEMPLAR_CHARS = 50  # skip "Congrats @x" and thread fragments: no reusable pattern


@dataclass
class Post:
    account: str
    text: str
    media: str
    likes: int
    replies: int
    reposts: int
    engagement: int
    url: str
    date: date
    category: str
    segment: str

    @property
    def clean_text(self) -> str:
        return html.unescape(re.sub(r"\s*https://t\.co/\S+", "", self.text)).strip()

    @property
    def log_engagement(self) -> float:
        return math.log1p(self.engagement)

    def brief(self) -> dict:
        return {"account": self.account, "text": self.clean_text, "media": self.media,
                "engagement": self.engagement, "category": self.category, "segment": self.segment}


def _int(v: str) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def load(path=None) -> list[Post]:
    path = path or config.DATASET_PATH
    if not path.exists():
        raise FileNotFoundError(f"dataset not found at {path} (pull main, or set HARNESS_DATASET)")
    with open(path, encoding="utf-8", newline="") as f:
        return [
            Post(
                account=r["Account"], text=r["Post Content"], media=r["Media"],
                likes=_int(r["Likes"]), replies=_int(r["Replies"]), reposts=_int(r["Retweets"]),
                engagement=_int(r["Engagement"]), url=r["Post URL"], date=date.fromisoformat(r["Date"]),
                category=r["Category"], segment=r["Segment"],
            )
            for r in csv.DictReader(f)
            if r.get("Post Content")
        ]


def _diverse(posts: list[Post], n: int, per_account: int = 2) -> list[Post]:
    out, seen = [], {}
    for p in posts:
        if seen.get(p.account, 0) < per_account:
            out.append(p)
            seen[p.account] = seen.get(p.account, 0) + 1
        if len(out) == n:
            break
    return out


def exemplars(posts: list[Post] | None = None, winners: int = 12, losers: int = 6) -> dict[str, list[dict]]:
    """Winners from comparable-size accounts, plus flops from the *same* accounts as contrast:
    same company, same audience, different result is the most informative pair."""
    posts = posts if posts is not None else load()
    comparable = [p for p in posts if p.segment in COMPARABLE_SEGMENTS and len(p.clean_text) >= MIN_EXEMPLAR_CHARS]
    wins = _diverse(sorted((p for p in comparable if p.category in VIRAL), key=lambda p: -p.engagement), winners)
    accounts = {p.account for p in wins}
    flops = _diverse(sorted((p for p in comparable if p.category == "Deadweight" and p.account in accounts),
                            key=lambda p: (p.engagement, p.account)), losers, per_account=1)
    return {"winners": [p.brief() for p in wins], "flops": [p.brief() for p in flops]}
