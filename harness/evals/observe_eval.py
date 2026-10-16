"""Eval after the observation agent (the hatched box on the whiteboard's second row): time-travel recall.

Observe's job is to surface the story the account should join before the story peaks. Replaying the
dev-tool dataset tests that without posting access: at a cutoff time T, orient sees only posts from
the 24 hours before T, with no engagement counts (those are final values and would leak the
outcome). A story is live at T if it already has a post before T and at least two other accounts
post about it in the 24 hours after. Recall is the share of live stories whose keywords appear in
the top-k narratives orient returns.

Stories come from eval/fixtures/devtools_stories.json: model launches that several dev-tool
accounts posted about, found by matching "Name version" phrases across accounts. The list is
built with hindsight on purpose; it is the answer key, and orient never sees it.

A naive observer that turns the k most recent posts into narratives sets the floor orient must beat.
When orient fails at a cutoff (it returned unparseable JSON on 70-signal windows in the first run),
the cutoff counts as a miss and the error is recorded.
"""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from harness import config
from harness.evals.dataset import Post
from harness.evals.predictors import with_retries
from harness.llm import LLM
from harness.models import Narrative, Signal
from harness.orient import orient

STORIES_PATH = config.ROOT / "eval" / "fixtures" / "devtools_stories.json"
MISSION_PATH = config.ROOT / "eval" / "fixtures" / "devtools_mission.md"


def load_stories(path: Path = STORIES_PATH) -> dict[str, list[str]]:
    stories = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(stories, dict) or not all(isinstance(v, list) and v for v in stories.values()):
        raise ValueError(f"{path} must map each story id to a non-empty list of lowercase keywords")
    return stories


def mentions(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def live_stories(posts: list[Post], stories: dict[str, list[str]], t: datetime, window_h: float = 24,
                 min_accounts_after: int = 2) -> list[str]:
    window = timedelta(hours=window_h)
    out = []
    for sid, keys in stories.items():
        on_story = [p for p in posts if mentions(p.clean_text, keys)]
        before = [p for p in on_story if t - window <= p.created_at < t]
        after = {p.account for p in on_story if t <= p.created_at < t + window}
        if before and len(after - {p.account for p in before}) >= min_accounts_after:
            out.append(sid)
    return out


def cutoffs(posts: list[Post], stories: dict[str, list[str]], lag_h: float = 1.0) -> list[datetime]:
    """One cutoff per story, an hour after its first post: early enough that the story hasn't peaked."""
    out = set()
    for keys in stories.values():
        times = [p.created_at for p in posts if mentions(p.clean_text, keys)]
        if times:
            out.add(min(times) + timedelta(hours=lag_h))
    return sorted(out)


def signals_before(posts: list[Post], t: datetime, window_h: float = 24) -> list[Signal]:
    """Posts from the window before t as Signals, without metrics: the CSV's counts are final values."""
    window = timedelta(hours=window_h)
    return [Signal(source="devtools_csv", text=p.clean_text, url=p.id, author=p.account,
                   created_at=p.created_at.isoformat()) for p in sorted(posts, key=lambda p: p.created_at)
            if t - window <= p.created_at < t]


def naive_narratives(signals: list[Signal], k: int) -> list[Narrative]:
    """The floor: the k most recent posts, each taken as its own narrative."""
    recent = sorted(signals, key=lambda s: s.created_at or "", reverse=True)[:k]
    return [Narrative(id=f"recent-{i}", title=s.text[:80], summary=s.text) for i, s in enumerate(recent)]


def surfaced(narratives: list[Narrative], keys: list[str]) -> bool:
    return any(mentions(f"{n.title} {n.summary} {n.angle}", keys) for n in narratives)


def time_travel(llm: LLM, posts: list[Post], stories: dict[str, list[str]], mission: str, top: int = 5,
                window_h: float = 24) -> dict:
    rows = []
    for t in cutoffs(posts, stories):
        live = live_stories(posts, stories, t, window_h)
        if not live:
            continue
        signals = signals_before(posts, t, window_h)
        naive = naive_narratives(signals, top)
        row = {"cutoff": t.isoformat(), "n_signals": len(signals), "live": live,
               "naive_surfaced": [s for s in live if surfaced(naive, stories[s])]}
        try:
            narratives = with_retries(orient, attempts=2)(llm, mission, signals, top)
        except Exception as e:  # noqa: BLE001 - an orient failure is a result here, counted as a miss
            row.update(surfaced=[], error=f"{type(e).__name__}: {str(e)[:200]}", narratives=[])
        else:
            row.update(surfaced=[s for s in live if surfaced(narratives, stories[s])],
                       narratives=[asdict(n) for n in narratives])
        rows.append(row)
    n_live = sum(len(r["live"]) for r in rows)
    return {
        "top": top, "window_h": window_h, "n_cutoffs": len(rows), "n_live": n_live,
        "n_orient_errors": sum("error" in r for r in rows),
        "recall": sum(len(r["surfaced"]) for r in rows) / n_live if n_live else None,
        "naive_recall": sum(len(r["naive_surfaced"]) for r in rows) / n_live if n_live else None,
        "rows": rows,
    }
