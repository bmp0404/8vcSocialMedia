"""Experiment specs in eval/experiments/queue/: what an agent or a person asks the loop to test.

One TOML file per experiment, named `<id>.toml`:

    id = "styled-reply"
    proposed_at = 2026-10-16T14:00:00Z    # with a timezone; confirmation uses items rooted after it
    proposed_by = "sam"                   # a person, or "agent:<name>"
    profile = "wnba"                      # the mission profile the instrument runs under
    fitness = "action_lineup"
    hypothesis = "Short style rules read less like AI than the persona JSON call."
    source = "research/action.md, findings 1 and 2"   # optional
    label = "styled prompt"                           # optional
    threads = ["action/fixtures/x_threads.json"]      # optional, action_lineup only: fixture threads to screen on

    [change]
    kind = "knobs"                        # or "code"
    knobs = { "action.reply.generation" = "styled" }
    # kind = "code" also needs: commit = "<sha in this checkout's history>", summary = "what it does"

    [guardrails]                          # optional; tightens the instrument's own, never loosens
    veto_pass_rate = 0.05

    [budget]                              # optional; LLM calls per phase
    screen_calls = 400
    confirm_calls = 400

A code change is tested through the knob that switches it on, because the engine compares configs it
can hash, diff and revert; the commit must already be in the checkout the loop runs from, so the arm
really runs that code. Anything else in the file is an error, so a typo can't silently drop a guardrail.

Each spec goes through two phases, both recorded in the ledger with `proposer.spec` set to its id and
`proposer.spec_digest` to a hash of what it tests: a screen on items rooted at or before the proposal,
then, if it passed (a `candidate`), a confirmation on items rooted after it. The cutoff is the later of
`proposed_at` and when the loop first saw this version of the file, so a backdated spec can't confirm
on items its author had already seen. Editing what a spec tests gives it a new digest, so the edited
spec is screened from scratch rather than confirmed on the old screen.
"""

import hashlib
import json

import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harness import config
from harness.iterate.knobs import DEFAULT, KnobError

QUEUE_DIR = config.ROOT / "eval" / "experiments" / "queue"
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
TOP_KEYS = {"id", "proposed_at", "proposed_by", "profile", "fitness", "hypothesis", "source", "label", "change",
            "guardrails", "budget", "threads"}
REQUIRED = ("id", "proposed_at", "proposed_by", "profile", "fitness", "hypothesis", "change")
CHANGE_KEYS = {"kind", "knobs", "commit", "summary"}
BUDGET_KEYS = {"screen_calls", "confirm_calls"}
DEFAULT_CALLS = 400
MAX_CALLS = 2000  # one phase may not spend more than a whole batch's allowance
# Confirmation needs this many clusters (threads or accounts) rooted after the proposal. The instrument
# ledger demotes at n >= 20 for the same reason: with fewer clusters a bootstrap interval is too wide to
# clear a noise floor. Policy, not measured.
MIN_CONFIRM_CLUSTERS = 20
# A confirmation uses the first 30 clusters rooted after the cutoff, and a screen the 40 rooted closest
# before it, so a phase's cost stops growing with the watcher. Policy, not measured.
CONFIRM_CLUSTERS = 30
SCREEN_CLUSTERS = 40
CLOCK_SKEW = timedelta(minutes=10)  # how far in the future a proposal time may be


class SpecError(ValueError):
    """A queue file the loop refuses to run, with the reason."""


@dataclass(frozen=True)
class Spec:
    id: str
    path: Path
    proposed_at: datetime
    proposed_by: str
    profile: str
    fitness: str
    hypothesis: str
    knobs: dict[str, Any]
    kind: str  # "knobs" or "code"
    source: str | None = None
    label: str = ""
    commit: str | None = None
    summary: str | None = None
    guardrails: dict[str, float] = field(default_factory=dict)
    threads: tuple[Path, ...] = ()  # fixture thread files, for instruments that take them
    screen_calls: int = DEFAULT_CALLS
    confirm_calls: int = DEFAULT_CALLS

    @property
    def digest(self) -> str:
        """A hash of everything that decides what the spec tests, but not its prose."""
        what = {"fitness": self.fitness, "profile": self.profile, "knobs": self.knobs, "kind": self.kind,
                "commit": self.commit, "guardrails": self.guardrails, "threads": [p.name for p in self.threads]}
        return hashlib.sha256(json.dumps(what, sort_keys=True).encode()).hexdigest()[:12]

    def proposer(self, phase: str, cutoff: datetime) -> dict:
        return {"kind": "queue", "spec": self.id, "spec_digest": self.digest, "phase": phase, "source": self.source,
                "by": self.proposed_by, "proposed_at": cutoff.isoformat(timespec="seconds"),
                **({"commit": self.commit, "summary": self.summary} if self.kind == "code" else {})}


