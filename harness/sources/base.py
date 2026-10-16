from harness.models import Signal


class Source:
    """A pluggable observer. Subclass, set `name`, implement `fetch`, register in sources/__init__.py."""

    name = "base"

    def fetch(self, mission: str) -> list[Signal]:
        raise NotImplementedError
