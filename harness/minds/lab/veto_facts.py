"""Does showing the veto the live facts the gate leaned on change what it rejects? Writes
action/experiments/veto_facts.json.

Two arms on the same batches: the production veto (`veto.veto`: claude-opus-5-5, both orders, a split counts as a
veto) with and without the LIVE FACTS block `lanes.veto_facts` builds. The gate runs with facts in both arms, as in
production, so only drafts it passes reach the veto. The sets are the ones fact_grounding measured the gate on:

- Real replies, for false rejections. wnba: the 24 threads in action/fixtures/x_threads.json with the ESPN facts live
  at each post. devtools: the corpus reply samples with the author's own posts as facts (local files only, so only
  counts are written). Only a reply the gate passed by leaning on a fact gets a block, so only those can differ
  between arms; they are judged in batches of up to 6 per thread. A false rejection is the veto rejecting one.
- Invented stats, for catches. The drafts fact_grounding cached (claude-sonnet-5-5 told to make a number up), the
  ones with a number. Each thread's gate-passing drafts are one batch. A batch the gate passed without leaning on a
  fact has the same input in both arms, so it is judged once and counts for both.

The veto's context mirrors the hot path's: the post, the thread's other replies, and the mission plus the post as
the facts block. Verdicts are cached in data/context/veto_facts_cache.jsonl (gitignored), so a rerun pays only for
new batches.

    HARNESS_PROVIDER=anthropic uv run python -m harness.minds.lab.veto_facts \\
        --context-dir <dir with x_metrics.jsonl, author_timelines.jsonl, reply_samples.jsonl> --drafts <cache.jsonl>
"""

import argparse
import hashlib
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from harness import config
from harness.minds import gate, lanes, veto
from harness.minds.lab import fact_grounding as fg
from harness.minds.settings import ActionConfig
from harness.models import Narrative

REPORT = config.ROOT / "action" / "experiments" / "veto_facts.json"
CACHE = config.ROOT / "data" / "context" / "veto_facts_cache.jsonl"
BATCH = 6  # ActionConfig.reply_samples: the most drafts one veto call sees in production


def veto_context(t: dict, others: list[str], mission: str) -> str:
    """What answer_target hands the veto for this post, without live facts: post, thread, handles, facts block."""
    block = lanes.facts_block(mission, [Narrative(id="m", title=f"@{t['author']} just posted", summary=t["text"])])
    return (f"POST by @{t['author']}:\n{t['text']}\n\nTHREAD:\n" + "\n".join(f"- {r}" for r in others)
            + f"\n\nHANDLES IN THREAD: \n\nFACTS:\n{block}")


def with_block(context: str, lines: list[str]) -> str:
    return context + ("\n\n" + lanes.VETO_FACTS_HEADER + "\n" + "\n".join(lines) if lines else "")


class Judge:
    """veto.veto with a verdict cache and a hard call budget (two calls per judged batch)."""

    def __init__(self, llm, cfg: ActionConfig, budget: int):
        self.llm, self.cfg, self.budget, self.spent = llm, cfg, budget, 0
        self.lock = threading.Lock()
        self.cache = {}
        if CACHE.exists():
            for row in fg._read_jsonl(CACHE):
                self.cache[row["key"]] = row["results"]

    def __call__(self, texts: list[str], context: str, mission: str) -> list[dict]:
        key = hashlib.sha256(json.dumps([texts, context, mission, self.cfg.veto_model]).encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        with self.lock:
            if self.spent + 2 > self.budget:
                raise RuntimeError(f"veto call budget of {self.budget} reached")
            self.spent += 2
        results = [r.evidence() | {"text": r.text} for r in
                   veto.veto(self.llm, self.cfg, texts, lane="reply", context=context, mission=mission)]
        with self.lock:
            self.cache[key] = results
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            with CACHE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "results": results}, ensure_ascii=False) + "\n")
        return results


def unsupported(ev: dict) -> bool:
    return any((ev.get(o) or {}).get("unsupported") for o in ("fwd", "rev"))


# ---------------------------------------------------------------- the two sets


