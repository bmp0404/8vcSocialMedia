"""Measure each label field: agreement between annotators and validity against the dev benchmarks.

Agreement is Krippendorff's alpha (nominal for enum fields, ordinal for ordinal ones) over items
both annotators labeled. Validity treats a field as a scorer on dev-split benchmark pairs whose two
items are both labeled:
- `preregistered`: ordinal fields with a direction fixed before looking (more specific, novel,
  adding or engagement-guessed wins; more AI-sounding, baity or hostile loses), score = mean rank.
- `crossfit`: any field; an item's score is the win rate of its value in pairs from other threads
  or authors, so the direction is learned without seeing the pair being scored.
"""

import json
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from harness.corpus.bench import load_pairs
from harness.corpus.stats import accuracy, pair_credit
from harness.label.agreement import alpha, percent_agreement
from harness.label.annotate import LLM_DIR
from harness.label.schema import LABELS_DIR, SCHEMA

SCREENING_DIR = LABELS_DIR / "screening"  # runs of models compared on the pilot but not used as annotators

DIRECTION = {"specificity": 1, "novelty": 1, "adds_to_parent": 1, "engagement_guess": 1,
             "reads_as_ai": -1, "engagement_bait": -1, "hostility": -1}


def load_runs(prefix: str, dirs: tuple[Path, ...] = (LLM_DIR, SCREENING_DIR)) -> dict[str, dict[str, dict]]:
    """model -> item_id -> labels, from runs named <prefix>-<model>, latest good answer per item."""
    out = {}
    for path in sorted(p for d in dirs for p in Path(d).glob(f"{prefix}-*.jsonl")):
        model = path.stem[len(prefix) + 1:]
        labels = {}
        for row in map(json.loads, path.open(encoding="utf-8")):
            if not row.get("error"):
                labels[row["item_id"]] = row["labels"]
        out[model] = labels
    return out


def _level(field: dict) -> tuple[str, list | None]:
    return ("ordinal", field["values"]) if field["kind"] == "ordinal" else ("nominal", None)


def field_alpha(runs: dict, a: str, b: str, field: dict) -> dict | None:
    units = [[runs[a][i][field["name"]], runs[b][i][field["name"]]]
             for i in runs[a].keys() & runs[b].keys()
             if field["name"] in runs[a][i] and field["name"] in runs[b][i]]
    if len(units) < 10:
        return None
    level, order = _level(field)
    try:
        al = alpha(units, level, order)
    except ValueError as e:
        return {"n": len(units), "alpha": None, "note": str(e)}
    return {"n": len(units), "alpha": round(al, 3), "agree": round(percent_agreement(units), 3)}


def agreement_table(runs: dict, schema: dict = SCHEMA) -> dict:
    """field -> 'a|b' -> alpha for every pair of models."""
    table = {}
    for f in schema["fields"]:
        table[f["name"]] = {f"{a}|{b}": field_alpha(runs, a, b, f) for a, b in combinations(sorted(runs), 2)}
    return table


def _value_dist(runs: dict, models: list[str], item_id: str, name: str) -> list | None:
    vals = [runs[m][item_id][name] for m in models if item_id in runs[m] and name in runs[m][item_id]]
    return vals if len(vals) == len(models) else None


def validity(runs: dict, models: list[str], schema: dict = SCHEMA) -> dict:
    out = {}
    for bench in ("replies", "posts"):
        pairs = [p for p in load_pairs(bench) if p.split == "dev"]
        out[bench] = {}
        for f in schema["fields"]:
            name = f["name"]
            usable = []
            for p in pairs:
                vw = _value_dist(runs, models, f"x:{p.winner}", name)
                vl = _value_dist(runs, models, f"x:{p.loser}", name)
                if vw and vl:
                    usable.append((p, vw, vl))
            if len(usable) < 30:
                continue
            groups = [p.group for p, _, _ in usable]
            res = {"pairs": len(usable), "groups": len(set(groups))}
            if f["kind"] == "ordinal" and name in DIRECTION:
                rank = {v: i for i, v in enumerate(f["values"])}
                d = DIRECTION[name]

                def mean_rank(vals):
                    return d * sum(rank[v] for v in vals) / len(vals)
                res["preregistered"] = accuracy([pair_credit(mean_rank(vw), mean_rank(vl)) for _, vw, vl in usable],
                                                groups).to_dict()
            # Cross-fitted value win rates: wins and appearances per value, leaving the pair's own group out.
            wins, seen = defaultdict(Counter), defaultdict(Counter)
            for p, vw, vl in usable:
                for v in vw:
                    wins[p.group][json.dumps(v)] += 1 / len(vw)
                    seen[p.group][json.dumps(v)] += 1 / len(vw)
                for v in vl:
                    seen[p.group][json.dumps(v)] += 1 / len(vl)
            tw, ts = Counter(), Counter()
            for g in wins.keys() | seen.keys():
                tw.update(wins[g])
                ts.update(seen[g])

            def rate(vals, group):
                r = []
                for v in vals:
                    k = json.dumps(v)
                    w, s = tw[k] - wins[group][k], ts[k] - seen[group][k]
                    r.append((w + 1) / (s + 2))
                return sum(r) / len(r)
            res["crossfit"] = accuracy([pair_credit(rate(vw, p.group), rate(vl, p.group)) for p, vw, vl in usable],
                                       groups).to_dict()
            out[bench][name] = res
    return out


def distributions(runs: dict, schema: dict = SCHEMA) -> dict:
    return {m: {f["name"]: dict(Counter(json.dumps(l[f["name"]]) for l in labels.values() if f["name"] in l))
                for f in schema["fields"]} for m, labels in runs.items()}
