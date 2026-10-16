"""Run one batch of experiments: champion, champion repeat and challengers on the same items, under a
call budget, then gate every comparison together and write the ledger.

A batch is one Benjamini-Hochberg family. Proposals that don't fit the budget are recorded as
`pending` with the reason, so the ledger shows what was planned and not run.

A confirmation retests a promotion against the champion it replaced, usually under a stricter
instrument: pass `champion` (the old config) and a proposal with proposer kind "confirmation" whose
source is the promoted record. If the confirmation isn't promoted again, the run appends an automatic
revert of that record, so a promotion that fails its retest doesn't stay in force.

`mode` says what a gate pass means:
- "promote" (the default): the change joins the champion, at most one change per rolling 7 days across
  every instrument (campaign.weekly_cap). A pass while the window is closed is recorded inconclusive.
- "screen": the change becomes a `candidate`, which never touches the champion. The experiment queue
  screens on items that existed when the spec was written and promotes only after a confirmation on
  items rooted later (harness/improve/queue.py).
- "stack": `champion` is the config before a promotion and the proposal re-applies it, to measure a
  combination of promotions no run scored together (campaign.unmeasured). Recorded `stack_ok`, or
  `stack_failed` when the combination is shown worse or fails a guardrail.
"""

import hashlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.config import ROOT
from harness.iterate import campaign, ledger, stats
from harness.iterate.fitness import ArmResult, Fitness, Item, Metric
from harness.iterate.knobs import Config, KnobError, diff, readers, unified_diff
from harness.iterate.llm import BudgetExceeded, Meter, MeteredLLM, MockMeteredLLM

Q = 0.1  # BH level; the memory research recommends q=0.1 across a run's hypotheses (research/memory.md, finding 1)
N_BOOT = 2000
MODES = ("promote", "screen", "stack")
EXPLICIT_CHAMPION_KINDS = {"confirmation", "stack"}


@dataclass
class Proposal:
    fitness: str
    changes: dict[str, Any]
    hypothesis: str
    proposer: dict = field(default_factory=lambda: {"kind": "manual", "source": None})
    label: str = ""


@dataclass
class Arm:
    key: str
    fitness: Fitness
    config: Config
    repeat: bool = False
    result: ArmResult | None = None
    error: str = ""
    seconds: float = 0.0


def _llm_factory(provider: str, meter: Meter, arm: str, mock_respond: Callable) -> Callable[[str], Any]:
    cache: dict[str, Any] = {}

    def llm_for(model: str):
        if model not in cache:
            cache[model] = (MockMeteredLLM(meter, arm, mock_respond, model) if provider == "mock"
                            else MeteredLLM(model, meter, arm))
        return cache[model]
    return llm_for


def _run_arm(arm: Arm, items: list[Item], provider: str, meter: Meter, seed: int) -> None:
    started = time.monotonic()
    try:
        llm_for = _llm_factory(provider, meter, arm.key, getattr(arm.fitness, "mock_respond", lambda s, p: {}))
        arm.result = arm.fitness.run_arm(arm.config, items, llm_for, seed=seed, repeat=arm.repeat)
    except BudgetExceeded as e:
        arm.error = f"over budget: {e}"
    except Exception as e:  # noqa: BLE001 - an arm's crash is recorded; the other arms still count
        arm.error = f"{type(e).__name__}: {e}"
    arm.seconds = round(time.monotonic() - started, 1)
    print(f"[iterate] arm {arm.key}: {'ERROR ' + arm.error if arm.error else 'done'} in {arm.seconds}s")


def _metric_block(fit: Fitness, items: list[Item], champ: ArmResult, chall: ArmResult, metric: Metric,
                  seed: int) -> tuple[dict, stats.Paired]:
    def values(arm: ArmResult) -> dict[str, float]:
        return arm.values if metric is fit.primary else arm.secondary[metric.name]

    a, b = values(champ), values(chall)
    clusters = [it.cluster for it in items]
    sa, sb = fit.stat(items, a, metric), fit.stat(items, b, metric)
    paired = stats.paired_bootstrap(sa, sb, clusters, higher_is_better=metric.higher_is_better, n_boot=N_BOOT, seed=seed)
    lo, hi = paired.interval(0.95)
    # The interval is on the improvement; flip it back to challenger - champion for display.
    delta_ci = [lo, hi] if metric.higher_is_better else [-hi, -lo]
    block = {"name": metric.name, "higher_is_better": metric.higher_is_better, "primary": metric is fit.primary,
             "guardrail": metric.guardrail, "margin": metric.margin,
             "champion": {"value": paired.champion, "ci95": _ci(stats.marginal_interval(sa, clusters, N_BOOT, seed))},
             "challenger": {"value": paired.challenger, "ci95": _ci(stats.marginal_interval(sb, clusters, N_BOOT, seed))},
             "delta": {"value": paired.delta, "ci95": delta_ci}}
    return block, paired


