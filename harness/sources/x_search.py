from harness.models import Signal
from harness.sources.base import Source


class XSearchSource(Source):
    """Live X posts about the account's subjects.

    TODO: pick an access path once we know what 8VC's credits cover:
      - Grok (xAI) API with live X search
      - X API v2 recent search (needs a paid tier)
      - Playwright / browser use against x.com/search when no API is available
    Must return Signal(source="x", text, id, conversation_id, url, author, created_at,
    metrics={likes, reposts, replies, views}) using keyword arguments.
    """

    name = "x"

    def fetch(self, mission: str) -> list[Signal]:
        raise NotImplementedError("X search source not wired up yet; see docstring.")
