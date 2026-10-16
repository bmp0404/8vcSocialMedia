"""How often does each slop-gate rule fire on text people wrote? Writes
action/experiments/gate_calibration.json.

Corpora: direct replies in action/fixtures/x_threads.json (real X fans), and the post text in
data/devtools_virality_30d.csv (dev-tool brand accounts; some of those may be AI-assisted, so
treat their rate as an upper bound on false positives). Optional: any JSON list of AI drafts,
passed as --drafts, to get the hit rate on model output side by side.

    uv run python -m harness.minds.lab.gate_calibration [--drafts action/experiments/drafts.json]
"""

import argparse
import csv
import json
import re
import statistics

from harness import config
from harness.minds import gate
from harness.minds.lab.fetch_x_threads import OUT as FIXTURE

REPORT = config.ROOT / "action" / "experiments" / "gate_calibration.json"
TCO = re.compile(r"https?://t\.co/\w+")


def _rates(texts: list[str], kind: str, contexts: list[str | None] | None = None) -> dict:
    contexts = contexts or [None] * len(texts)
    results = [gate.check(t, kind, context=c) for t, c in zip(texts, contexts)]
    style = [r for r in results if any(f.rule not in gate.GROUNDING_RULES for f in r.findings)]
    out = {"n": len(texts), "rejected": sum(not r.passed for r in results),
           "rejected_rate": round(sum(not r.passed for r in results) / max(len(texts), 1), 3),
           "rejected_by_style_rules_rate": round(len(style) / max(len(texts), 1), 3), "rules": {}}
    for name in gate.RULES:
        hits = [t for t, r in zip(texts, results) if any(f.rule == name for f in r.findings)]
        out["rules"][name] = {"hits": len(hits), "rate": round(len(hits) / max(len(texts), 1), 3),
                              "examples": [h[:140] for h in hits[:3]]}
    return out


def _pct(xs: list[int], q: float) -> int:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--drafts", help="JSON list of {text, kind, method} model drafts to compare")
    args = p.parse_args()

    threads = json.loads(FIXTURE.read_text(encoding="utf-8"))["threads"]
    replies, contexts = [], []
    for t in threads:
        texts = [r["text"] for r in t["direct_replies"] if r["text"].strip()]
        for i, text in enumerate(texts):
            # What a writer answering this post would have seen: the post and the other replies.
            replies.append(text)
            contexts.append(f"@{t['author']}: {t['text']}\n" + "\n".join(texts[:i] + texts[i + 1:]))
    top3 = [r["text"] for t in threads for r in sorted(t["direct_replies"], key=lambda r: -r["likes"])[:3]]
    with open(config.ROOT / "data" / "devtools_virality_30d.csv", encoding="utf-8") as f:
        devtools = [TCO.sub("", row["Post Content"]).strip() for row in csv.DictReader(f)]
    devtools = [t for t in devtools if t]

    report = {
        "x_replies": _rates(replies, "reply"),
        "x_replies_with_thread_context": _rates(replies, "reply", contexts),
        "devtools_posts": _rates(devtools, "post"),
        "reply_length_chars": {
            "all_replies": {"p50": _pct([len(t) for t in replies], 0.5), "p90": _pct([len(t) for t in replies], 0.9)},
            "top3_replies": {"p50": _pct([len(t) for t in top3], 0.5), "p90": _pct([len(t) for t in top3], 0.9),
                             "mean": round(statistics.fmean(len(t) for t in top3), 1)},
        },
    }
    if args.drafts:
        drafts = json.loads((config.ROOT / args.drafts).read_text(encoding="utf-8"))
        by_method: dict[str, list[dict]] = {}
        for d in drafts:
            by_method.setdefault(d["method"], []).append(d)
        report["model_drafts"] = {
            m: _rates([d["text"] for d in ds], ds[0].get("kind", "reply")) for m, ds in sorted(by_method.items())
        }
    REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    for corpus in ("x_replies", "x_replies_with_thread_context", "devtools_posts"):
        r = report[corpus]
        print(f"{corpus}: n={r['n']} rejected={r['rejected_rate']:.1%} (style rules alone {r['rejected_by_style_rules_rate']:.1%})")
        for name, v in r["rules"].items():
            if v["hits"]:
                print(f"   {name:<18} {v['hits']:>4} ({v['rate']:.1%})  e.g. {v['examples'][0][:90]!r}")
    print("lengths:", report["reply_length_chars"])
    for m, r in report.get("model_drafts", {}).items():
        print(f"draft method {m}: n={r['n']} rejected={r['rejected_rate']:.1%} "
              + ", ".join(f"{k}={v['hits']}" for k, v in r["rules"].items() if v["hits"]))


if __name__ == "__main__":
    main()
