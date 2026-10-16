"""Save facts to the active profile's memory store, load the live ones, and refresh them from their source.

`refresh` picks the source from the profile's observe.toml: ESPN for each league in `[espn]
scoreboard_leagues`, else the recent posts of each account in `[reply_targets] accounts`.

    HARNESS_PROVIDER=anthropic HARNESS_PROFILE=wnba uv run python -m harness.facts refresh
"""

import os
from datetime import datetime, timezone

from harness import config
from harness.facts import ground, posts
from harness.facts.espn import Espn
from harness.learning import kinds
from harness.learning.kinds import Fact
from harness.learning.store import flush, get_store


class FactsError(RuntimeError):
    """A profile has no facts source, or facts would land in a throwaway store."""


def save(facts: list[Fact], store=None) -> int:
    store = store or get_store()
    for f in facts:
        kinds.save(store, f)
    flush(store)
    return len(facts)


def load(store=None) -> list[Fact]:
    store = store or get_store()
    return [item for m in store.list_memories(Fact.KIND) if isinstance(item := kinds.load(m), Fact)]


def load_live(at: datetime, store=None) -> list[Fact]:
    return ground.live(load(store), at)


def refresh(profile: str, *, at: datetime | None = None, espn: Espn | None = None, reader=None, store=None) -> dict:
    """Fetch the profile's facts as of `at` (default now) and save them. Returns counts per source."""
    from harness.sources.watch import load_watch

    at = at or datetime.now(timezone.utc)
    with config.use_profile(profile):
        if store is None and config.PROVIDER == "mock" and not os.getenv("MEMSTORE_PATH"):
            raise FactsError("the mock provider keeps memory in a temporary store that real runs never read; "
                             "set HARNESS_PROVIDER as for a real run, or MEMSTORE_PATH")
        watch = load_watch()
        counts: dict[str, int] = {}
        if watch.espn.scoreboard_leagues:
            for league in watch.espn.scoreboard_leagues:
                facts = (espn or Espn(league=league)).facts_at(at, rosters=True)
                counts[f"espn:{league}"] = save(facts, store)
        elif watch.reply_targets.accounts:
            if reader is None:
                from harness.context.http import PoliteClient
                from harness.context.xreader import FxTwitterReader

                reader = FxTwitterReader(PoliteClient(min_interval_s=1.0), max_age_s=600)
            from harness.context.http import FetchError

            failed = {}
            for handle in watch.reply_targets.accounts:
                # fxtwitter answers about 1 timeline read in 10 with a 404 that a later read gets past
                # (harness/watcher/DESIGN.md), so one failed handle skips that handle, not the other 23.
                try:
                    statuses = reader.profile_statuses(handle, limit=20)
                except FetchError as e:
                    failed[handle] = str(e)
                    print(f"[facts] skipped @{handle}: {e}")
                    continue
                own = [{"id": p.id, "text": p.text, "created_at": p.created_at, "url": p.url}
                       for p in statuses if not p.is_repost and p.author_handle.lower() == handle.lower()]
                counts[f"posts:{handle}"] = save(posts.post_facts(handle, own), store)
            if not counts:
                raise FactsError(f"every timeline read failed: {failed}")
        else:
            raise FactsError(f"profile {profile!r} has neither [espn] scoreboard_leagues nor [reply_targets] accounts "
                             "in observe.toml, so there is nothing to read facts from")
    return counts
