"""Detection and veto pass rate together, on the same 19 threads as reply_experiment.

Iterate's v3 retest showed lineup detection alone can't be the bar: the continuation lane fooled
judges but wrote as the other team's fans, so the mission-fit veto passed only 0.259 of its drafts
against 0.556 for the persona prompt. This script measures three writers under one frozen veto
(the current harness.minds.veto prompt, Opus 5.5, two orders):

  pipeline_v2  the current reply lane: planning call (side), page signed in as the account,
               rival threads banter, gate, veto, random pick
  pipeline_v1  the drafts the earlier lane wrote (cached in reply_experiment_raw.json), re-vetoed
  naive        the persona prompt's drafts (cached), re-vetoed

Veto pass rate = drafts that pass the veto / drafts written (a gate failure counts as a fail).
Detection = the reply_experiment lineup, by three judges, on the draft each arm would ship.

    HARNESS_PROVIDER=anthropic uv run python -m harness.minds.lab.side_experiment
"""

import json
import random
from concurrent.futures import ThreadPoolExecutor

from harness import config
from harness.llm import AnthropicLLM
from harness.minds import gate, lanes, veto, writer
from harness.minds.lab.reply_experiment import (
    CONTEXT, EXP_DIR, GEN_MODEL, JUDGE2_MODEL, JUDGE3_MODEL, JUDGE_MODEL, GATEWAY_REPORTED, RAW as REPLY_RAW,
    Cache, judge_lineup, load_threads, page_replies, rate,
)
from harness.minds.settings import ActionConfig
from harness.models import Narrative

RAW = EXP_DIR / "side_experiment_raw.json"
SUMMARY = EXP_DIR / "side_experiment.json"


def target_for(t: dict) -> dict:
    n = Narrative(id=t["id"], title=f"@{t['author']} post", summary=t["text"][:200], relevance=0.8, velocity=0.8)
    return {"url": t["url"], "author": t["author"], "text": t["text"], "age_minutes": 5.0, "replies": t["replies"],
            "thread": [{"author": f"r{i}", "text": r["text"], "likes": r["likes"]} for i, r in enumerate(page_replies(t))],
            "narrative": n}


def veto_context(t: dict) -> str:
    page = [r["text"] for r in page_replies(t)]
    return (f"POST by @{t['author']}:\n{t['text']}\n\nTHREAD:\n" + "\n".join(f"- {r}" for r in page)
            + f"\n\nFACTS:\n{config.mission()}\n\n{CONTEXT}")


def run_v2(t: dict) -> dict:
    llm, cfg, mission = AnthropicLLM(GEN_MODEL), ActionConfig(), config.mission()
    tgt = target_for(t)
    plans, err = lanes.plan_targets(llm, mission, config.lessons(), [tgt])
    plan = plans.get(t["url"])
    if plan is None or plan["tool"] == "abstain":
        return {"planned": plan, "plan_error": err, "abstained": (plan or {}).get("reason", "no_angle"), "candidates": []}
    d, a = lanes.answer_target(llm, mission, f"{mission}\n\n{CONTEXT}", tgt, cfg, fetch=lambda u: [],
                               intent=plan.get("intent", ""), side=plan.get("side", "neutral"),
                               account=writer.load_account())
    out = {"planned": plan, "candidates": d.candidates, "detail": d.detail}
    if a is None:
        out["abstained"] = d.reason
    else:
        out["text"], out["method"] = a.text, a.draft_meta["method"]
    return out


def reveto(t: dict, texts: list[str]) -> list[dict]:
    """The current veto on drafts an earlier arm wrote. Gate failures count as fails."""
    cfg, mission, ctx = ActionConfig(), config.mission(), veto_context(t)
    passing = [x for x in texts if gate.check(x, "reply", context=ctx, post=t["text"]).passed]
    results = {r.text: r for r in veto.veto(AnthropicLLM(GEN_MODEL), cfg, passing, lane="reply", context=ctx, mission=mission)}
    return [{"text": x, "gate_passed": x in results, "veto": results[x].evidence() if x in results else None} for x in texts]


def summarize_arm(rows: list[dict]) -> dict:
    drafts = [c for r in rows for c in r]
    passed = [c for c in drafts if (c.get("veto") or {}).get("passed")]
    whys = {}
    for c in drafts:
        v = c.get("veto") or {}
        if v and not v.get("passed"):
            for w in v.get("why", "").split(", "):
                whys[w] = whys.get(w, 0) + 1
    return {"drafts": len(drafts), "veto_passed": len(passed), "veto_pass_rate": round(len(passed) / max(len(drafts), 1), 3),
            "gate_failed": sum(1 for c in drafts if c.get("gate") or c.get("gate_passed") is False),
            "veto_reasons": dict(sorted(whys.items(), key=lambda kv: -kv[1]))}