def real_batches(threads: list[dict], mission: str) -> list[dict]:
    """Batches of replies the gate passed only by leaning on a fact, with both arms' contexts."""
    out = []
    for k, t in enumerate(threads):
        idx = fg.index_for(t)
        changed = []
        for i, text in enumerate(t["replies"]):
            ctx = f"@{t['author']}: {t['text']}\n" + "\n".join(t["replies"][:i] + t["replies"][i + 1:])
            if gate.check(text, "reply", context=ctx, facts=idx).passed and not gate.check(text, "reply", context=ctx).passed:
                changed.append(text)
        changed = list(dict.fromkeys(changed))
        for j in range(0, len(changed), BATCH):
            texts = changed[j: j + BATCH]
            base = veto_context(t, [r for r in t["replies"] if r not in texts], mission)
            out.append({"thread": k, "texts": texts, "base": base, "lines": lanes.veto_facts(texts, base, t["text"], idx)})
    return out


def invented_batches(threads: list[dict], drafts: dict, profile: str, batch: str, mission: str) -> tuple[list[dict], dict]:
    """Batches of invented-stat drafts the gate passed, and the gate's counts, for one draft batch."""
    out, counts = [], {"drafts_with_a_number": 0, "gate_rejected": 0, "grounding_caught": 0}
    for k, t in enumerate(threads):
        key = (batch, profile, f"@{t['author']}: {t['text']}")
        if key not in drafts:
            raise KeyError(f"no cached {batch} drafts for {profile} post {key[2][:60]!r}; run fact_grounding --invent")
        idx = fg.index_for(t)
        ctx = f"@{t['author']}: {t['text']}\n" + "\n".join(t["replies"])
        passing = []
        for d in drafts[key]:
            if not gate.NUMBER.search(d):
                continue  # fact_grounding skips these too: the model ignored the instruction
            res = gate.check(d, "reply", context=ctx, facts=idx)
            counts["drafts_with_a_number"] += 1
            counts["grounding_caught"] += any(f.rule in gate.GROUNDING_RULES for f in res.findings)
            if res.passed:
                passing.append(d)
            else:
                counts["gate_rejected"] += 1
        passing = list(dict.fromkeys(passing))
        for j in range(0, len(passing), BATCH):
            texts = passing[j: j + BATCH]
            base = veto_context(t, t["replies"], mission)
            out.append({"thread": k, "texts": texts, "base": base, "lines": lanes.veto_facts(texts, base, t["text"], idx)})
    return out, counts


# ---------------------------------------------------------------- judging and counting


def judge_all(judge: Judge, batches: list[dict], mission: str, workers: int) -> None:
    """Fill each batch's "without" and "with" verdicts. A batch without a block is judged once for both arms."""
    def one(b):
        b["without"] = judge(b["texts"], b["base"], mission)
        b["with"] = judge(b["texts"], with_block(b["base"], b["lines"]), mission) if b["lines"] else b["without"]

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, batches))


def paired(batches: list[dict], flag) -> dict:
    """Counts of `flag` per arm over every judged text, and with-minus-without with a thread-bootstrap interval."""
    before, after, threads, changed = [], [], [], []
    for b in batches:
        for w, f in zip(b["without"], b["with"]):
            before.append(flag(w))
            after.append(flag(f))
            threads.append(b["thread"])
            if flag(w) != flag(f):
                changed.append({"text": w["text"], "without": w["why"] or "passed", "with": f["why"] or "passed"})
    out = {"judged": len(before), "without": sum(before), "with": sum(after)}
    out["change"] = fg._paired_change(before, after, threads) if before else None
    return out | {"flipped": changed}


def summarize_real(batches: list[dict], examples: bool) -> dict:
    out = {"batches": len(batches), "batches_with_a_block": sum(bool(b["lines"]) for b in batches),
           "rejected": paired(batches, lambda v: not v["passed"]),
           "flagged_unsupported": paired(batches, unsupported)}
    if not examples:
        for k in ("rejected", "flagged_unsupported"):
            out[k]["flipped"] = len(out[k]["flipped"])
    elif batches:
        out["block_example"] = next((b["lines"] for b in batches if b["lines"]), [])
    return out