def _ci(ci):
    return list(ci) if ci else None


def _common(items: list[Item], *arms: ArmResult, metric_names: list[str]) -> list[Item]:
    keep = []
    for it in items:
        if all(it.id in a.values and all(it.id in a.secondary.get(m, {}) for m in metric_names) for a in arms):
            keep.append(it)
    return keep


def run_batch(proposals: list[Proposal], fitnesses: dict[str, Fitness], *, provider: str, max_calls: int,
              seed: int = 0, directory: Path | None = None, run_note: str = "",
              champion: Config | None = None, mode: str = "promote", now: datetime | None = None) -> list[dict]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    now = now or datetime.now(timezone.utc)
    directory = directory or ledger.ledger_dir(provider)
    # Records carry `now`, so a run told it is next week (a test, a replay) files its decisions next week.
    started_at = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    run_id = f"run-{started_at[:19].replace('-', '').replace(':', '')}"
    current, _ = ledger.champion(directory)
    if champion is None:
        if mode == "stack":
            raise ValueError("a stack check needs the baseline config as `champion`")
        champion = current
    elif any(p.proposer.get("kind") not in EXPLICIT_CHAMPION_KINDS for p in proposals):
        raise ValueError("an explicit champion is only for confirmation and stack runs")
    meter = Meter(max_calls)
    print(f"[iterate] {run_id}: champion {champion.ref}, {len(proposals)} proposal(s), cap {max_calls} calls, "
          f"provider {provider}")

    # Validate proposals against the registry and their instrument, and dedupe challengers.
    planned: list[tuple[Proposal, Config]] = []
    pending: list[tuple[Proposal, Config | None, str]] = []
    for prop in proposals:
        fit = fitnesses.get(prop.fitness)
        try:
            if fit is None:
                raise KnobError(f"no fitness {prop.fitness!r}; choose from {sorted(fitnesses)}")
            challenger = champion.with_changes(prop.changes)  # unknown knobs and bad values raise first
            unmeasured = set(prop.changes) - set(fit.knobs)
            if unmeasured:
                raise KnobError(f"{prop.fitness} can't measure {sorted(unmeasured)}; it reads {list(fit.knobs)}")
        except KnobError as e:
            pending.append((prop, None, f"invalid proposal: {e}"))
            continue
        if challenger.hash == champion.hash:
            pending.append((prop, challenger, "challenger equals the champion"))
        elif any(c.hash == challenger.hash and p.fitness == prop.fitness for p, c in planned):
            pending.append((prop, challenger, "duplicate of an earlier proposal in this run"))
        else:
            planned.append((prop, challenger))

    # Budget: each fitness used needs a champion and a repeat arm, plus one arm per challenger.
    items_by_fit = {name: fitnesses[name].items() for name in {p.fitness for p, _ in planned}}
    kept, spent = [], 0
    for name in items_by_fit:
        n, fit = len(items_by_fit[name]), fitnesses[name]
        base = fit.estimate_calls(champion, n) + fit.estimate_calls(champion, n, repeat=True)
        for prop, cfg in [(p, c) for p, c in planned if p.fitness == name]:
            cost = fit.estimate_calls(cfg, n) + (base if not any(p.fitness == name for p, _ in kept) else 0)
            if spent + cost > max_calls:
                pending.append((prop, cfg, f"not run: needs about {cost} calls, {max_calls - spent} left of {max_calls}"))
                continue
            spent += cost
            kept.append((prop, cfg))
    print(f"[iterate] running {len(kept)} challenger(s); estimated {spent} calls")

    arms: dict[str, Arm] = {}
    for prop, cfg in kept:
        fit = fitnesses[prop.fitness]
        arms.setdefault(f"{prop.fitness}:champion", Arm(f"{prop.fitness}:champion", fit, champion))
        arms.setdefault(f"{prop.fitness}:champion_repeat", Arm(f"{prop.fitness}:champion_repeat", fit, champion, repeat=True))
        arms.setdefault(f"{prop.fitness}:{cfg.ref}", Arm(f"{prop.fitness}:{cfg.ref}", fit, cfg))
    with ThreadPoolExecutor(max(1, len(arms))) as pool:
        list(pool.map(lambda a: _run_arm(a, items_by_fit[a.key.split(":")[0]], provider, meter, seed), arms.values()))

    # Compare each challenger with the champion on the items all three arms scored.
    results = []
    for prop, cfg in kept:
        fit = fitnesses[prop.fitness]
        champ, rep, chall = (arms[f"{prop.fitness}:{k}"] for k in ("champion", "champion_repeat", cfg.ref))
        failed = [a for a in (champ, rep, chall) if a.error or a.result is None]
        if failed:
            results.append((prop, cfg, None, "; ".join(f"{a.key}: {a.error}" for a in failed)))
            continue
        names = [m.name for m in fit.secondaries]
        items = _common(items_by_fit[prop.fitness], champ.result, rep.result, chall.result, metric_names=names)
        if len(items) < 5:
            results.append((prop, cfg, None, f"only {len(items)} items scored by all three arms"))
            continue
        primary, paired = _metric_block(fit, items, champ.result, chall.result, fit.primary, seed)
        repeat_block, _ = _metric_block(fit, items, champ.result, rep.result, fit.primary, seed + 1)
        secondary, broken = [], []
        for m in fit.secondaries:
            try:
                secondary.append(_metric_block(fit, items, champ.result, chall.result, m, seed))
            except ValueError as e:
                print(f"[iterate] secondary {m.name} skipped: {e}")
                if m.guardrail:
                    broken.append(m.name)  # a guardrail that can't be computed blocks, below
        failures = {k: len(a.result.failures) for k, a in (("champion", champ), ("champion_repeat", rep),
                                                            ("challenger", chall))}
        results.append((prop, cfg, {"items": items, "primary": primary, "paired": paired, "repeat": repeat_block,
                                    "secondary": secondary, "broken_guardrails": broken, "failures": failures}, ""))

    # One BH family for the whole run.
    tested = [r for r in results if r[2] is not None]
    adjusted, rejected = stats.benjamini_hochberg([r[2]["paired"].p_one_sided for r in tested], Q)
    n_rej = sum(rejected)
    decisions = {}
    for (prop, _cfg, res, _), p_adj, rej in zip(tested, adjusted, rejected):
        noise = abs(res["repeat"]["delta"]["value"])
        dec = stats.decide(res["paired"], rejected_by_bh=rej, n_rejected=n_rej, family_size=len(tested), q=Q,
                           noise_floor=noise)
        status, reason = dec.status, dec.reason
        unproven = [f"{blk['name']} (95% lower bound {pr.interval(0.95)[0]:+.3f}, margin -{blk['margin']})"
                    for blk, pr in res["secondary"] if blk["guardrail"] and not stats.non_inferior(pr, blk["margin"])]
        unproven += [f"{name} (could not be computed)" for name in res["broken_guardrails"]]
        if status == "promoted" and unproven:
            status, reason = "inconclusive", f"{reason}; blocked: not shown non-inferior on {', '.join(unproven)}"
        # Items an arm failed on drop out of the comparison, so a challenger that fails on the hard items
        # would be graded on an easier set. Allow a little slack, then block.
        extra = res["failures"]["challenger"] - res["failures"]["champion"]
        if status == "promoted" and extra > max(2, 0.1 * len(items_by_fit[prop.fitness])):
            status, reason = "inconclusive", f"{reason}; blocked: the challenger failed on {extra} more items"
        if mode == "stack":
            failed_more = extra > max(2, 0.1 * len(items_by_fit[prop.fitness]))
            worse = dec.status == "rejected" or bool(unproven) or failed_more
            if failed_more:
                unproven.append(f"the combination failed on {extra} more items")
            status = "stack_failed" if worse else "stack_ok"
            reason = (f"the promotions together are worse than without {prop.proposer.get('source')}: {dec.reason}"
                      + (f"; not shown non-inferior on {', '.join(unproven)}" if unproven else "")
                      if worse else f"measured together and not shown worse: {dec.reason}")
        decisions[id(prop)] = (status, reason, dec, p_adj, rej, noise)

    # At most one promotion per fitness per run: two winners were never measured together, so applying
    # both would crown a config no arm scored. The strongest bound wins; ties go to the smaller p-value,
    # then the config hash, so the outcome doesn't depend on proposal order.
    def demote(prop, why):
        s_, r_, d_, pa_, rj_, nf_ = decisions[id(prop)]
        decisions[id(prop)] = ("inconclusive", f"cleared the gate, but {why}; retest it against the new champion "
                               f"({r_})", d_, pa_, rj_, nf_)

    winners = {}
    for prop, cfg, res, _ in tested:
        if decisions[id(prop)][0] != "promoted":
            continue
        rank = (-decisions[id(prop)][2].bound, res["paired"].p_one_sided, cfg.hash)
        best = winners.get(prop.fitness)
        if best is None or rank < best[0]:
            if best is not None:
                demote(best[1], f"{prop.label or 'another challenger'} on the same fitness cleared it more strongly")
            winners[prop.fitness] = (rank, prop)
        else:
            demote(prop, f"{best[1].label or 'another challenger'} on the same fitness cleared it more strongly")
    # Bounds on different fitnesses are in different units, so a knob two fitnesses both want to set
    # is left for a run of its own.
    kept_props = [w[1] for w in winners.values()]
    for prop in kept_props:
        for other in kept_props:
            if other is not prop and set(prop.changes) & set(other.changes):
                demote(prop, f"{other.fitness} also promoted a change to {sorted(set(prop.changes) & set(other.changes))}")

    # Campaign accounting: one counted promotion per rolling 7 days, across every fitness.
    counted = [prop for prop, _cfg, res, _ in tested if decisions[id(prop)][0] == "promoted"
               and prop.proposer.get("kind") not in campaign.UNCOUNTED_KINDS]
    if mode == "promote" and counted:
        cap = campaign.weekly_cap(ledger.records(directory), now)
        if not cap.open:
            for prop in counted:
                demote(prop, f"{cap.holder} was promoted within the last 7 days and the engine promotes one change "
                             f"per rolling week; the next can be promoted after {cap.opens_at.isoformat(timespec='minutes')}")
        else:
            # P-values are comparable across fitnesses where bounds aren't; ties go to the config hash.
            res_of = {id(p): (r, c) for p, c, r, _ in tested}
            best = min(counted, key=lambda p: (decisions[id(p)][3], res_of[id(p)][0]["paired"].p_one_sided,
                                               res_of[id(p)][1].hash))
            for prop in counted:
                if prop is not best:
                    demote(prop, f"the engine promotes one change per rolling week and {best.label or best.fitness} "
                                 "cleared the gate with a smaller adjusted p-value in the same run")
    if mode == "screen":
        for prop, _cfg, _res, _ in tested:
            s_, r_, d_, pa_, rj_, nf_ = decisions[id(prop)]
            if s_ == "promoted":
                decisions[id(prop)] = ("candidate", f"cleared the gate on the screening items ({r_}); it joins the "
                                       "champion only after a confirmation on items rooted after it was proposed",
                                       d_, pa_, rj_, nf_)

    recs = []
    report_paths = _write_reports(directory, run_id, arms, provider)
    for prop, cfg, res, err in results:
        recs.append(_record(prop, champion, cfg, fitnesses[prop.fitness], res, err, decisions.get(id(prop)),
                            family=len(tested), run_id=run_id, seed=seed, arms=arms, meter=meter,
                            report=report_paths.get(prop.fitness), ts=started_at))
    for prop, cfg, why in pending:
        recs.append(_record(prop, champion, cfg, fitnesses.get(prop.fitness), None, why, None, family=len(tested),
                            run_id=run_id, seed=seed, arms=arms, meter=meter, report=None, ts=started_at))
    run = {"run_id": run_id, "started_at": started_at, "finished_at": ledger.now(), "provider": provider,
           "mode": mode, "champion": champion.ref, "max_calls": max_calls, "estimated_calls": spent,
           "llm_calls": meter.calls,
           "by_arm": dict(meter.by_arm), "bh": {"q": Q, "family_size": len(tested), "rejected": n_rej},
           "experiments": [r["id"] for r in recs], "note": run_note}
    for cfg in {champion.ref: champion, **{c.ref: c for _, c in kept}}.values():
        ledger.save_config(directory, cfg)
    ledger.write(directory, recs, run)
    print(f"[iterate] {run_id}: {meter.calls} calls; wrote {len(recs)} record(s) to {directory}")
    for prop, rec in zip([r[0] for r in results] + [p[0] for p in pending], recs):
        source = prop.proposer.get("source")
        if prop.proposer.get("kind") != "confirmation" or rec["decision"]["status"] == "promoted":
            continue
        if rec["decision"]["status"] == "pending":
            print(f"[iterate] confirmation of {source} didn't run; leaving it in force")
            continue
        rev = ledger.revert_record(directory, source, f"Confirmation {rec['id']} didn't promote it again: "
                                                      f"{rec['decision']['reason']}", by="auto", at=started_at)
        ledger.write(directory, [rev])
        recs.append(rev)
        print(f"[iterate] reverted {source}: {rev['id']}")
    return recs


