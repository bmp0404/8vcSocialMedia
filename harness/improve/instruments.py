"""The instrument ledger, eval/experiments/instruments.json: does each measuring instrument predict real
outcomes it never saw, and may it still gate promotions?

An instrument is anything the loop measures with: the reply ranker, the TF-IDF post ranker, the Claude
label model, the lineup AI detector, the action veto, the slop gate (harness/minds/gate.py) and the
audience panel. Each is scored against outcomes after its cutoff (harness/improve/outcomes.py says
which): `_sgu_`'s real replies first, then threads rooted after the cutoff, each source kept apart.

**The rule.** An instrument may gate promotions while its interval is above chance or it has fewer than
20 outcomes. At 20 or more, an interval that includes chance (or sits below it) makes it report-only,
and the ledger records when and why. The deciding source is the first with 20 or more outcomes, in the
order `sgu`, `threads`: real results outrank everything else. A source whose interval is undefined (a
screen passes every `_sgu_` reply, since each one shipped through it) can't judge, and the next decides. "Outcomes" counts the resampling unit, so
fifty pairs from one thread are one outcome, not fifty. The status follows the current evidence, so an
instrument whose interval later clears chance gates again, and that is recorded too. The panel is
report-only whatever its numbers (B3: it ties the free TF-IDF ridge at one LLM call per draft).

**Cutoffs and versions.** A fitted model's cutoff is the newest root it trained on. A prompt or rule
instrument's cutoff is the moment the ledger first saw its current version, because nothing records
which data a person looked at while tuning a prompt. A version is a hash of what defines the
instrument (its prompt, model, rules or training cutoff); a new version starts a new cutoff, and the
old version's numbers stay in the history.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harness.iterate.ledger import EXPERIMENTS_DIR

LEDGER_PATH = EXPERIMENTS_DIR / "instruments.json"
MIN_N = 20
SOURCES = ("sgu", "threads")  # the order the rule consults them
GATES, REPORT_ONLY = "gates", "report_only"


@dataclass(frozen=True)
class Score:
    source: str  # "sgu" or "threads"
    metric: str  # "spearman" or "pairwise_accuracy"
    chance: float
    n: int  # outcomes: _sgu_ replies, or threads (accounts for post pairs)
    items: int  # replies or pairs behind them
    estimate: float | None
    ci95: tuple[float, float] | None
    coverage: float | None  # share of items the instrument scored; abstentions count as its worst rank
    ids_sha256: str  # what was scored, so the history grows only when the outcomes do
    detail: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("estimate", "coverage"):
            d[k] = round(d[k], 4) if d[k] is not None else None
        d["ci95"] = [round(v, 4) for v in self.ci95] if self.ci95 else None
        return d


def ids_digest(ids) -> str:
    return hashlib.sha256(",".join(sorted(map(str, ids))).encode()).hexdigest()


def empty(source: str, metric: str, chance: float, why: str) -> Score:
    return Score(source, metric, chance, 0, 0, None, None, None, ids_digest([]), {"note": why})


def decide(scores: dict[str, Score], role: str = "gate") -> tuple[str, str]:
    """(status, reason) under the rule in the module docstring. Pure."""
    if role == "report":
        return REPORT_ONLY, "report-only by design; its numbers are recorded but never gate a promotion"
    unjudged = []
    for source in SOURCES:
        s = scores.get(source)
        if s is None or s.n < MIN_N:
            continue
        where = f"{source}: {s.metric} on {s.n} outcomes"
        if s.ci95 is None:
            # Every _sgu_ reply passed the screen that shipped it, so the scores can't vary: that source
            # can't judge the screen, and the next one decides.
            unjudged.append(f"{source} can't judge it (its scores or outcomes don't vary)")
            continue
        lo, hi = s.ci95
        if lo > s.chance:
            return GATES, f"{where}, {s.estimate:.3f} [{lo:.3f}, {hi:.3f}], above chance ({s.chance:g})"
        side = "below" if hi < s.chance else "includes"
        return REPORT_ONLY, f"{where}, {s.estimate:.3f} [{lo:.3f}, {hi:.3f}], {side} chance ({s.chance:g})"
    counts = ", ".join(f"{src} {scores[src].n}" for src in SOURCES if src in scores) or "none"
    return GATES, ("; ".join(unjudged + [""]) + f"no source with {MIN_N} or more outcomes can judge it ({counts}); "
                   "it gates until one can")


def load(path: Path = LEDGER_PATH) -> dict:
    if not Path(path).is_file():
        return {"instruments": {}}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(ledger: dict, path: Path = LEDGER_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger = {"note": "written by harness/improve/instruments.py each cycle of `harness improve`; do not edit. "
                      "harness/improve/DESIGN.md explains the rule.",
              "rule": {"min_n": MIN_N, "sources_in_order": list(SOURCES)}, **ledger}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def first_seen(ledger: dict, name: str, version: str, now: datetime) -> datetime:
    """When the ledger first saw this version of the instrument; `now` for a version it hasn't seen."""
    entry = ledger.get("instruments", {}).get(name)
    if entry and entry.get("version") == version and entry.get("version_since"):
        return datetime.fromisoformat(entry["version_since"])
    return now


def record(ledger: dict, name: str, *, role: str, what: str, version: str, cutoff: datetime, cutoff_basis: str,
           scores: dict[str, Score], now: datetime, fit_evidence: dict | None = None) -> list[dict]:
    """Update one instrument's entry in place. Returns the events this update caused (version or status changes)."""
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    instruments = ledger.setdefault("instruments", {})
    old = instruments.get(name) or {}
    status, reason = decide(scores, role)
    events = list(old.get("events", []))
    new_events = []
    new_version = old.get("version") != version
    if new_version:
        new_events.append({"at": stamp, "event": "new version" if old else "first seen", "version": version,
                           "cutoff": cutoff.isoformat(), "why": cutoff_basis})
    if old.get("status") != status:
        new_events.append({"at": stamp, "event": "status", "from": old.get("status"), "to": status, "why": reason})
    history = list(old.get("history", []))
    for src, s in scores.items():
        prev = (old.get("scores") or {}).get(src) or {}
        if new_version or prev.get("ids_sha256") != s.ids_sha256:
            history.append({"at": stamp, "version": version, "source": src, "n": s.n, "items": s.items,
                            "estimate": None if s.estimate is None else round(s.estimate, 4),
                            "ci95": [round(v, 4) for v in s.ci95] if s.ci95 else None})
    instruments[name] = {
        "role": role, "what": what, "version": version,
        "version_since": stamp if new_version else old.get("version_since", stamp),
        "cutoff": cutoff.isoformat(), "cutoff_basis": cutoff_basis,
        "status": status, "reason": reason,
        "status_since": stamp if old.get("status") != status else old.get("status_since", stamp),
        "scores": {src: s.to_json() for src, s in scores.items()},
        **({"fit_evidence": fit_evidence} if fit_evidence else {}),
        "history": history, "events": events + new_events,
    }
    ledger["updated_at"] = stamp
    return [{"instrument": name, **e} for e in new_events]
