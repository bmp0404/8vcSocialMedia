import json

from harness import config
from harness.models import Signal
from harness.sources.base import Source


class SampleSource(Source):
    """Offline fixture data, so the loop can run without network or X access."""

    name = "sample"

    def fetch(self, mission: str) -> list[Signal]:
        rows = json.loads(config.SAMPLE_SIGNALS_PATH.read_text(encoding="utf-8"))
        return [Signal(source=self.name, **row) for row in rows]
