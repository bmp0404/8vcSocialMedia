"""Rules the engine applies across runs, where a single run can't see the problem.

1. **One promotion per rolling 7 days** (`weekly_cap`). Every run is its own Benjamini-Hochberg family,
   so a loop that runs daily would otherwise spend a fresh 10% false-discovery budget each day on the
   same frozen items. eval/experiments/DESIGN.md says why this rule and not BH across the week.
2. **Confirmation only on items rooted after the proposal** (`rooted_after`). A change chosen while
   looking at a set of items is confirmed only on items that didn't exist yet, so neither a person
   nor a proposer can have tuned it to them.
3. **Stacked promotions are measured together** (`unmeasured`). If the champion sets an instrument's
   knobs to a combination no decided run on that instrument scored, the next run on it measures the
   combination first.

`Restricted` wraps an instrument to apply rule 2 and to tighten its guardrails for one experiment.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from harness.iterate.fitness import Item, Metric
from harness.iterate.knobs import DEFAULT, Config

WINDOW = timedelta(days=7)
# A revert restores values the champion already had, and a confirmation re-promotes a change already in
# force, so neither adds a change for the cap to count.
UNCOUNTED_KINDS = frozenset({"revert", "confirmation"})
# Statuses whose arms were really scored: a stack check counts these as measurements of a config.
MEASURED = frozenset({"promoted", "rejected", "inconclusive", "candidate", "stack_ok", "stack_failed"})


def decided_at(record: dict) -> datetime:
    at = record["decision"].get("at") or record["created_at"]
    t = datetime.fromisoformat(at)
    if t.tzinfo is None:
        raise ValueError(f"{record['id']} has a decision time without a timezone: {at!r}")
    return t


def _aware(t: datetime, what: str) -> datetime:
    if t.tzinfo is None:
        raise ValueError(f"{what} must carry a timezone, got {t.isoformat()}")
    return t


@dataclass(frozen=True)
class Cap:
    open: bool
    holder: str | None = None  # the promotion that closed the window
    opens_at: datetime | None = None


def counted_promotions(records: list[dict]) -> list[dict]:
    """Promotions that added a change to the champion: neither reverts nor confirmations."""
    return [r for r in records if r["decision"]["status"] == "promoted"
            and r["proposer"].get("kind") not in UNCOUNTED_KINDS]


def weekly_cap(records: list[dict], now: datetime) -> Cap:
    """Closed while any counted promotion was decided within the 7 days before `now`."""
    now = _aware(now, "now")
    recent = [r for r in counted_promotions(records) if decided_at(r) > now - WINDOW]
    if not recent:
        return Cap(True)
    last = max(recent, key=decided_at)
    return Cap(False, last["id"], decided_at(last) + WINDOW)


def _need_times(items: list[Item]) -> None:
    missing = [it.id for it in items if it.root_at is None]
    if missing:
        raise ValueError(f"{len(missing)} item(s) have no root time (first: {missing[:3]}); an instrument has to "
                         "date its items before an experiment on it can be confirmed")


def rooted_after(items: list[Item], cutoff: datetime) -> list[Item]:
    """Items whose root post went out strictly after `cutoff`: the only ones a confirmation may use."""
    cutoff = _aware(cutoff, "the confirmation cutoff")
    _need_times(items)
    return [it for it in items if it.root_at > cutoff]


def rooted_by(items: list[Item], cutoff: datetime) -> list[Item]:
    """Items rooted at or before `cutoff`: what existed when the experiment was proposed."""
    cutoff = _aware(cutoff, "the screening cutoff")
    _need_times(items)
    return [it for it in items if it.root_at <= cutoff]


class Restricted:
    """An instrument limited to items rooted after (or by) a time, with guardrails at least as strict as its own.

    `guardrails` maps a secondary metric's name to the most it may get worse. A metric that is already a
    guardrail keeps the stricter of the two margins; asking for a looser one raises, so a spec can only
    tighten the ruler.
    """

    def __init__(self, fit, *, after: datetime | None = None, by: datetime | None = None,
                 guardrails: dict[str, float] | None = None):
        if after is not None and by is not None:
            raise ValueError("pass after= (a confirmation) or by= (a screen), not both")
        self.fit = fit
        self.after = _aware(after, "after") if after is not None else None
        self.by = _aware(by, "by") if by is not None else None
        for attr in ("name", "stage", "unit", "cluster_unit", "primary", "knobs", "llm_opinion"):
            setattr(self, attr, getattr(fit, attr))
        self.guardrails = dict(guardrails or {})
        self.secondaries = tuple(self._tighten(m) for m in fit.secondaries)
        unknown = set(self.guardrails) - {m.name for m in fit.secondaries}
        if unknown:
            raise ValueError(f"{fit.name} has no secondary metric {sorted(unknown)}; "
                             f"choose from {[m.name for m in fit.secondaries]}")

    def _tighten(self, m: Metric) -> Metric:
        if m.name not in self.guardrails:
            return m
        margin = float(self.guardrails[m.name])
        if margin < 0:
            raise ValueError(f"guardrail {m.name}: a margin can't be negative, got {margin}")
        if m.guardrail and margin > m.margin:
            raise ValueError(f"guardrail {m.name}: {margin} is looser than the instrument's own {m.margin}")
        return replace(m, guardrail=True, margin=margin)

    def items(self) -> list[Item]:
        items = self.fit.items()
        if self.after is not None:
            return rooted_after(items, self.after)
        if self.by is not None:
            return rooted_by(items, self.by)
        return items

    def instrument(self) -> dict:
        return {**self.fit.instrument(),
                "items_rooted_after": self.after.isoformat() if self.after else None,
                "items_rooted_by": self.by.isoformat() if self.by else None,
                "guardrails": {m.name: m.margin for m in self.secondaries if m.guardrail}}

    def run_arm(self, *args, **kw):
        return self.fit.run_arm(*args, **kw)

    def stat(self, *args, **kw):
        return self.fit.stat(*args, **kw)

    def estimate_calls(self, *args, **kw):
        return self.fit.estimate_calls(*args, **kw)

    def __getattr__(self, name: str) -> Any:  # mock_respond and anything else an instrument adds
        return getattr(self.fit, name)


@dataclass(frozen=True)
class Stack:
    """A fitness whose knobs the champion sets to a combination no decided run on it measured."""

    fitness: str
    baseline: Config  # the champion with this fitness's knobs as they were before `latest`
    champion: Config
    latest: str  # the most recent promotion (or revert) that moved one of the fitness's knobs
    latest_by: str  # "auto" or "human": who decided it


def _project(cfg: Config, knobs: tuple[str, ...]) -> dict:
    return {k: cfg.values[k] for k in knobs}


def unmeasured(records: list[dict], configs: dict[str, Config], fit_knobs: dict[str, tuple[str, ...]],
               champion: Config) -> list[Stack]:
    """For each fitness, whether the champion's values for its knobs were ever an arm in a decided run on it.

    `records` must be in decision order (ledger.records). Defaults count as measured only if a run scored
    them too; a fitness whose knobs are all at their defaults is skipped, since no promotion moved them.
    """
    out = []
    for name, ks in fit_knobs.items():
        want = _project(champion, ks)
        if want == _project(DEFAULT, ks):
            continue
        measured = any(
            r["eval"].get("fitness") == name and r["decision"]["status"] in MEASURED and r["eval"].get("metrics")
            and any((c := configs.get(r[side]["ref"])) is not None and _project(c, ks) == want
                    for side in ("champion", "challenger"))
            for r in records)
        if measured:
            continue
        # Replay promotions to find the last one that moved this fitness's knobs and the config before it.
        cfg, latest, before = DEFAULT, None, None
        for r in records:
            if r["decision"]["status"] != "promoted":
                continue
            after = cfg.with_changes({k: v["to"] for k, v in r["change"]["params"].items()})
            if _project(after, ks) != _project(cfg, ks):
                latest, before = r, cfg
            cfg = after
        if latest is None:
            raise ValueError(f"the champion moved {name}'s knobs but no promoted record did; is the ledger complete?")
        baseline = champion.with_changes(_project(before, ks))
        out.append(Stack(name, baseline, champion, latest["id"], latest["decision"].get("by", "auto")))
    return out