def _datetime(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as e:
            raise SpecError(f"proposed_at {value!r} is not an ISO date-time") from e
    if not isinstance(value, datetime):
        raise SpecError(f"proposed_at must be a date-time, got {value!r}")
    if value.tzinfo is None:
        raise SpecError("proposed_at needs a timezone (end it with Z for UTC)")
    return value.astimezone(timezone.utc)


def _in_history(commit: str, repo: Path) -> bool:
    out = subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=repo, capture_output=True)
    return out.returncode == 0


def parse(path: Path, catalog: dict[str, type], *, repo: Path = config.ROOT, now: datetime | None = None) -> Spec:
    """Read and check one spec against the instruments in `catalog` (fitness name -> class). Raises SpecError."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f"{path.name} is not valid TOML: {e}") from e
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise SpecError(f"{path.name}: unknown key(s) {sorted(unknown)}; allowed: {sorted(TOP_KEYS)}")
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise SpecError(f"{path.name}: missing {missing}")
    sid = data["id"]
    if not isinstance(sid, str) or not ID.match(sid):
        raise SpecError(f"{path.name}: id {sid!r} must be lowercase letters, digits and dashes")
    if path.stem != sid:
        raise SpecError(f"{path.name}: the file must be named {sid}.toml")
    for key in ("proposed_by", "profile", "fitness", "hypothesis"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise SpecError(f"{sid}: {key} must be a non-empty string")
    try:
        config.profile_dir(data["profile"])
    except config.ProfileError as e:
        raise SpecError(f"{sid}: {e}") from e

    fit = catalog.get(data["fitness"])
    if fit is None:
        raise SpecError(f"{sid}: unknown fitness {data['fitness']!r}; choose from {sorted(catalog)}")
    if not hasattr(fit, "llm_opinion"):
        raise SpecError(f"{sid}: {data['fitness']} doesn't declare llm_opinion, so the loop can't tell whether it "
                        "grades against real outcomes")
    if fit.llm_opinion:
        raise SpecError(f"{sid}: {data['fitness']} scores a model's opinion, and no LLM opinion score is ever a fitness")

    change = data["change"]
    if not isinstance(change, dict):
        raise SpecError(f"{sid}: [change] must be a table")
    if set(change) - CHANGE_KEYS:
        raise SpecError(f"{sid}: unknown [change] key(s) {sorted(set(change) - CHANGE_KEYS)}")
    kind = change.get("kind")
    if kind not in ("knobs", "code"):
        raise SpecError(f"{sid}: change.kind must be \"knobs\" or \"code\", got {kind!r}")
    knob_values = change.get("knobs")
    if not isinstance(knob_values, dict) or not 1 <= len(knob_values) <= 2:
        raise SpecError(f"{sid}: change.knobs must set one or two knobs"
                        + (" (a code change names the knob that switches it on)" if kind == "code" else ""))
    unmeasured = set(knob_values) - set(fit.knobs)
    if unmeasured:
        raise SpecError(f"{sid}: {data['fitness']} can't measure {sorted(unmeasured)}; it reads {list(fit.knobs)}")
    try:
        DEFAULT.with_changes(knob_values)
    except KnobError as e:
        raise SpecError(f"{sid}: {e}") from e
    commit = summary = None
    if kind == "code":
        commit, summary = change.get("commit"), change.get("summary")
        if not isinstance(commit, str) or not COMMIT.match(commit):
            raise SpecError(f"{sid}: a code change needs commit = \"<7 to 40 hex digits>\"")
        if not isinstance(summary, str) or not summary.strip():
            raise SpecError(f"{sid}: a code change needs a summary of what the code does")
        if not _in_history(commit, repo):
            raise SpecError(f"{sid}: commit {commit} isn't in this checkout's history, so the arm wouldn't run it; "
                            "merge it first")
    elif {"commit", "summary"} & set(change):
        raise SpecError(f"{sid}: commit and summary belong to kind = \"code\"")

    guardrails = data.get("guardrails", {})
    if not isinstance(guardrails, dict):
        raise SpecError(f"{sid}: [guardrails] must be a table")
    own = {m.name: m for m in fit.secondaries}
    for name, margin in guardrails.items():
        if name not in own:
            raise SpecError(f"{sid}: {data['fitness']} has no secondary metric {name!r}; choose from {sorted(own)}")
        if isinstance(margin, bool) or not isinstance(margin, int | float) or margin < 0:
            raise SpecError(f"{sid}: guardrail {name} needs a non-negative margin, got {margin!r}")
        if own[name].guardrail and margin > own[name].margin:
            raise SpecError(f"{sid}: guardrail {name} margin {margin} is looser than the instrument's {own[name].margin}")

    budget = data.get("budget", {})
    if not isinstance(budget, dict) or set(budget) - BUDGET_KEYS:
        raise SpecError(f"{sid}: [budget] takes only {sorted(BUDGET_KEYS)}")
    calls = {}
    for key in BUDGET_KEYS:
        n = budget.get(key, DEFAULT_CALLS)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_CALLS:
            raise SpecError(f"{sid}: budget.{key} must be an integer from 1 to {MAX_CALLS}, got {n!r}")
        calls[key] = n

    for key in ("source", "label"):
        if key in data and not isinstance(data[key], str):
            raise SpecError(f"{sid}: {key} must be a string")
    threads = data.get("threads", [])
    if threads:
        if not getattr(fit, "accepts_threads", False):
            raise SpecError(f"{sid}: {data['fitness']} doesn't take thread files")
        if not isinstance(threads, list) or not all(isinstance(t, str) for t in threads):
            raise SpecError(f"{sid}: threads must be a list of repo-relative paths")
        outside = [t for t in threads if not (repo / t).resolve().is_relative_to(Path(repo).resolve())]
        if outside:
            raise SpecError(f"{sid}: thread file(s) {outside} lie outside the repository")
        missing = [t for t in threads if not (repo / t).is_file()]
        if missing:
            raise SpecError(f"{sid}: thread file(s) {missing} not found under {repo}")
    proposed_at = _datetime(data["proposed_at"])
    if proposed_at > (now or datetime.now(timezone.utc)) + CLOCK_SKEW:
        raise SpecError(f"{sid}: proposed_at {proposed_at.isoformat()} is in the future; a screen would include "
                        "items up to then and the confirmation couldn't start until it passed")
    return Spec(id=sid, path=path, proposed_at=proposed_at, proposed_by=data["proposed_by"].strip(),
                profile=data["profile"], fitness=data["fitness"], hypothesis=data["hypothesis"].strip(),
                knobs=dict(knob_values), kind=kind, source=data.get("source"), label=data.get("label", ""),
                commit=commit, summary=summary, guardrails={k: float(v) for k, v in guardrails.items()},
                threads=tuple(repo / t for t in threads), screen_calls=calls["screen_calls"],
                confirm_calls=calls["confirm_calls"])


def load(directory: Path, catalog: dict[str, type], *, repo: Path = config.ROOT,
         now: datetime | None = None) -> tuple[list[Spec], dict[str, str]]:
    """Every spec in `directory`, sorted by id, and {file name: why it was refused} for the rest."""
    specs, errors = [], {}
    if not Path(directory).is_dir():
        return specs, errors
    for path in sorted(Path(directory).glob("*.toml")):
        try:
            specs.append(parse(path, catalog, repo=repo, now=now))
        except SpecError as e:
            errors[path.name] = str(e)
    return specs, errors


# ---------------------------------------------------------------- where a spec stands, from the ledger


@dataclass(frozen=True)
class Phase:
    """The ledger's view of one spec: its screen and confirmation records, newest last."""

    screen: list[dict]
    confirm: list[dict]

    @property
    def next(self) -> str:
        """screen, confirm or done. `pending` records (not run) don't settle a phase."""
        screened = [r for r in self.screen if r["decision"]["status"] != "pending"]
        if not screened:
            return "screen"
        if screened[-1]["decision"]["status"] != "candidate":
            return "done"
        confirmed = [r for r in self.confirm if r["decision"]["status"] != "pending"]
        return "done" if confirmed else "confirm"


def phase(spec: Spec, records: list[dict]) -> Phase:
    """The records of this version of the spec: same id and same digest."""
    mine = [r for r in records if r["proposer"].get("kind") == "queue" and r["proposer"].get("spec") == spec.id
            and r["proposer"].get("spec_digest") == spec.digest]
    return Phase([r for r in mine if r["proposer"].get("phase") == "screen"],
                 [r for r in mine if r["proposer"].get("phase") == "confirm"])