def main() -> None:
    test, _ = load_threads(19)
    cache = Cache(RAW)
    old = json.loads(REPLY_RAW.read_text(encoding="utf-8"))
    with ThreadPoolExecutor(4) as pool:
        v2 = dict(zip([t["id"] for t in test], pool.map(lambda t: cache.get_or(f"v2|{t['id']}", lambda: run_v2(t)), test)))

        def old_texts(t, arm):
            g = old.get(f"gen|{t['id']}|{arm}", {})
            if arm == "naive":
                return [g["text"]] if g.get("text") else []
            return [c["text"] for c in g.get("candidates", []) if c.get("text")]

        rv1 = dict(zip([t["id"] for t in test], pool.map(
            lambda t: cache.get_or(f"reveto|{t['id']}|pipeline", lambda: reveto(t, old_texts(t, "pipeline"))), test)))
        rnaive = dict(zip([t["id"] for t in test], pool.map(
            lambda t: cache.get_or(f"reveto|{t['id']}|naive", lambda: reveto(t, old_texts(t, "naive"))), test)))

        def shipped(t, rows):
            ok = [c["text"] for c in rows if (c.get("veto") or {}).get("passed")]
            return random.Random(t["url"]).choice(ok) if ok else None

        picks = {("v2", t["id"]): v2[t["id"]].get("text") for t in test}
        picks |= {("pipeline_v1", t["id"]): shipped(t, rv1[t["id"]]) for t in test}
        picks |= {("naive", t["id"]): shipped(t, rnaive[t["id"]]) for t in test}
        futures = {}
        for (arm, tid), text in picks.items():
            if not text:
                continue
            t = next(x for x in test if x["id"] == tid)
            for jname, model in (("los", JUDGE_MODEL), ("artsa", JUDGE2_MODEL), ("opus", JUDGE3_MODEL)):
                key = f"lineup|{arm}|{tid}|{jname}"
                futures[key] = pool.submit(cache.get_or, key, lambda t=t, text=text, model=model, arm=arm:
                                           judge_lineup(t, text, f"{t['id']}{arm}", model))
        lineups = {k: f.result() for k, f in futures.items()}

    def detection(arm, jname):
        xs = [lineups[f"lineup|{arm}|{t['id']}|{jname}"]["detected"] for t in test
              if "detected" in lineups.get(f"lineup|{arm}|{t['id']}|{jname}", {})]
        return rate(xs)

    v2_rows = [v2[t["id"]].get("candidates", []) for t in test]
    summary = {
        "threads": len(test),
        "veto": {"model": ActionConfig().veto_model, "orders": 2, "checks": "p_ai, unsupported, off_mission, risk"},
        "judges": {j: GATEWAY_REPORTED[j] for j in (JUDGE_MODEL, JUDGE2_MODEL, JUDGE3_MODEL)},
        "arms": {
            "pipeline_v2": {**summarize_arm(v2_rows), "shipped": sum(1 for t in test if v2[t["id"]].get("text")),
                            "abstained": {r: sum(1 for t in test if v2[t["id"]].get("abstained") == r)
                                          for r in {v2[t["id"]].get("abstained") for t in test} if r},
                            "sides": {s: sum(1 for t in test if (v2[t["id"]].get("planned") or {}).get("side") == s)
                                      for s in ("own", "rival", "neutral")},
                            "methods": {m: sum(1 for t in test if v2[t["id"]].get("method") == m)
                                        for m in ("continuation", "banter", "styled")}},
            "pipeline_v1_reveto": {**summarize_arm([rv1[t["id"]] for t in test]),
                                   "shipped": sum(1 for t in test if picks[("pipeline_v1", t["id"])])},
            "naive_reveto": {**summarize_arm([rnaive[t["id"]] for t in test]),
                             "shipped": sum(1 for t in test if picks[("naive", t["id"])])},
        },
    }
    for arm, key in (("pipeline_v2", "v2"), ("pipeline_v1_reveto", "pipeline_v1"), ("naive_reveto", "naive")):
        summary["arms"][arm]["detection_on_shipped"] = {j: detection(key, j) for j in ("opus", "artsa", "los")}
    summary["shipped_v2"] = [{"thread": t["url"], "side": (v2[t["id"]].get("planned") or {}).get("side"),
                              "method": v2[t["id"]].get("method"), "text": v2[t["id"]].get("text"),
                              "abstained": v2[t["id"]].get("abstained")} for t in test]
    SUMMARY.write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary["arms"], indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