def _write_reports(directory: Path, run_id: str, arms: dict[str, Arm], provider: str) -> dict[str, str]:
    """Per-item detail (drafts, picks, scores) for each fitness, so a reader can audit any number."""
    out: dict[str, dict] = {}
    for arm in arms.values():
        name = arm.key.split(":")[0]
        out.setdefault(name, {"run_id": run_id, "provider": provider, "arms": {}})["arms"][arm.key] = {
            "config_ref": arm.config.ref, "repeat": arm.repeat, "error": arm.error, "seconds": arm.seconds,
            "detail": arm.result.detail if arm.result else {}, "failures": arm.result.failures if arm.result else {}}
    paths = {}
    for name, data in out.items():
        path = directory / "reports" / f"{run_id}-{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        paths[name] = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    return paths


def _record(prop: Proposal, champion: Config, cfg: Config | None, fit: Fitness | None, res: dict | None, err: str,
            decision, *, family: int, run_id: str, seed: int, arms: dict[str, Arm], meter: Meter,
            report: str | None, ts: str) -> dict:
    challenger = cfg or champion
    params = diff(champion, challenger)
    # The clock reading keeps two runs of one proposal in the same second from colliding on an id.
    rid = hashlib.sha256(f"{run_id}:{prop.fitness}:{challenger.hash}:{prop.hypothesis}:{time.time_ns()}"
                         .encode()).hexdigest()[:6]
    stage = fit.stage if fit else "unknown"
    ev: dict[str, Any] = {"dataset": None, "dataset_sha256": None, "n": 0, "unit": getattr(fit, "unit", None),
                          "clusters": 0, "cluster_unit": getattr(fit, "cluster_unit", None), "seed": seed,
                          "report": report, "fitness": prop.fitness, "metrics": []}
    if fit is not None:
        inst = fit.instrument()
        ev.update({"dataset": inst.get("dataset"), "dataset_sha256": inst.get("dataset_sha256"), "instrument": inst})
    if res is not None:
        status, reason, dec, p_adj, rej, noise = decision
        ev.update({"n": len(res["items"]), "clusters": len({it.cluster for it in res["items"]}),
                   "metrics": [res["primary"]] + [blk for blk, _ in res["secondary"]],
                   "noise_floor": {"value": noise, "champion_repeat": res["repeat"]["challenger"],
                                   "delta": res["repeat"]["delta"]},
                   "failures": res["failures"],
                   "correction": {"method": "benjamini-hochberg", "q": Q, "family_size": family,
                                  "p": res["paired"].p_one_sided, "p_adjusted": p_adj, "rejected": rej,
                                  "interval_level": dec.interval_level},
                   "item_ids_sha256": hashlib.sha256(",".join(sorted(it.id for it in res["items"])).encode()).hexdigest()})
    else:
        status, reason = "pending", err
    return {
        "schema": ledger.SCHEMA, "id": f"exp-{ts[:10].replace('-', '')}-{stage}-{rid}", "run_id": run_id,
        "created_at": ts, "stage": stage, "hypothesis": prop.hypothesis, "proposer": prop.proposer,
        "change": {"summary": prop.label or ", ".join(f"{k} → {v['to']}" for k, v in params.items()) or "no change",
                   "files": readers(params), "diff": unified_diff(champion, challenger), "params": params},
        "champion": {"ref": champion.ref, "label": "champion", "config_hash": champion.hash},
        "challenger": {"ref": challenger.ref, "label": prop.label or "challenger", "config_hash": challenger.hash},
        "eval": ev,
        "decision": {"status": status, "by": "auto", "at": ts, "reason": reason},
        "cost": _cost(prop, challenger, arms, meter),
    }


def _cost(prop: Proposal, challenger: Config, arms: dict[str, Arm], meter: Meter) -> dict:
    """The challenger arm's own cost, plus the champion and repeat arms it shares with every other
    challenger on the same fitness (reported once per record as `shared_llm_calls`, not summed)."""
    by_arm, own, shared = {}, {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}, 0
    for kind in ("champion", "champion_repeat", challenger.ref):
        arm = arms.get(f"{prop.fitness}:{kind}")
        if arm is None:
            continue
        m = meter.by_arm.get(arm.key, {})
        by_arm[kind] = {**{k: m.get(k, 0) for k in ("llm_calls", "input_tokens", "output_tokens", "errors")},
                        "seconds": arm.seconds, "shared": kind != challenger.ref}
        if kind == challenger.ref:
            own = {"llm_calls": m.get("llm_calls", 0), "input_tokens": m.get("input_tokens", 0),
                   "output_tokens": m.get("output_tokens", 0), "seconds": arm.seconds}
        else:
            shared += m.get("llm_calls", 0)
    return {**own, "usd": None, "shared_llm_calls": shared, "by_arm": by_arm}
