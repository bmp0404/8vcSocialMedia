"""Proposers: where the next experiments come from. Both may only change registered knobs.

- RuleProposer walks the knob registry from the champion one knob at a time. It starts with
  research-motivated changes, skips anything the ledger already settled against an equivalent
  champion, and stops at a small family size, because every extra comparison in a run makes the
  Benjamini-Hochberg bar higher for all of them.
- LLMProposer shows a model the registry, the ledger and the research findings, and asks for the
  next experiments as JSON. It never writes code: `validate` rejects any proposal that names an
  unknown fitness or knob, sets a value outside a knob's domain, changes a knob its fitness can't
  measure, or repeats a settled experiment. Rejections are kept with their reasons.

Settled means decided as promoted, rejected, inconclusive or candidate against a champion that agrees on
every knob the fitness reads. Pending and superseded records don't count, so unrun proposals come back. A
candidate counts as settled because screening it again on the same items is the overfitting a confirmation
on later items exists to avoid.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from harness.iterate import knobs
from harness.iterate.knobs import BY_PATH, Config, KnobError
from harness.iterate.runner import Proposal

SETTLED = {"promoted", "rejected", "inconclusive", "candidate"}
DEFAULT_MAX = 3  # research/eval.md finding 13 and the eval worker: spend the BH family on few comparisons


@dataclass
class Rejected:
    proposal: dict
    reason: str


@dataclass
class Batch:
    accepted: list[Proposal] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


def _canon(changes: Mapping[str, Any]) -> str:
    return knobs.canonical_json({k: knobs._jsonable(v) for k, v in changes.items()})


def is_settled(prop: Proposal, champion: Config, records: list[dict], fit_knobs: Mapping[str, tuple[str, ...]],
               configs: Mapping[str, Config], datasets: Mapping[str, str] | None = None) -> bool:
    """True when the ledger already decided this exact change against a champion that agrees with the
    current one on every knob the fitness reads, on the same dataset. Without the old champion's
    config, compare only the changed knobs' starting values. A grown dataset (more threads or
    accounts) unsettles it: more items is how an inconclusive result gets the power to decide."""
    want = _canon({k: champion.with_changes(prop.changes).values[k] for k in prop.changes})
    for r in records:
        if r["decision"]["status"] not in SETTLED or r["eval"].get("fitness") != prop.fitness:
            continue
        if _canon({k: v["to"] for k, v in r["change"]["params"].items()}) != want:
            continue
        if datasets and datasets.get(prop.fitness) not in (None, r["eval"].get("dataset_sha256")):
            continue
        past = configs.get(r["champion"]["ref"])
        if past is not None:
            if all(past.values[k] == champion.values[k] for k in fit_knobs[prop.fitness]):
                return True
        elif all(knobs._jsonable(champion.values[k]) == v["from"] for k, v in r["change"]["params"].items()):
            return True
    return False


def validate(raw: list[dict], champion: Config, records: list[dict], fit_knobs: Mapping[str, tuple[str, ...]],
             proposer: dict, limit: int = DEFAULT_MAX, configs: Mapping[str, Config] | None = None,
             datasets: Mapping[str, str] | None = None) -> Batch:
    """Turn raw proposals into runnable ones, rejecting anything outside the registry with a reason."""
    batch = Batch()
    seen: set[tuple] = set()
    for item in raw:
        try:
            if not isinstance(item, dict):
                raise KnobError(f"a proposal must be a JSON object, got {type(item).__name__}")
            fit, changes = item.get("fitness"), item.get("changes")
            if fit not in fit_knobs:
                raise KnobError(f"unknown fitness {fit!r}; choose from {sorted(fit_knobs)}")
            if not isinstance(changes, dict) or not 1 <= len(changes) <= 2:
                raise KnobError("changes must set one or two knobs")
            unmeasured = set(changes) - set(fit_knobs[fit])
            if unmeasured:
                raise KnobError(f"{fit} can't measure {sorted(unmeasured)}")
            challenger = champion.with_changes(changes)  # raises on unknown knobs and bad values
            if challenger.hash == champion.hash:
                raise KnobError("every change equals the champion's value")
            hypothesis = str(item.get("hypothesis") or "").strip()
            if not hypothesis:
                raise KnobError("a proposal needs a hypothesis")
            prop = Proposal(fit, dict(changes), hypothesis,
                            {**proposer, "source": str(item.get("source") or "") or None}, str(item.get("label") or ""))
            if is_settled(prop, champion, records, fit_knobs, configs or {}, datasets):
                raise KnobError("the ledger already decided this change against an equivalent champion")
            key = (fit, challenger.hash)
            if key in seen:
                raise KnobError("duplicate of an earlier proposal in this batch")
            if len(batch.accepted) >= limit:
                raise KnobError(f"over the batch limit of {limit} comparisons")
            seen.add(key)
            batch.accepted.append(prop)
        except KnobError as e:
            batch.rejected.append(Rejected(item if isinstance(item, dict) else {"raw": repr(item)}, str(e)))
    return batch


class RuleProposer:
    """One-knob coordinate search from the champion, research priors first."""

    def __init__(self, priors: list[Proposal], limit: int = DEFAULT_MAX, int_steps: int = 1):
        self.priors, self.limit, self.int_steps = priors, limit, int_steps

    def candidates(self, champion: Config, fit_knobs: Mapping[str, tuple[str, ...]]) -> list[dict]:
        out, seen = [], set()
        for p in self.priors:
            key = (p.fitness, _canon(p.changes))
            if key not in seen:
                seen.add(key)
                out.append({"fitness": p.fitness, "changes": p.changes, "hypothesis": p.hypothesis,
                            "source": p.proposer.get("source"), "label": p.label})
        for fit, paths in fit_knobs.items():
            for path in paths:
                k = BY_PATH[path]
                current = champion.values[path]
                if k.kind is str and k.choices:
                    values = [c for c in k.choices if c != current]
                elif k.kind is int and k.lo is not None and k.hi is not None:
                    step = max(1, round((k.hi - k.lo) / 8))
                    values = [v for v in (current - step, current + step) if k.lo <= v <= k.hi]
                else:
                    continue  # sets and floats need a prior; blind search over them wastes the family
                for v in values:
                    out.append({"fitness": fit, "changes": {path: v},
                                "hypothesis": f"Coordinate search: {path} {current!r} -> {v!r} moves {fit}.",
                                "source": "rule: one-knob neighbourhood of the champion"})
        return out

    def propose(self, champion: Config, records: list[dict], fit_knobs: Mapping[str, tuple[str, ...]],
                configs: Mapping[str, Config] | None = None, datasets: Mapping[str, str] | None = None) -> Batch:
        raw = self.candidates(champion, fit_knobs)
        batch = validate(raw, champion, records, fit_knobs, {"kind": "rule"}, limit=self.limit, configs=configs,
                         datasets=datasets)
        # Running out of room isn't a defect in the candidate; keep those out of the rejected list.
        batch.rejected = [r for r in batch.rejected if not r.reason.startswith(("over the batch limit", "duplicate"))]
        return batch


LLM_SYSTEM = """You plan experiments for a media harness that tunes its own configuration. You may only propose
changes to the registered knobs listed below, measured by one of the listed fitness functions. You never
propose code changes. Each experiment is tested against the current champion on the same items with a paired
bootstrap, Benjamini-Hochberg across the batch and a noise floor, so every extra experiment raises the bar
for all of them: propose at most {limit}, and only changes you have a specific reason to expect to matter.
Prefer changes the research findings or ledger results motivate; say which one in "source". Don't repeat an
experiment the ledger already decided against an equivalent champion unless the champion changed since.