def summarize_invented(batches: list[dict], counts: dict) -> dict:
    caught = paired(batches, lambda v: not v["passed"])
    n = counts["drafts_with_a_number"]
    return {**counts, "reached_veto": caught["judged"], "batches_with_a_block": sum(bool(b["lines"]) for b in batches),
            "veto_caught": {"without": caught["without"], "with": caught["with"]},
            "caught_in_total": {"without": counts["gate_rejected"] + caught["without"],
                                "with": counts["gate_rejected"] + caught["with"], "of": n},
            "veto_flagged_unsupported": {k: v for k, v in paired(batches, unsupported).items() if k in ("without", "with")},
            "flipped": caught["flipped"]}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--context-dir", type=Path, default=fg.CONTEXT, help="where the devtools corpus jsonl files are")
    p.add_argument("--drafts", type=Path, default=fg.DRAFTS, help="fact_grounding's invented-draft cache")
    p.add_argument("--budget", type=int, default=700, help="hard cap on veto calls")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--dry-run", action="store_true", help="build the batches and count the calls; judge nothing")
    args = p.parse_args()

    espn = fg._MemoEspn()
    sets = {"wnba": fg.wnba_threads(espn), "devtools": fg.devtools_threads(args.context_dir)}
    missions = {}
    for profile in sets:
        with config.use_profile(profile):
            missions[profile] = config.mission()
    drafts = {(d.get("batch", "design"), d["profile"], d["post"]): d["replies"] for d in fg._read_jsonl(args.drafts)}
    invent_threads = {"wnba": sets["wnba"],
                      "devtools": random.Random(0).sample(sets["devtools"], min(fg.DEVTOOLS_INVENT_THREADS, len(sets["devtools"])))}

    real = {pr: real_batches(ts, missions[pr]) for pr, ts in sets.items()}
    invented = {(pr, b): invented_batches(invent_threads[pr], drafts, pr, b, missions[pr])
                for pr in sets for b in ("design", "heldout")}
    all_batches = [b for bs in real.values() for b in bs] + [b for bs, _ in invented.values() for b in bs]
    calls = sum(2 + 2 * bool(b["lines"]) for b in all_batches)
    print(f"[veto_facts] {len(all_batches)} batches, at most {calls} veto calls")
    if args.dry_run:
        for pr, bs in real.items():
            print(pr, "real:", len(bs), "batches,", sum(len(b["texts"]) for b in bs), "replies,",
                  sum(bool(b["lines"]) for b in bs), "with a block")
        for (pr, b), (bs, counts) in invented.items():
            print(pr, b, "invented:", counts, len(bs), "batches,", sum(bool(x["lines"]) for x in bs), "with a block")
        return

    from harness.llm import get_llm
    from harness.watcher import modelrec

    modelrec.install()
    judge = Judge(get_llm("anthropic"), replace(ActionConfig(), veto_parallel_orders=True), args.budget)
    with modelrec.recording() as recorded:
        for profile in sets:
            judge_all(judge, real[profile], missions[profile], args.workers)
            for b in ("design", "heldout"):
                judge_all(judge, invented[(profile, b)][0], missions[profile], args.workers)
    report = {
        "veto_model": judge.cfg.veto_model,
        "calls": {"made": len(recorded), "from_cache_or_earlier_runs": calls - len(recorded),
                  "models": sorted({(c["requested"], c["reported"]) for c in recorded})},
        "wnba_real_replies": summarize_real(real["wnba"], examples=True),
        "devtools_real_replies": summarize_real(real["devtools"], examples=False),
    }
    for (pr, b), (bs, counts) in invented.items():
        report[f"{pr}_invented_stats{'' if b == 'design' else '_heldout'}"] = summarize_invented(bs, counts)
    REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: ({kk: vv for kk, vv in v.items() if kk not in ("flipped", "block_example")}
                          if isinstance(v, dict) else v) for k, v in report.items()}, indent=1, default=str))


if __name__ == "__main__":
    main()
