"""The experiment ledger: one JSON file per experiment, the champion pointer, and a readable changelog.

Records are the source of truth. The champion is derived from them: start from the registry
defaults and apply each promoted record's knob changes in the order they were decided. A revert is
another promoted record whose changes undo an earlier one, so nothing is ever deleted and every
champion can be rebuilt. `champion.json` and `CHANGELOG.md` are regenerated after every write.

Mock runs write to out/iterate/, which git ignores, so fake numbers never reach eval/experiments/.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness import config as hconfig
from harness.iterate.knobs import DEFAULT, Config, KnobError

SCHEMA = "iterate.experiment/v1"
EXPERIMENTS_DIR = hconfig.ROOT / "eval" / "experiments"
MOCK_DIR = hconfig.OUT_DIR / "iterate"
SKIP = {"champion.json", "DESIGN.md", "CHANGELOG.md"}


def ledger_dir(provider: str) -> Path:
    return MOCK_DIR if provider == "mock" else EXPERIMENTS_DIR


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def records(directory: Path) -> list[dict]:
    """Every experiment record, oldest decision first."""
    out = []
    for path in sorted(directory.glob("exp-*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("schema") != SCHEMA:
            raise ValueError(f"{path} has schema {rec.get('schema')!r}; expected {SCHEMA!r}")
        out.append(rec)
    return sorted(out, key=lambda r: (r["decision"].get("at") or r["created_at"], r["id"]))


def champion(directory: Path) -> tuple[Config, list[str]]:
    """The current champion and the ids of the records that shaped it, oldest first."""
    cfg, history = DEFAULT, []
    for rec in records(directory):
        if rec["decision"]["status"] != "promoted":
            continue
        changes = {k: v["to"] for k, v in rec["change"]["params"].items()}
        try:
            cfg = cfg.with_changes(changes)
        except KnobError as e:
            raise ValueError(f"promoted record {rec['id']} no longer applies to the registry: {e}") from e
        history.append(rec["id"])
    return cfg, history


def configs(directory: Path) -> dict[str, Config]:
    """Every config the ledger saved, by ref. A config that no longer fits the registry is skipped."""
    out = {}
    for path in sorted((directory / "configs").glob("cfg-*.json")):
        try:
            cfg = Config.from_json(json.loads(path.read_text(encoding="utf-8")))
        except KnobError:
            continue
        out[path.stem] = cfg
    return out


def save_config(directory: Path, cfg: Config) -> Path:
    path = directory / "configs" / f"{cfg.ref}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(cfg.to_json(), indent=1) + "\n", encoding="utf-8")
    return path


def write(directory: Path, recs: list[dict], run: dict | None = None) -> list[Path]:
    """Write records (and an optional run summary), then regenerate the champion and changelog."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for rec in recs:
        path = directory / f"{rec['id']}.json"
        if path.exists():
            raise FileExistsError(f"{path} exists; experiment ids must be unique")
        path.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    if run:
        rp = directory / "runs" / f"{run['run_id']}.json"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    refresh(directory)
    return paths


def refresh(directory: Path) -> None:
    cfg, history = champion(directory)
    save_config(directory, cfg)
    (directory / "champion.json").write_text(json.dumps(
        {"note": "derived from the exp-*.json records by harness/iterate/ledger.py; do not edit",
         "ref": cfg.ref, "config_hash": cfg.hash, "config": cfg.to_json()["values"], "history": history},
        indent=1) + "\n", encoding="utf-8")
    (directory / "CHANGELOG.md").write_text(changelog(records(directory), cfg, history), encoding="utf-8")


def _fmt(stat: dict | None) -> str:
    if not stat or stat.get("value") is None:
        return "n/a"
    ci = stat.get("ci95")
    return f"{stat['value']:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else f"{stat['value']:.3f}"


