"""`harness improve validate`: run queue specs through the loop's own parser and admission, and spend nothing.

The loop runs from another checkout, so a spec's author can't watch it try, and a refused spec waits for a
person. This runs the code the loop runs before it spends a call (`queue.parse`, then `Scheduler.admit`)
against the ledger, instrument file and watcher threads the loop would read, and prints the decision: the
phase, the items, the calls it would budget and the models it would probe. It never probes a model and
never runs an arm. Its scheduler state and its copy of the watcher's threads live in a temporary directory
that is deleted on exit, so it can't mark anything seen or done for the live loop.

Two things can differ from the loop's own decision. The loop's cutoff is when it first sees the file, so its
screen uses items up to then, not up to now. And a spec here gets the cycle's whole experiment allowance,
where the loop gives it what earlier specs and due stack checks left; due stack checks are listed, because
the loop runs them first.
"""

import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from harness.improve import fold, queue
from harness.improve import scheduler as sch
from harness.iterate import campaign, ledger

# Admission states that let the phase through or hold it for a reason that clears on its own. The others
# (refused, stale, deferred for budget) need the spec or the loop's allowance to change.
ADMITTED = {"ready", "waiting", "held", "done"}


@dataclass
class Verdict:
    file: str
    state: str  # "ready", a state Scheduler.admit gave, or "refused" when the parser refused the file
    note: str = ""
    spec: queue.Spec | None = None
    plan: sch.Plan | None = None
    items_by_source: dict[str, int] = field(default_factory=dict)  # the plan's items: fixture files, watcher, corpus
    stacks_due: list[str] = field(default_factory=list)  # promotions whose stack check the loop runs first

    @property
    def admitted(self) -> bool:
        return self.state in ADMITTED


def _no_probe(models: set[str]) -> dict[str, str]:
    raise AssertionError("validate never probes a model")


def check(paths: list[Path], cfg: sch.ImproveConfig, catalog: dict[str, type] | None = None,
          now: datetime | None = None) -> list[Verdict]:
    """A verdict per spec file. Reads the ledger, instruments file and watcher threads `cfg` names; writes only
    to a temporary directory."""
    now = now or datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="improve-validate-") as tmp:
        s = sch.Scheduler(replace(cfg, out_dir=Path(tmp)), echo=False, probe=_no_probe, catalog=catalog)
        if Path(s.cfg.threads_dir).is_dir():
            fold.snapshot(s.cfg.threads_dir, s.snapshot_dir)  # what the loop's fold step does each cycle
        records, configs = ledger.records(s.cfg.ledger_dir), ledger.configs(s.cfg.ledger_dir)
        champion, _ = ledger.champion(s.cfg.ledger_dir)
        reads = {n: c.reads for n, c in s.catalog.items() if hasattr(c, "reads")}
        due = campaign.unmeasured(records, configs, {n: c.knobs for n, c in s.catalog.items()}, champion, reads)
        out = []
        for path in paths:
            try:
                spec = queue.parse(Path(path), s.catalog, now=now)
            except queue.SpecError as e:
                out.append(Verdict(Path(path).name, "refused", str(e)))
                continue
            entry: dict = {}
            with sch.profile_env(spec.profile):
                plan = s.admit(spec, entry, now, s.cfg.experiment_calls)
                counts = _by_source(s, spec, plan) if plan else {}
            out.append(Verdict(Path(path).name, "ready" if plan else entry["state"], entry.get("note", ""), spec, plan,
                               counts, [st.latest for st in due if st.fitness == spec.fitness]))
        return out


