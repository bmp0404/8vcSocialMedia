import uuid
from dataclasses import asdict, dataclass, field

ACTION_TYPES = {"post", "reply", "quote", "mention", "follow"}


@dataclass
class Signal:
    """One raw item observed in the info sphere (a post, thread, headline...)."""

    source: str
    text: str
    url: str | None = None
    author: str | None = None
    # Who published a news link, for credit ("via ESPN"). `author` is the byline for some sources (ESPN), so
    # a caption credited it as "via Michael Voepel" (Integration run 37). Optional; None for posts.
    publisher: str | None = None
    created_at: str | None = None
    metrics: dict = field(default_factory=dict)
    # What the item is: "news", "game", "post", "reply_target", "own_post" or "mention".
    # Orient uses it to pick a velocity formula and to keep reply targets apart from news.
    kind: str = ""
    # For an X post: its most-liked direct replies, each {author, text, likes, followers}. Filled
    # for reply targets so the reply minds can see the thread they are joining without refetching.
    thread: list[dict] = field(default_factory=list)


@dataclass
class Narrative:
    """A cluster of signals the brand could plausibly join."""

    id: str
    title: str
    summary: str
    relevance: float = 0.0
    velocity: float = 0.0
    angle: str = ""
    signal_urls: list[str] = field(default_factory=list)
    # "data" when orient computed velocity from timestamps and counts, "llm" when it kept the
    # model's guess because no member signal had a timestamp.
    velocity_basis: str = ""
    # X posts in this narrative that a reply or quote can target, hottest first. Each dict has
    # url, author, text, created_at, age_minutes and engagement_per_hour (null when X gave no
    # counts), plus likes, reposts, replies, quotes, views, author_followers and views_per_hour
    # when the source reported them, and `thread`, the post's top replies (see Signal.thread).
    reply_targets: list[dict] = field(default_factory=list)


@dataclass
class Action:
    """A recommended X action, handed to the posting bot."""

    type: str
    text: str
    mind: str
    narrative_id: str
    rationale: str = ""
    target_url: str | None = None
    target_handle: str | None = None
    # qualify's score for this draft, or None when nothing scored it: the profile names no post ranker, or the draft
    # is a reply or a follow, which the learned ranker leaves alone (harness/qualify.py). None is "no prediction",
    # never a prediction of 0.
    predicted_score: float | None = None
    panel: dict = field(default_factory=dict)
    vetoed: bool = False
    critique: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # Set by the decide stage's lanes (harness/minds/lanes.py); optional, so older readers ignore them.
    lane: str = ""  # "reply" or "post"
    target_text: str = ""  # text of the post a reply or quote answers
    draft_meta: dict = field(default_factory=dict)  # how the draft was made and selected

    def to_dict(self) -> dict:
        return asdict(self)
