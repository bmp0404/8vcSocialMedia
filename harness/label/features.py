"""LLM labels as ranker features: item -> field -> the values the reference annotators gave."""

import json
from collections import defaultdict
from pathlib import Path

from harness.label.annotate import LLM_DIR
from harness.label.schema import SCHEMA

# The two annotators every scaled label comes from (chosen on the pilot; see harness/label/DESIGN.md).
REFERENCE_MODELS = ("claude-sonnet-5-5", "gpt-6.1-sol")


def reply_label_votes(models: tuple[str, ...] = REFERENCE_MODELS, schema: dict = SCHEMA,
                      llm_dir: Path = LLM_DIR) -> tuple[dict, dict]:
    """(item_id -> field -> [values], schema). Uses each model's latest error-free answer, and keeps an
    item only when every model answered it, so every item's features average the same annotators."""
    latest = defaultdict(dict)  # item -> model -> (at, labels)
    for path in sorted(Path(llm_dir).glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for row in map(json.loads, f):
                m = row["annotator"]["model"]
                if m not in models or row.get("error"):
                    continue
                prev = latest[row["item_id"]].get(m)
                if prev is None or row["at"] >= prev[0]:
                    latest[row["item_id"]][m] = (row["at"], row["labels"])
    names = [f["name"] for f in schema["fields"]]
    out = {}
    for item_id, per_model in latest.items():
        if set(per_model) != set(models):
            continue
        out[item_id] = {n: [per_model[m][1][n] for m in models if n in per_model[m][1]] for n in names}
        out[item_id] = {n: v for n, v in out[item_id].items() if len(v) == len(models)}
    return out, schema