def changelog(recs: list[dict], cfg: Config, history: list[str]) -> str:
    lines = ["# Experiment changelog", "",
             "Generated from the `exp-*.json` records in this folder by `harness/iterate/ledger.py`. Newest first. "
             "Every number is from the run named in its record; brackets are 95% intervals from resampling the "
             "cluster unit (accounts or threads). DESIGN.md explains the gate.", "",
             f"Current champion: `{cfg.ref}`, shaped by {len(history)} promotion(s)"
             + (f": {', '.join(f'`{h}`' for h in history)}." if history else "; all knobs are at their defaults."), ""]
    for rec in reversed(recs):
        d, ev = rec["decision"], rec["eval"]
        primary = next((m for m in ev.get("metrics", []) if m.get("primary")), None)
        params = ", ".join(f"`{k}` {v['from']!r} → {v['to']!r}" for k, v in rec["change"]["params"].items())
        lines += [f"## {rec['id']}: {d['status']}", "",
                  f"- **Change:** {params or rec['change']['summary']}",
                  f"- **Hypothesis:** {rec['hypothesis']}",
                  f"- **Proposed by:** {rec['proposer']['kind']} ({rec['proposer'].get('source') or 'no source'})"]
        if primary:
            dirn = "higher" if primary["higher_is_better"] else "lower"
            where = (f"{ev['n']} {ev['unit']}s" if ev["unit"] == ev["cluster_unit"]
                     else f"{ev['n']} {ev['unit']}s from {ev['clusters']} {ev['cluster_unit']}s")
            lines += [f"- **{primary['name']}** ({dirn} is better), {where}: champion {_fmt(primary['champion'])}, challenger "
                      f"{_fmt(primary['challenger'])}, difference {_fmt(primary['delta'])}"]
            nf, corr = ev.get("noise_floor") or {}, ev.get("correction") or {}
            if nf:
                lines.append(f"- **Noise floor** (champion rerun with new lineups or seeds): {nf['value']:.3f}")
            if corr:
                lines.append(f"- **Correction:** Benjamini-Hochberg q={corr['q']} over {corr['family_size']} "
                             f"comparison(s); p={corr['p']:.4f}, adjusted {corr['p_adjusted']:.4f}")
        lines += [f"- **Decision:** {d['reason']} ({d['by']}, {d['at']})",
                  f"- **Cost:** {rec['cost']['llm_calls']} calls on the challenger arm"
                  + (f", plus shared champion arms ({rec['cost'].get('shared_llm_calls', 0)} calls)"
                     if rec["cost"].get("shared_llm_calls") else ""), ""]
    return "\n".join(lines) + "\n"


def revert_record(directory: Path, experiment_id: str, reason: str, by: str = "human",
                  at: str | None = None) -> dict[str, Any]:
    """A record that undoes a promoted experiment's knob changes. `by` is "human" from the CLI, "auto"
    when a confirmation run or a stack check fails. `at` stamps it (default now), so a run working on a
    simulated clock files its revert after the promotion it undoes."""
    target = next((r for r in records(directory) if r["id"] == experiment_id), None)
    if target is None:
        raise KeyError(f"no experiment {experiment_id} in {directory}")
    if target["decision"]["status"] != "promoted":
        raise ValueError(f"{experiment_id} was {target['decision']['status']}, not promoted; nothing to revert")
    current, _ = champion(directory)
    undo = {k: v["from"] for k, v in target["change"]["params"].items()}
    after = current.with_changes(undo)
    from harness.iterate.knobs import diff, readers, unified_diff
    params = diff(current, after)
    ts = at or now()
    return {
        "schema": SCHEMA, "id": f"exp-{ts[:19].replace('-', '').replace(':', '')}-revert-{experiment_id[-6:]}",
        "run_id": None, "created_at": ts, "stage": target["stage"],
        "hypothesis": f"Revert {experiment_id}.", "proposer": {"kind": "revert", "source": experiment_id},
        "change": {"summary": f"revert {experiment_id}", "files": readers(params), "diff": unified_diff(current, after),
                   "params": params},
        "champion": {"ref": current.ref, "label": "champion before the revert", "config_hash": current.hash},
        "challenger": {"ref": after.ref, "label": "champion after the revert", "config_hash": after.hash},
        "eval": {"dataset": None, "n": 0, "unit": None, "clusters": 0, "cluster_unit": None, "seed": None,
                 "report": None, "metrics": []},
        "decision": {"status": "promoted", "by": by, "at": ts, "reason": reason},
        "cost": {"llm_calls": 0, "usd": None, "seconds": 0.0, "input_tokens": 0, "output_tokens": 0, "by_arm": {}},
    }
