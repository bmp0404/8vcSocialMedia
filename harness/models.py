import uuid
from dataclasses import asdict, dataclass, field

ACTION_TYPES = {"post", "reply", "quote", "mention", "follow"}
LANE_TYPES = {"reply": {"reply", "quote", "mention", "follow"}, "post": {"post"}}


@dataclass
class Signal:
    """One raw item observed in the info sphere (a post, thread, headline...)."""

    source: str
    text: str
    id: str | None = None  # native id on the source platform (e.g. tweet id)
    conversation_id: str | None = None
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
    """A recommended X action, handed to the posting bot. One row in memory's `actions` table."""

    type: str
    text: str
    mind: str
    narrative_id: str  # post lane: narrative slug; reply lane: "sig:<signal id>"
    rationale: str = ""
    target_url: str | None = None
    target_handle: str | None = None  # without the leading @
    predicted_score: float = 0.0
    panel: dict = field(default_factory=dict)
    vetoed: bool = False
    critique: str = ""
    lane: str = "post"  # reply | post
    context: dict = field(default_factory=dict)  # the observed Signal (reply) or Narrative (post) it responds to
    run_id: int | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return asdict(self)
