"""Evolve the evaluators (tournament judge and audience panel) against real engagement in the dataset.

Accounts are split 50/50 into a dev set and a holdout set. Each iteration scores the current instructions on
dev, asks the LLM to revise them using the cases they got wrong, and scores the candidate. A candidate is
adopted only if it beats the current version on dev *and* gains at least MIN_HOLDOUT_GAIN on holdout accounts it never saw,
so a prompt can't win by memorizing companies. Winners are stored in kv ("judge_core", "panel_core") and
used by every run; the fixed rules (fact sheet, ads lose) live outside the evolvable part.

- Judge metric: accuracy on same-account pairs (one viral post, one flop from the same company), which
  removes account size and fame and leaves the writing. 0.5 is chance.
- Panel metric: Spearman of the weighted panel score vs. each post's engagement relative to its own account.

Writes eval/tune-<timestamp>.json and appends to kv "tune_history".
"""

import json
import random
from collections import defaultdict
from datetime import datetime

from harness import config, dataset, memory
from harness.llm import LLM
from harness.metrics import _ranks, spearman
from harness.models import Action
from harness.qualify import PANEL_CORE, panel_scores, panel_system, weighted_score
from harness.tournament import JUDGE_CORE, judge, judge_system
from harness.virality import training_set

# A candidate must beat the current version on holdout by this much. With 30 pairs one pair is 0.033, and a
# one-pair "win" is noise: the first real tune adopted a genre bias (announcements) on exactly that.
MIN_HOLDOUT_GAIN = 0.05

TUNE_SYSTEM = """You improve the instructions an LLM evaluator follows when predicting how developer audiences
on X react to posts. You get the current instructions and the cases it got wrong, with what really happened.
Revise the instructions so they capture general, transferable signals of writing craft that make developers
engage (hooks, specificity, story, stakes, clarity, emotional pull) and fix the pattern behind the misses.
The evaluator will judge creative, story-driven drafts from a small dev-tool account, so don't add a
preference for a genre such as launch announcements, and never mention specific companies, products, or
posts. Keep the evaluator's job and output unchanged and stay under 150 words. Return JSON:
{"core": "the revised instructions", "change": "one line: what changed and why"}"""


def _post_action(p: dataset.Post) -> Action:
    # mind "dataset" + narrative_id = account lets the judge's retrieval hide the post's own company.
    return Action(type="post", text=p.clean_text, mind="dataset", narrative_id=p.account)


