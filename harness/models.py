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
    created_at: str | None = None
    metrics: dict = field(default_factory=dict)
    # What the item is: "news", "game", "post", "reply_target", "own_post" or "mention".
    # Orient uses it to pick a velocity formula and to keep reply targets apart from news.
    kind: str = ""


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
    # url, author, text, age_minutes and engagement_per_hour (null when X gave no counts).
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
    predicted_score: float = 0.0
    panel: dict = field(default_factory=dict)
    vetoed: bool = False
    critique: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return asdict(self)
