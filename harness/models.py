import uuid
from dataclasses import asdict, dataclass, field

# The X actions 8VC's posting bot executes. The full action space, including outreach the team
# sends, is defined in harness/actions/catalog.py.
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
    """A recommended action: an X action for the posting bot, or outreach/content work for the team.

    The fields after `target_handle` follow the action-selection output: who it's for, why (evidence),
    where and when, what it needs first, what success looks like and by when, and when to drop it.
    Type-specific fields (a pitch's outlet, a clip's timestamps...) live in `details`.
    """

    type: str
    text: str
    mind: str
    narrative_id: str
    variant: str = ""
    rationale: str = ""
    target_url: str | None = None
    target_handle: str | None = None
    channel: str = "x"
    target_person: str | None = None
    evidence: list[str] = field(default_factory=list)
    timing: str = ""
    prerequisites: list[str] = field(default_factory=list)
    intended_outcome: str = ""
    measure_days: int = 0
    dismiss_if: str = ""
    missing_info: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
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