def _split(rng: random.Random):
    texts, z, accounts, posts = training_set()
    accts = sorted(set(accounts))
    rng.shuffle(accts)
    dev_accts = set(accts[: len(accts) // 2])
    rows = [(p, zi) for p, zi in zip(posts, z) if len(p.clean_text) >= 20]
    return ([r for r in rows if r[0].account in dev_accts], [r for r in rows if r[0].account not in dev_accts])


def _pairs(rows, n: int, rng: random.Random) -> list[tuple[Action, Action, dataset.Post, dataset.Post]]:
    by_acct = defaultdict(lambda: ([], []))
    for p, _ in rows:
        if p.category in dataset.VIRAL:
            by_acct[p.account][0].append(p)
        elif p.category == "Deadweight":
            by_acct[p.account][1].append(p)
    pool = [(h, f) for hits, flops in by_acct.values() for h in hits for f in flops]
    rng.shuffle(pool)
    out, used = [], set()
    for h, f in pool:  # each post at most once, so a few prolific accounts don't dominate
        if h.url in used or f.url in used:
            continue
        used |= {h.url, f.url}
        out.append((_post_action(h), _post_action(f), h, f))
        if len(out) == n:
            break
    return out


def _posts(rows, n: int, rng: random.Random):
    by_cat = defaultdict(list)
    for p, z in rows:
        by_cat[p.category].append((p, z))
    per = max(1, n // max(1, len(by_cat)))
    return [r for cat in sorted(by_cat) for r in rng.sample(by_cat[cat], min(per, len(by_cat[cat])))]


def eval_judge(llm: LLM, core: str, pairs, seed: int) -> tuple[float, list[dict]]:
    picks = judge(llm, config.mission(), [(h, f) for h, f, _, _ in pairs], random.Random(seed), core=core)
    right, misses = [], []
    for (h, f, hp, fp), w in zip(pairs, picks):
        if w is None:
            continue
        right.append(w is h)
        if w is not h:
            misses.append({"got_more_engagement": hp.clean_text[:280], "got_less": fp.clean_text[:280],
                           "engagement": [hp.engagement, fp.engagement]})
    return (round(sum(right) / len(right), 3) if right else 0.0), misses


def eval_panel(llm: LLM, core: str, rows) -> tuple[float, list[dict]]:
    acts = [_post_action(p) for p, _ in rows]
    scores = panel_scores(llm, acts, core=core)
    weights = memory.kv_get("panel_weights", {})
    pred = [weighted_score(scores.get(a.id, {}), weights) for a in acts]
    z = [zi for _, zi in rows]
    rp, rz = _ranks(pred), _ranks(z)
    errors = sorted(range(len(rows)), key=lambda i: abs(rp[i] - rz[i]), reverse=True)[:8]  # worst rank misses
    misses = [{"text": rows[i][0].clean_text[:280], "panel_score": pred[i],
               "actual": "above this account's usual" if z[i] > 0 else "below this account's usual"} for i in errors]
    return spearman(pred, z), misses


def _propose(llm: LLM, what: str, core: str, misses: list[dict], current_core: str) -> dict:
    return llm.complete_json(
        TUNE_SYSTEM,
        f"EVALUATOR: {what}\n\nCURRENT INSTRUCTIONS:\n{core}\n\nCASES IT GOT WRONG:\n{json.dumps(misses, indent=1)}",
        mock=lambda: {"core": current_core + "\n(mock revision)", "change": "mock"},
    )


def _tune_one(llm: LLM, name: str, current: str, evaluate, iterations: int) -> dict:
    dev_score, misses = evaluate(current, "dev")
    hold_score = None
    log = {"start": {"dev": dev_score}, "iterations": [], "adopted": False}
    print(f"[tune] {name}: current dev {dev_score:.3f}")
    for i in range(iterations):
        out = _propose(llm, name, current, misses, current)
        cand = (out.get("core") or "").strip()
        if not cand:
            print(f"[tune] {name}: iteration {i + 1} returned no instructions")
            continue
        cand_dev, cand_misses = evaluate(cand, "dev")
        step = {"change": out.get("change", ""), "dev": cand_dev, "core": cand}
        if cand_dev > dev_score:
            if hold_score is None:
                hold_score, _ = evaluate(current, "holdout")
            cand_hold, _ = evaluate(cand, "holdout")
            step["holdout"] = cand_hold
            step["holdout_current"] = hold_score
            if cand_hold >= hold_score + MIN_HOLDOUT_GAIN:
                current, dev_score, hold_score, misses = cand, cand_dev, cand_hold, cand_misses
                step["adopted"] = log["adopted"] = True
        log["iterations"].append(step)
        verdict = "ADOPTED" if step.get("adopted") else "rejected"
        print(f"[tune] {name}: iteration {i + 1}: dev {cand_dev:.3f}"
              + (f", holdout {step['holdout']:.3f} vs {step['holdout_current']:.3f}" if "holdout" in step else "")
              + f" -> {verdict}  ({step['change']})")
    log["final"] = {"dev": dev_score, "holdout": hold_score, "core": current}
    return log


def tune(llm: LLM, target: str = "both", iterations: int = 2, pairs: int = 30, posts: int = 30, seed: int = 0) -> dict:
    rng = random.Random(seed)
    dev, hold = _split(rng)
    report = {"at": datetime.now().isoformat(timespec="seconds"), "provider": config.PROVIDER, "seed": seed}

    if target in ("judge", "both"):
        sets = {"dev": _pairs(dev, pairs, rng), "holdout": _pairs(hold, pairs, rng)}
        print(f"[tune] judge: {len(sets['dev'])} dev pairs, {len(sets['holdout'])} holdout pairs (same-account)")
        current = memory.kv_get("judge_core") or JUDGE_CORE
        report["judge"] = _tune_one(llm, "judge", current, lambda core, s: eval_judge(llm, core, sets[s], seed), iterations)
        if report["judge"]["adopted"]:
            memory.kv_set("judge_core", report["judge"]["final"]["core"])

    if target in ("panel", "both"):
        sets = {"dev": _posts(dev, posts, rng), "holdout": _posts(hold, posts, rng)}
        print(f"[tune] panel: {len(sets['dev'])} dev posts, {len(sets['holdout'])} holdout posts")
        current = memory.kv_get("panel_core") or PANEL_CORE
        report["panel"] = _tune_one(llm, "panel", current, lambda core, s: eval_panel(llm, core, sets[s]), iterations)
        if report["panel"]["adopted"]:
            memory.kv_set("panel_core", report["panel"]["final"]["core"])

    history = memory.kv_get("tune_history", [])
    history.append({k: report[k] for k in report if k in ("at", "provider")} |
                   {t: {"adopted": report[t]["adopted"], **{k: v for k, v in report[t]["final"].items() if k != "core"}}
                    for t in ("judge", "panel") if t in report})
    memory.kv_set("tune_history", history[-20:])
    config.EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out = config.EVAL_DIR / f"tune-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[tune] report -> {out}")
    print(f"[tune] judge instructions in use:\n{judge_system()}\n[tune] panel instructions in use:\n{panel_system()}")
    return report