def _by_source(s: sch.Scheduler, spec: queue.Spec, plan: sch.Plan) -> dict[str, int]:
    """How many of the phase's items each source supplied. A thread two sources hold counts once, under the source
    whose copy the instrument keeps: fixture files, then the watcher, then the corpus (Scheduler.build's order)."""
    fixture = {it.id for it in s.catalog[spec.fitness](list(spec.threads)).items()} if spec.threads else set()
    watcher = set()
    if "watcher" in (spec.sources or ("watcher",)):
        watch, profiles = s._watch()
        watcher = {t["id"] for t in fold.lineup_threads(watch, profiles, spec.profile)} if watch is not None else set()
    corpus = {t["id"] for t in s.corpus_threads(spec.profile)} if "corpus" in spec.sources else set()
    out = {"fixture files": 0, "watcher": 0, "corpus": 0}
    for it in plan.fit.items():
        source = next((n for n, ids in (("fixture files", fixture), ("watcher", watcher), ("corpus", corpus))
                       if it.id in ids), None)
        if source is None:
            raise AssertionError(f"item {it.id} came from no source the spec names")
        out[source] += 1
    return out


def render(v: Verdict) -> list[str]:
    lines = [f"{v.file}: {'admitted' if v.admitted else 'NOT ADMITTED'} ({v.state})"]
    if v.note:
        lines.append(f"  {v.note}")
    if v.spec is not None:
        lines.append(f"  fitness {v.spec.fitness}, profile {v.spec.profile}, kind {v.spec.kind}, digest {v.spec.digest}")
        if v.spec.baseline:
            lines.append(f"  both arms: {v.spec.baseline}")
        if v.spec.sources or v.spec.min_clusters:
            lines.append(f"  sources: {', '.join(v.spec.sources) or 'the instrument default'}; each phase needs at "
                         f"least {v.spec.min_clusters or 'its default number of'} clusters")
        lines.append(f"  challenger adds: {v.spec.knobs}")
    if v.plan is not None:
        p = v.plan
        items = p.fit.items()
        lines += [
            f"  next phase: {p.phase}, on items rooted {'by' if p.phase == 'screen' else 'after'} "
            f"{p.cutoff.isoformat(timespec='minutes')}",
            f"  items: {len(items)} in {len({it.cluster for it in items})} {p.fit.cluster_unit}s: "
            + ", ".join(f"{n} from {src}" for src, n in v.items_by_source.items() if n),
            f"  estimated calls: {p.estimate} for the champion, its repeat and the challenger; budget {p.budget} after "
            f"{p.probes} probe call(s)",
            f"  models it would probe first: {', '.join(sorted(p.needed))}",
            f"  guardrails: {', '.join(f'{m.name} (margin {m.margin:g})' for m in p.fit.secondaries if m.guardrail)}",
        ]
    if v.stacks_due:
        lines.append(f"  the loop runs a stack check on {v.spec.fitness} first (after {', '.join(v.stacks_due)})")
    return lines


def main(args) -> int:
    queue_dir = Path(args.queue_dir) if args.queue_dir else queue.QUEUE_DIR
    paths = [Path(p) for p in args.specs] or sorted(queue_dir.glob("*.toml"))
    if not paths:
        print(f"[improve] no spec files in {queue_dir}")
        return 1
    paths_kw: dict = {k: Path(v) for k, v in (("threads_dir", args.threads_dir), ("ledger_dir", args.ledger_dir),
                                        ("instruments_path", args.instruments), ("corpus_dir", args.corpus_dir)) if v}
    if args.experiment_calls is not None:
        paths_kw["experiment_calls"] = args.experiment_calls
    cfg = sch.ImproveConfig(provider="anthropic", **paths_kw)  # the live loop's settings: probes count, real ledger
    if not Path(cfg.threads_dir).is_dir():
        print(f"[improve] no watcher threads at {cfg.threads_dir}, so specs see their own thread files only; "
              "pass --threads-dir to read the watcher's")
    print(f"[improve] validating {len(paths)} spec(s) against {cfg.ledger_dir} and {cfg.instruments_path}; "
          "nothing is probed or run")
    verdicts = check(paths, cfg)
    for v in verdicts:
        print("\n".join(render(v)))
    bad = [v.file for v in verdicts if not v.admitted]
    print(f"[improve] {len(verdicts) - len(bad)} of {len(verdicts)} admitted"
          + (f"; not admitted: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0
