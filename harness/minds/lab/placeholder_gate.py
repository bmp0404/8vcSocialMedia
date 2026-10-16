"""Does the broader placeholder rule reject text people wrote? Writes action/experiments/placeholder_gate.json.

Compares the gate as it is against the same gate with the placeholder rule it replaced (square brackets only,
and TODO or TBD in any case), on the texts gate_calibration and fact_grounding use:
- wnba: direct replies in action/fixtures/x_threads.json, each checked against its post and the other replies;
- devtools: the corpus reply samples, the same way (local files only, so only counts are written);
- devtools posts: data/devtools_virality_30d.csv, without context;
- model drafts: every draft string in action/experiments/*.json and tests/fixtures/hot/*.json.
No LLM calls.

    uv run python -m harness.minds.lab.placeholder_gate --context-dir <dir with the corpus jsonl files>
"""

import argparse
import csv
import json
import re
from pathlib import Path

from harness import config
from harness.minds import gate
from harness.minds.lab import fact_grounding as fg

REPORT = config.ROOT / "action" / "experiments" / "placeholder_gate.json"
OLD = re.compile(r"\[(?:insert|todo|tbd|link|name|stat|video|image|photo|gif|media|clip)\b[^\]]*\]|\{[a-z_]+\}"
                 r"|\bTODO\b|\bTBD\b", re.I)


def _old_rule(text: str, c) -> list:
    return [(f"unresolved placeholder {m.group(0)!r}", m.start(), m.end()) for m in OLD.finditer(text)]


OLD_RULES = {**gate.RULES, "placeholder": _old_rule}


def compare(items: list[tuple[str, str, str | None]], examples: bool) -> dict:
    """items: (text, kind, context). Rejections under the old and new placeholder rule, and what changed."""
    out = {"n": len(items), "rejected_before": 0, "rejected_after": 0, "placeholder_before": 0, "placeholder_after": 0,
           "newly_rejected": [], "no_longer_rejected": []}
    for text, kind, ctx in items:
        before = gate.check(text, kind, context=ctx, rules=OLD_RULES)
        after = gate.check(text, kind, context=ctx)
        out["rejected_before"] += not before.passed
        out["rejected_after"] += not after.passed
        out["placeholder_before"] += any(f.rule == "placeholder" for f in before.findings)
        out["placeholder_after"] += any(f.rule == "placeholder" for f in after.findings)
        if before.passed != after.passed:
            out["newly_rejected" if before.passed else "no_longer_rejected"].append(text)
    for key in ("newly_rejected", "no_longer_rejected"):
        out[key] = out[key] if examples else len(out[key])
    return out


def thread_items(threads: list[dict]) -> list[tuple[str, str, str]]:
    items = []
    for t in threads:
        for i, text in enumerate(t["replies"]):
            items.append((text, "reply", f"@{t['author']}: {t['text']}\n" + "\n".join(t["replies"][:i] + t["replies"][i + 1:])))
    return items


def model_drafts() -> list[str]:
    found: set[str] = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("text", "draft") and isinstance(v, str):
                    found.add(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for path in [*sorted((config.ROOT / "action" / "experiments").glob("*.json")),
                 *sorted((config.ROOT / "tests" / "fixtures" / "hot").glob("*.json"))]:
        walk(json.loads(path.read_text(encoding="utf-8")))
    return sorted(t for t in found if t.strip())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--context-dir", type=Path, default=fg.CONTEXT)
    args = p.parse_args()
    fixture = json.loads((config.ROOT / "action" / "fixtures" / "x_threads.json").read_text(encoding="utf-8"))["threads"]
    wnba = [{"author": t["author"], "text": t["text"], "replies": [r["text"] for r in t["direct_replies"] if r["text"].strip()]}
            for t in fixture]
    with open(config.ROOT / "data" / "devtools_virality_30d.csv", encoding="utf-8") as f:
        posts = [re.sub(r"https?://t\.co/\w+", "", row["Post Content"]).strip() for row in csv.DictReader(f)]
    drafts = model_drafts()
    report = {
        "wnba_real_replies": compare(thread_items(wnba), examples=True),
        "devtools_real_replies": compare(thread_items(fg.devtools_threads(args.context_dir)), examples=False),
        "devtools_posts": compare([(t, "post", None) for t in posts if t], examples=False),
        "model_drafts": compare([(t, "reply", None) for t in drafts], examples=True),
    }
    REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: {kk: (vv if not isinstance(vv, list) else len(vv)) for kk, vv in v.items()}
                      for k, v in report.items()}, indent=1))


if __name__ == "__main__":
    main()
