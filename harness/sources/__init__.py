from harness.models import Signal
from harness.sources.agent import ObservationAgentSource
from harness.sources.attention import AttentionSource
from harness.sources.base import Source
from harness.sources.bluesky import BlueskySource
from harness.sources.espn import EspnInjuriesSource, EspnNewsSource, EspnScoresSource
from harness.sources.google_news import GoogleNewsSource
from harness.sources.reddit import RedditSource
from harness.sources.reply_targets import ReplyTargetSource
from harness.sources.sample import SampleSource
from harness.sources.x_search import BrandActivitySource, XSearchSource

REGISTRY: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (
        SampleSource,
        RedditSource,
        XSearchSource,
        EspnNewsSource,
        EspnScoresSource,
        EspnInjuriesSource,
        GoogleNewsSource,
        ReplyTargetSource,
        BrandActivitySource,
        BlueskySource,
        AttentionSource,
        ObservationAgentSource,
    )
}


def observe(names: list[str], mission: str) -> list[Signal]:
    """Run each enabled source; a failing source is skipped, not fatal."""
    signals: list[Signal] = []
    for name in names:
        cls = REGISTRY.get(name)
        if not cls:
            print(f"[observe] unknown source '{name}', skipping")
            continue
        try:
            got = cls().fetch(mission)
            print(f"[observe] {name}: {len(got)} signals")
            signals.extend(got)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            print(f"[observe] {name} failed: {e}")
    return signals