Return JSON: {{"proposals": [{{"fitness": "...", "changes": {{"<knob path>": <value>}}, "hypothesis": "one
sentence a result could prove wrong", "source": "research/<file>.md finding N, or a ledger id", "label": "short"}}]}}"""


def ledger_summary(records: list[dict], limit: int = 30) -> list[dict]:
    out = []
    for r in records[-limit:]:
        primary = next((m for m in r["eval"].get("metrics", []) if m.get("primary")), None)
        out.append({"id": r["id"], "fitness": r["eval"].get("fitness"), "change": r["change"]["params"],
                    "status": r["decision"]["status"], "reason": r["decision"]["reason"][:300],
                    "metric": None if not primary else {
                        "name": primary["name"], "higher_is_better": primary["higher_is_better"],
                        "champion": primary["champion"]["value"], "challenger": primary["challenger"]["value"],
                        "delta_ci95": primary["delta"]["ci95"]},
                    "secondary": [{"name": m["name"], "delta": m["delta"]["value"], "ci95": m["delta"]["ci95"]}
                                  for m in r["eval"].get("metrics", []) if not m.get("primary")]})
    return out


class LLMProposer:
    def __init__(self, llm, findings: list[dict], limit: int = DEFAULT_MAX):
        self.llm, self.findings, self.limit = llm, findings, limit

    def prompt(self, champion: Config, records: list[dict], fit_knobs: Mapping[str, tuple[str, ...]]) -> str:
        measurable = {p for paths in fit_knobs.values() for p in paths}
        registry = [{"knob": k.path, "current": knobs._jsonable(champion.values[k.path]), "domain": k.domain(),
                     "help": k.help, "measured_by": [f for f, ps in fit_knobs.items() if k.path in ps]}
                    for k in knobs.REGISTRY if k.path in measurable]
        return (f"KNOBS (current champion {champion.ref}):\n{json.dumps(registry, indent=1)}\n\n"
                f"LEDGER (oldest first):\n{json.dumps(ledger_summary(records), indent=1)}\n\n"
                f"RESEARCH FINDINGS:\n{json.dumps(self.findings, indent=1)}")

    def propose(self, champion: Config, records: list[dict], fit_knobs: Mapping[str, tuple[str, ...]],
                configs: Mapping[str, Config] | None = None, datasets: Mapping[str, str] | None = None) -> Batch:
        fits = sorted(fit_knobs)

        def mock() -> dict:
            first = fit_knobs[fits[0]][0]
            k = BY_PATH[first]
            other = next((c for c in k.choices if c != champion.values[first]), None) if k.choices else None
            return {"proposals": [
                {"fitness": fits[0], "changes": {first: other}, "hypothesis": "mock proposal", "source": "mock"},
                {"fitness": fits[0], "changes": {"action.temperature": 0.7}, "hypothesis": "an invalid knob",
                 "source": "mock"}]}

        out = self.llm.complete_json(LLM_SYSTEM.format(limit=self.limit), self.prompt(champion, records, fit_knobs),
                                     mock=mock)
        raw = out.get("proposals", []) if isinstance(out, dict) else []
        model = getattr(self.llm, "model", "unknown")
        return validate(raw, champion, records, fit_knobs, {"kind": "llm", "model": model}, limit=self.limit,
                        configs=configs, datasets=datasets)


def findings_from_plans() -> list[dict]:
    """The research findings the plans cite, as the LLM proposer's evidence. Each is a hypothesis with a
    source, not a fact: the ledger is what says whether it held here."""
    from harness.iterate.plans import PLANS

    seen, out = set(), []
    for props in PLANS.values():
        for p in props:
            if p.hypothesis not in seen:
                seen.add(p.hypothesis)
                out.append({"source": p.proposer.get("source"), "claim": p.hypothesis})
    return out


def to_json(batch: Batch) -> dict[str, Any]:
    return {"accepted": [{"fitness": p.fitness, "changes": p.changes, "hypothesis": p.hypothesis,
                          "proposer": p.proposer, "label": p.label} for p in batch.accepted],
            "rejected": [{"proposal": r.proposal, "reason": r.reason} for r in batch.rejected]}
