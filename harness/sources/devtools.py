from datetime import timedelta

from harness import dataset
from harness.models import Signal
from harness.sources.base import Source

DAYS = 7
LIMIT = 60


class DevtoolsSource(Source):
    """Recent high-engagement posts from the data team's dev-tool dataset: live reply/quote targets.

    "Recent" is relative to the newest post in the file, so a stale export still yields signals.
    """

    name = "devtools"

    def fetch(self, mission: str) -> list[Signal]:
        posts = dataset.load()
        newest = max(p.date for p in posts)
        recent = [p for p in posts if p.date >= newest - timedelta(days=DAYS) and p.category in dataset.VIRAL]
        recent.sort(key=lambda p: -p.engagement)
        return [
            Signal(
                source=self.name,
                text=p.clean_text,
                url=p.url,
                author=p.account.lstrip("@"),
                created_at=p.date.isoformat(),
                metrics={"likes": p.likes, "replies": p.replies, "reposts": p.reposts, "category": p.category},
            )
            for p in recent[:LIMIT]
        ]
