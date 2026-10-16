"""Self-improvement step. Run after reviewing drafts and/or recording real X metrics.

1. Calibrate: reweight audience-panel members by how well they predicted real outcomes.
2. Lessons:   rewrite memory/lessons.md from what was approved, killed, and what actually performed.
3. Playbook:  distill winning hooks/formats (ours + the dev-tool dataset) into memory/playbook.md.
4. Mutate:    for each mind whose variants are all tested, rewrite its best prompt into a challenger.
5. Retire:    keep at most `max_active` variants per mind; drop the weakest tested ones.
6. Spawn:     optionally invent a brand-new mind for a lane nobody covers yet.
The bandit (bandit.py) then decides which variants draft the next run.
"""

import difflib
import json
import re
from collections import defaultdict
from statistics import correlation, StatisticsError

from harness import config, dataset, memory
from harness.bandit import ArmStats, arm_stats
from harness.learn import write_lessons
from harness.llm import LLM
from harness.reward import Reward, score

MIN_CALIBRATION_OUTCOMES = 5


def _examples(rows: list[dict], rewards: dict[str, Reward]) -> list[dict]:
    out = []
    for r in rows:
        a, rw = r["action"], rewards.get(r["action"]["id"])
        if not rw:
            continue
        out.append({
            "variant": a.get("variant", ""), "mind": a["mind"], "type": a["type"],
            "text": r["edited_text"] or a["text"], "reward": round(rw.value, 2), "signal": rw.source,
            "human_note": r["note"], "reasons": r.get("reasons", []), "critic": a.get("critique", ""), "metrics": r["metrics"],
        })
    return sorted(out, key=lambda e: e["reward"], reverse=True)


def calibrate_panel(rows: list[dict], rewards: dict[str, Reward]) -> dict | None:
    real = [(r["action"].get("panel", {}), rewards[r["action"]["id"]].value)
            for r in rows if r["metrics"] and r["action"]["id"] in rewards]
    if len(real) < MIN_CALIBRATION_OUTCOMES:
        print(f"[evolve] calibrate: {len(real)}/{MIN_CALIBRATION_OUTCOMES} real outcomes, skipping")
        return None
    weights = {}
    for p in config.panel():
        pairs = [(panel[p], y) for panel, y in real if p in panel]
        try:
            corr = correlation([x for x, _ in pairs], [y for _, y in pairs])
        except (StatisticsError, ValueError):
            corr = 0.0
        weights[p] = round(max(0.2, 1 + corr), 2)
    memory.kv_set("panel_weights", weights)
    print(f"[evolve] calibrate: panel weights {weights}")
    return weights


PLAYBOOK_SYSTEM = """You are a viral-content strategist for a dev-tool company's X account. From the evidence
below (our own winners, plus real dev-tool posts that went viral or flopped, often from the same company),
distill a playbook of reusable patterns: hook templates, formats, structures, media, and angles, each with
a one-line example in the brand's voice. Include a short "avoid" list drawn from the flops. Max 12 patterns.
No product claims beyond the mission's fact sheet."""

MUTATE_SYSTEM = """You improve the system prompt of one AI content writer ("mind") for a company's X account.
Keep its lane and identity. Use the evidence to change one or two specific things that should make its
drafts spread further: a hypothesis, not a rewrite for its own sake. Keep it under 200 words and keep any
rule about sticking to the fact sheet. Return JSON: {"prompt": "...", "change": "one line: what changed and why"}"""

SPAWN_SYSTEM = """You design a new AI content writer ("mind") for a company's X account: a lane that the
existing minds don't cover but the evidence suggests could go viral with this audience. Return JSON:
{"name": "snake_case_name", "prompt": "system prompt under 150 words, written like the existing ones"}"""


def write_playbook(llm: LLM, examples: list[dict]) -> None:
    winners = [e for e in examples if e["reward"] >= 0.7][:15]
    try:
        market = dataset.exemplars()
    except FileNotFoundError as e:
        print(f"[evolve] playbook: {e}")
        market = {"winners": [], "flops": []}
    if not winners and not market["winners"]:
        print("[evolve] playbook: no winners yet, skipping")
        return
    prompt = (f"MISSION:\n{config.mission()}\n\nCURRENT PLAYBOOK:\n{config.playbook()}\n\n"
              f"OUR WINNING POSTS:\n{json.dumps(winners, indent=1)}\n\n"
              f"DEV-TOOL POSTS THAT WENT VIRAL:\n{json.dumps(market['winners'], indent=1)}\n\n"
              f"DEV-TOOL POSTS THAT FLOPPED (same companies):\n{json.dumps(market['flops'], indent=1)}")
    _write(config.PLAYBOOK_PATH, llm.complete(PLAYBOOK_SYSTEM, prompt))
    print(f"[evolve] playbook -> {config.PLAYBOOK_PATH} ({len(winners)} own winners, "
          f"{len(market['winners'])} dataset winners, {len(market['flops'])} flops)")


