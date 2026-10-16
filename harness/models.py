import uuid
from dataclasses import asdict, dataclass, field

ACTION_TYPES = {"post", "reply", "quote", "mention", "follow"}
LANE_TYPES = {"reply": {"reply", "quote", "mention", "follow"}, "post": {"post"}}
# Reply-lane outcomes where the account deliberately does nothing. Logged to memory, never emailed.
#   wait: worth revisiting later (thread still forming, nothing sharp yet)
#   no_response: not worth engaging with at all
DECLINE_TYPES = {"wait", "no_response"}

# Structured reason carried by every veto, rejection and decline (memory `actions.reason`).
REASONS = ("off_tone", "stale", "wrong_target", "nothing_to_add", "factual_risk", "repetitive", "other")


def norm_reason(value) -> str:
    """Coerce whatever a model or a human sent into one of REASONS."""
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in REASONS else "other"


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
    sentiment: dict = field(default_factory=dict)  # predicted reaction split {positive, neutral, negative}
    vetoed: bool = False
    critique: str = ""
    reason: str | None = None  # one of REASONS when vetoed, rejected or declined
    lane: str = "post"  # reply | post
    context: dict = field(default_factory=dict)  # the observed Signal (reply) or Narrative (post) it responds to
    run_id: int | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return asdict(self)
