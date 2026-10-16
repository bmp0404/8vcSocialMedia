"""Export suggested X posts and replies to a spreadsheet-friendly CSV.

One row per public X draft (post, reply, quote, trend post) across the chosen runs. Killed and
critic-vetoed drafts are left out unless asked for. Human edits replace the drafted text. Written as UTF-8 with a BOM so Excel
shows emoji and quotes correctly.
"""

import csv
from datetime import datetime

from harness import config, memory
from harness.actions import is_public
from harness.models import Action

COLUMNS = [
    "run", "rank", "status", "type", "text", "reply_or_quote_to", "chars", "mind", "panel_score",
    "dataset_model", "elo", "record", "hook_upgraded", "critic_note", "why", "action_id",
]


def _public(a: dict) -> bool:
    try:
        return is_public(Action(**{k: v for k, v in a.items() if k in Action.__dataclass_fields__}))
    except TypeError:
        return a.get("type") in ("post", "reply", "quote")


def export_csv(runs: list[int] | None = None, include_rejected: bool = False, path=None):
    run_ids = runs or [r["id"] for r in memory.list_runs()]
    rows = []
    for run_id in run_ids:
        for a in memory.run_actions(run_id):
            status = a["review"]["status"]
            if not _public(a) or not (a["review"]["text"] or a.get("text")):
                continue
            if not include_rejected and (status == "killed" or (a.get("vetoed") and status != "approved")):
                continue
            text = a["review"]["text"] or a["text"]
            rows.append({
                "run": run_id, "rank": a.get("rank") or "", "status": status, "type": a["type"], "text": text,
                "reply_or_quote_to": a.get("target_url") or (f"@{a['target_handle']}" if a.get("target_handle") else ""),
                "chars": max(len(p.strip()) for p in text.split("\n---\n")), "mind": a.get("variant") or a["mind"],
                "panel_score": a.get("predicted_score", ""), "dataset_model": (a.get("panel") or {}).get("dataset_model", ""),
                "elo": a.get("elo") or "", "record": a.get("record", ""), "hook_upgraded": "yes" if a.get("pre_hook_text") else "",
                "critic_note": a.get("critique", ""), "why": a.get("rationale", ""), "action_id": a["id"],
            })
    rows.sort(key=lambda r: (-r["run"], r["rank"] if r["rank"] != "" else 999))
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = path or config.OUT_DIR / f"suggested-posts-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"[export] {len(rows)} suggestions from runs {run_ids} -> {path}")
    return path