def mutate_and_retire(llm: LLM, examples: list[dict], stats: dict[str, ArmStats], min_trials: int, max_active: int) -> None:
    by_mind: dict[str, list[dict]] = defaultdict(list)
    for v in memory.variants("active"):
        by_mind[v["mind"]].append(v)

    for mind, vs in by_mind.items():
        st = lambda v: stats.get(v["id"], ArmStats())  # noqa: E731
        untested = [v for v in vs if st(v).n < min_trials]
        if untested:
            print(f"[evolve] {mind}: waiting on {', '.join(v['id'] for v in untested)} (<{min_trials} scored)")
            continue
        champ = max(vs, key=lambda v: st(v).mean)
        mine = [e for e in examples if e["variant"] == champ["id"]]
        evidence = {"best": mine[:3], "worst": mine[-3:], "lessons": config.lessons()}
        try:
            out = llm.complete_json(
                MUTATE_SYSTEM,
                f"CURRENT PROMPT ({champ['id']}, mean reward {st(champ).mean:.2f}):\n{champ['prompt']}\n\n"
                f"EVIDENCE:\n{json.dumps(evidence, indent=1)}",
                mock=lambda: {"prompt": champ["prompt"] + "\n(mock mutation)", "change": "mock"},
            )
            vid = memory.add_variant(mind, out["prompt"].strip(), parent=champ["id"], note=out.get("change", ""))
            vs.append({"id": vid})
            print(f"[evolve] {mind}: {champ['id']} -> challenger {vid}: {out.get('change', '')}")
        except Exception as e:  # noqa: BLE001
            print(f"[evolve] {mind}: mutation failed: {e}")

        tested = sorted((v for v in vs if st(v).n >= min_trials and v["id"] != champ["id"]), key=lambda v: st(v).mean)
        while len(vs) > max_active and tested:
            loser = tested.pop(0)
            memory.retire_variant(loser["id"], f"retired: mean {st(loser).mean:.2f} < champion {st(champ).mean:.2f}")
            vs = [v for v in vs if v["id"] != loser["id"]]
            print(f"[evolve] {mind}: retired {loser['id']}")


def spawn_mind(llm: LLM, examples: list[dict]) -> None:
    existing = {v["mind"]: v["prompt"][:200] for v in memory.variants("active")}
    out = llm.complete_json(
        SPAWN_SYSTEM,
        f"MISSION:\n{config.mission()}\n\nEXISTING MINDS:\n{json.dumps(existing, indent=1)}\n\n"
        f"EVIDENCE (best first):\n{json.dumps(examples[:15], indent=1)}",
        mock=lambda: {"name": "mock_spawn", "prompt": "You are a mock spawned mind."},
    )
    name = re.sub(r"[^a-z0-9_]", "", out.get("name", "").lower().replace(" ", "_"))
    if not name or name in existing:
        print(f"[evolve] spawn: rejected name '{out.get('name')}'")
        return
    vid = memory.add_variant(name, out["prompt"].strip(), parent="spawn", note="spawned by evolve")
    print(f"[evolve] spawned new mind {vid}")


def evolve(llm: LLM, min_trials: int = 3, max_active: int = 3, spawn: bool = False) -> None:
    memory.sync_seed_minds(config.seed_minds())
    rows = memory.scored_actions()
    rewards = score(rows)
    if not rewards:
        print("[evolve] nothing of ours scored yet; building the playbook from the dataset only")
        write_playbook(llm, [])
        return
    examples = _examples(rows, rewards)
    n_real = sum(r.source == "outcome" for r in rewards.values())
    print(f"[evolve] {len(rewards)} scored actions ({n_real} real outcomes, {len(rewards) - n_real} reviews)")

    calibrate_panel(rows, rewards)
    write_lessons(llm, examples)
    write_playbook(llm, examples)
    mutate_and_retire(llm, examples, arm_stats(rows, rewards), min_trials, max_active)
    if spawn:
        spawn_mind(llm, examples)


def leaderboard() -> dict:
    """Everything the dashboard's Learning tab and `stats` show."""
    memory.sync_seed_minds(config.seed_minds())
    rows = memory.scored_actions()
    rewards = score(rows)
    stats = arm_stats(rows, rewards)
    all_variants = memory.variants(None)
    by_id = {v["id"]: v for v in all_variants}
    out = []
    for v in all_variants:
        s = stats.get(v["id"], ArmStats())
        parent = by_id.get(v["parent"])
        diff = list(difflib.unified_diff(
            parent["prompt"].splitlines(), v["prompt"].splitlines(), lineterm="", n=1))[2:] if parent else []
        out.append({**v, "n": s.n, "mean": round(s.mean, 3), "diff": diff})
    out.sort(key=lambda v: (v["status"] != "active", -v["mean"], v["id"]))
    return {
        "variants": out,
        "scored": len(rewards),
        "real_outcomes": sum(r.source == "outcome" for r in rewards.values()),
        "panel": config.panel(),
        "panel_weights": memory.kv_get("panel_weights", {}),
        "playbook": config.playbook(),
        "lessons": config.lessons(),
    }


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
