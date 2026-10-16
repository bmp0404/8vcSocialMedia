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


@dataclass
class Action:
    """A recommended X action, handed to the posting bot."""

    type: str
    text: str
    mind: str
    narrative_id: str
    variant: str = ""
    rationale: str = ""
    target_url: str | None = None
    target_handle: str | None = None
    predicted_score: float = 0.0
    panel: dict = field(default_factory=dict)
    vetoed: bool = False
    critique: str = ""
    elo: float | None = None  # pairwise tournament rating
    record: str = ""  # tournament wins-losses, e.g. "3-0"
    rank: int | None = None  # final position in this run
    hook_alternatives: list[str] = field(default_factory=list)
    pre_hook_text: str | None = None  # text before the hook pass swapped the opening line
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return asdict(self)
