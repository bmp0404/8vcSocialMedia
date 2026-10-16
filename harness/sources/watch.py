"""Load the brand's observe settings from `mission/observe.toml`.

Keeping queries, leagues and watched accounts in a TOML file keeps Python brand-agnostic: the
dev-tool brand writes its own file and sets HARNESS_OBSERVE_CONFIG.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from harness import config
from harness.sources.common import SourceError

DEFAULT_PATH = config.ROOT / "mission" / "observe.toml"


@dataclass
class EspnWatch:
    leagues: list[str] = field(default_factory=list)
    teams: dict[str, list[str]] = field(default_factory=dict)
    articles_per_league: int = 15


@dataclass
class NewsWatch:
    queries: list[str] = field(default_factory=list)
    window: str = "2d"
    per_query: int = 15


@dataclass
class XWatch:
    queries: list[str] = field(default_factory=list)
    lookback_hours: int = 24
    per_query: int = 25


@dataclass
class BlueskyWatch:
    queries: list[str] = field(default_factory=list)
    lookback_hours: int = 24
    per_query: int = 25


@dataclass
class ReplyTargetWatch:
    accounts: list[str] = field(default_factory=list)
    broad_accounts: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    include_search: bool = True
    max_age_minutes: int = 180
    limit: int = 10


@dataclass
class Watch:
    brand_handle: str = ""
    espn: EspnWatch = field(default_factory=EspnWatch)
    google_news: NewsWatch = field(default_factory=NewsWatch)
    x: XWatch = field(default_factory=XWatch)
    bluesky: BlueskyWatch = field(default_factory=BlueskyWatch)
    reply_targets: ReplyTargetWatch = field(default_factory=ReplyTargetWatch)


def _section(cls, raw: dict, name: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise SourceError(f"observe config: unknown keys in [{name}]: {sorted(unknown)}; expected {sorted(known)}")
    return cls(**raw)


def parse_watch(raw: dict) -> Watch:
    """Build a Watch from parsed TOML, rejecting keys it doesn't know so typos fail loudly."""
    sections = {"brand", "espn", "google_news", "x", "bluesky", "reply_targets"}
    unknown = set(raw) - sections
    if unknown:
        raise SourceError(f"observe config: unknown sections {sorted(unknown)}; expected {sorted(sections)}")
    brand = raw.get("brand", {})
    if set(brand) - {"x_handle"}:
        raise SourceError(f"observe config: [brand] only takes x_handle, got {sorted(brand)}")
    return Watch(
        brand_handle=brand.get("x_handle", "").lstrip("@").strip(),
        espn=_section(EspnWatch, raw.get("espn", {}), "espn"),
        google_news=_section(NewsWatch, raw.get("google_news", {}), "google_news"),
        x=_section(XWatch, raw.get("x", {}), "x"),
        bluesky=_section(BlueskyWatch, raw.get("bluesky", {}), "bluesky"),
        reply_targets=_section(ReplyTargetWatch, raw.get("reply_targets", {}), "reply_targets"),
    )


def load_watch(path: Path | None = None) -> Watch:
    path = path or Path(os.getenv("HARNESS_OBSERVE_CONFIG") or DEFAULT_PATH)
    if not path.exists():
        raise SourceError(f"observe config not found at {path}; copy mission/observe.toml or set HARNESS_OBSERVE_CONFIG")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SourceError(f"observe config {path} is not valid TOML: {e}") from e
    return parse_watch(raw)
