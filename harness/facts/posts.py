"""Facts from what an account and its thread already said, for profiles without a sports feed.

A dev-tool reply names products, people and numbers from the brand's own recent posts ("the 2.0 launch
last week", "Mark's talk"), which the thread under one post doesn't repeat. Each of the target author's
recent posts becomes a fact about that author, live for POST_DAYS from the day it went out. The thread
under the target becomes facts live for a day, covering replies past the dozen the writer's page shows.
The 30 days copy `harness.learning.brand.VALID_DAYS`; no data backs either number.
"""

from datetime import datetime, timedelta

from harness.facts.ground import split_handle
from harness.learning.kinds import Fact, short_hash, slug

POST_DAYS = 30
THREAD_DAYS = 1


def _subject(author: str) -> str:
    """'entity-atlanta-dream' for @AtlantaDream, so the handle's words anchor numbers in its facts."""
    return "entity-" + slug(split_handle(author))


def post_facts(author: str, posts: list[dict]) -> list[Fact]:
    """One fact per post of `author`'s: dicts with id, text, created_at (aware datetime) and url."""
    out = []
    for p in posts:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        when: datetime = p["created_at"]
        out.append(Fact(id=f"fact-post-{p['id']}", subject=_subject(author),
                        question=f"What did @{author.lstrip('@')} post on {when.date().isoformat()}?",
                        answer=text, quote=text, source_url=p.get("url") or "", as_of=when.date().isoformat(),
                        valid_until=(when + timedelta(days=POST_DAYS)).date().isoformat()))
    return out


def thread_facts(author: str, url: str, replies: list[str], at: datetime) -> list[Fact]:
    """The replies under one target as a single fact about its author, live from `at` for THREAD_DAYS."""
    texts = [r.strip() for r in replies if r and r.strip()]
    if not texts:
        return []
    return [Fact(id="fact-thread-" + short_hash(url), subject=_subject(author), question=f"What did people reply to {url}?",
                 answer="\n".join(texts), quote="\n".join(texts), source_url=url, as_of=at.date().isoformat(),
                 valid_until=(at + timedelta(days=THREAD_DAYS)).date().isoformat())]
