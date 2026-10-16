"""Load the brand's observe settings from the active mission profile's `observe.toml`.

Keeping queries, leagues and watched accounts in a TOML file keeps Python brand-agnostic: each
brand has its own profile under mission/profiles/, and HARNESS_PROFILE picks one.
"""

import os
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from harness import config
from harness.sources.common import SourceError

# Names the brand account for one run or one machine without editing the shared TOML file.
ACCOUNT_HANDLE_ENV = "HARNESS_ACCOUNT_HANDLE"


@dataclass
class EspnWatch:
    leagues: list[str] = field(default_factory=list)
    scoreboard_leagues: list[str] = field(default_factory=list)
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
class AttentionWatch:
    terms: list[str] = field(default_factory=list)
    baseline_days: int = 14


@dataclass
class AgentWatch:
    # Feeds the observation agent reads when the LLM is unavailable, and its starting plan.
    default_sources: list[str] = field(default_factory=list)


@dataclass
class ReplyTargetWatch:
    accounts: list[str] = field(default_factory=list)
    broad_accounts: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    include_search: bool = True
    max_age_minutes: int = 90
    limit: int = 10


@dataclass
class Watch:
    brand_handle: str = ""
    espn: EspnWatch = field(default_factory=EspnWatch)
    google_news: NewsWatch = field(default_factory=NewsWatch)
    x: XWatch = field(default_factory=XWatch)
    bluesky: BlueskyWatch = field(default_factory=BlueskyWatch)
    attention: AttentionWatch = field(default_factory=AttentionWatch)
    agent: AgentWatch = field(default_factory=AgentWatch)
    reply_targets: ReplyTargetWatch = field(default_factory=ReplyTargetWatch)


def _section(cls, raw: dict, name: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise SourceError(f"observe config: unknown keys in [{name}]: {sorted(unknown)}; expected {sorted(known)}")
    return cls(**raw)


def parse_watch(raw: dict) -> Watch:
    """Build a Watch from parsed TOML, rejecting keys it doesn't know so typos fail loudly."""
    sections = {"brand", "espn", "google_news", "x", "bluesky", "attention", "agent", "reply_targets"}
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
        attention=_section(AttentionWatch, raw.get("attention", {}), "attention"),
        agent=_section(AgentWatch, raw.get("agent", {}), "agent"),
        reply_targets=_section(ReplyTargetWatch, raw.get("reply_targets", {}), "reply_targets"),
    )


# Set by the observation agent so a source it calls reads the agent's queries for that one call.
_ACTIVE: ContextVar[Watch | None] = ContextVar("observe_watch", default=None)


@contextmanager
def use(watch: Watch):
    """Make load_watch() return `watch` inside the block."""
    token = _ACTIVE.set(watch)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def config_path() -> Path:
    """The active profile's observe.toml (config.profile_file); a missing file is a SourceError."""
    try:
        return config.profile_file("observe.toml")
    except config.ProfileError as e:
        raise SourceError(f"observe config: {e}") from e


def _handle_override() -> str:
    return os.getenv(ACCOUNT_HANDLE_ENV, "").strip().lstrip("@").strip()


def load_watch(path: Path | None = None) -> Watch:
    """The observe config, with HARNESS_ACCOUNT_HANDLE in place of [brand] x_handle when it is set."""
    if path is None and (active := _ACTIVE.get()) is not None:
        return active
    path = path or config_path()
    if not path.exists():
        raise SourceError(f"observe config not found at {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SourceError(f"observe config {path} is not valid TOML: {e}") from e
    watch = parse_watch(raw)
    if override := _handle_override():
        watch.brand_handle = override
    return watch


def account_handle() -> tuple[str, str]:
    """(the brand account's X handle without "@", where it came from); the handle is "" when unset.

    [brand] x_handle is the one place the account is named, and HARNESS_ACCOUNT_HANDLE overrides it.
    This is the account Observe reads as x_brand and `harness outcomes` measures. The reply writer's
    framing_handle in the profile's reply_framing.json is a different setting: it shapes how drafts read.
    """
    if override := _handle_override():
        return override, ACCOUNT_HANDLE_ENV
    path = config_path()
    return load_watch(path).brand_handle, f"{path} [brand] x_handle"
