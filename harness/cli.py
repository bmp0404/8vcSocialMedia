import argparse
import json
from datetime import datetime

from harness import bandit, config, dataset, memory
from harness.backtest import backtest
from harness.evolve import _examples, evolve, leaderboard
from harness.learn import learn
from harness.hooks import sharpen_hooks
from harness.llm import get_llm
from harness.minds import decide
from harness.orient import orient
from harness.qualify import qualify
from harness.reward import score
from harness.review import serve
from harness.sources import observe
from harness.tournament import tournament


def run(args) -> None:
    llm = get_llm(args.provider)
    mission = config.mission()
    sources = args.sources.split(",") if args.sources else config.SOURCES

    signals = observe(sources, mission)
    if not signals:
        raise SystemExit("No signals observed; check HARNESS_SOURCES.")
    narratives = orient(llm, mission, signals, top=args.narratives)
    print(f"[orient] {len(narratives)} narratives: " + ", ".join(n.id for n in narratives))

    memory.sync_seed_minds(config.seed_minds())
    rows = memory.scored_actions()
    rewards = score(rows)
    active = {v["id"]: v for v in memory.variants("active")}
    chosen = bandit.choose(list(active), bandit.arm_stats(rows, rewards), k=args.minds)
    winners = [f"(ours) {e['text']}" for e in _examples(rows, rewards) if e["reward"] >= 0.7][:6]
    try:
        market = [f"({w['account']}, {w['engagement']} engagements) {w['text']}"
                  for w in dataset.exemplars(winners=8)["winners"]]
    except FileNotFoundError as e:
        print(f"[decide] {e}")
        market = []
    examples = winners + market

    actions = decide(
        llm, mission, [active[v] for v in chosen], narratives,
        lessons=config.lessons(), playbook=config.playbook(), examples=examples, per_mind=args.per_mind,
    )
    kept = qualify(llm, mission, actions)
    ranked = tournament(llm, mission, kept, rounds=args.rounds, field=args.field)
    ranked = sharpen_hooks(llm, mission, ranked, top=args.hooks)[: args.top]

    run_id = memory.save_run(actions)
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = config.OUT_DIR / f"recommendations-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(
        json.dumps({"run_id": run_id, "recommendations": [a.to_dict() for a in ranked]}, indent=2),
        encoding="utf-8",
    )

    print(f"\nTop {len(ranked)} recommendations (run {run_id}) -> {out}\n")
    for i, a in enumerate(ranked, 1):
        target = a.target_url or (f"@{a.target_handle}" if a.target_handle else "")
        elo = f"elo {a.elo} {a.record}" if a.elo is not None else ""
        hook = " *hook" if a.pre_hook_text else ""
        print(f"{i:>2}. [{a.predicted_score:.2f}] {elo:<14} {a.type:<7} {a.variant:<22} {a.text}{hook}  {target}")
    print("\nNext: `python -m harness review` to approve/kill, then `python -m harness evolve`.")


def record(args) -> None:
    with open(args.file, encoding="utf-8") as f:
        n = memory.record_outcomes(json.load(f))
    print(f"[record] stored {n} outcomes")


def stats(args) -> None:
    lb = leaderboard()
    print(f"{lb['scored']} scored actions ({lb['real_outcomes']} real outcomes)\n")
    print(f"{'variant':<26}{'status':<9}{'scored':>7}{'mean':>7}  parent / note")
    for v in lb["variants"]:
        print(f"{v['id']:<26}{v['status']:<9}{v['n']:>7}{v['mean']:>7.2f}  {v['parent']} / {v['note'] or ''}")
    print(f"\npanel weights: {lb['panel_weights'] or 'uncalibrated (all 1.0)'}")


def add_run_args(p) -> None:
    p.add_argument("--provider", help="override HARNESS_PROVIDER (anthropic|xai|openai|mock)")
    p.add_argument("--sources", help="comma-separated source plugins, overrides HARNESS_SOURCES")
    p.add_argument("--narratives", type=int, default=5)
    p.add_argument("--minds", type=int, default=5, help="how many mind variants the bandit picks per run")
    p.add_argument("--per-mind", type=int, default=4, help="drafts per mind variant (best-of-N pool)")
    p.add_argument("--rounds", type=int, default=4, help="pairwise tournament rounds (0 = off)")
    p.add_argument("--field", type=int, default=16, help="how many top drafts enter the tournament")
    p.add_argument("--hooks", type=int, default=5, help="top drafts that get the hook-rewrite pass (0 = off)")
    p.add_argument("--top", type=int, default=10)


def add_evolve_args(p) -> None:
    p.add_argument("--min-trials", type=int, default=3, help="scored drafts before a variant counts as tested")
    p.add_argument("--max-active", type=int, default=3, help="max live variants per mind")
    p.add_argument("--spawn", action="store_true", help="also invent a brand-new mind")


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness", description="Self-improving X harness for Coordigent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="observe -> orient -> decide -> qualify, write ranked recommendations")
    add_run_args(p)
    p.set_defaults(func=run)

    p = sub.add_parser("review", help="open the approve/kill dashboard; writes out/approved-run<id>.json")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--provider", help="LLM provider for the dashboard's Evolve button")
    p.set_defaults(func=lambda a: serve(a.port, not a.no_browser, a.provider))

    p = sub.add_parser("record", help="store real X metrics: JSON list of {action_id, metrics}")
    p.add_argument("file")
    p.set_defaults(func=record)

    p = sub.add_parser("learn", help="rewrite memory/lessons.md from reviews + metrics (evolve also does this)")
    p.add_argument("--provider")
    p.set_defaults(func=lambda a: learn(get_llm(a.provider)))

    p = sub.add_parser("backtest", help="grade the panel + judge against real engagement in the dev-tool dataset")
    p.add_argument("--provider")
    p.add_argument("--per-category", type=int, default=10, help="posts sampled per virality category")
    p.add_argument("--pairs", type=int, default=20, help="viral-vs-flop pairs for the judge")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--calibrate", action="store_true", help="apply the fitted panel weights")
    p.set_defaults(func=lambda a: backtest(get_llm(a.provider), a.per_category, a.pairs, a.seed, a.calibrate))

    p = sub.add_parser("evolve", help="learn from reviews + metrics: lessons, playbook, new prompt variants")
    p.add_argument("--provider")
    add_evolve_args(p)
    p.set_defaults(func=lambda a: evolve(get_llm(a.provider), a.min_trials, a.max_active, a.spawn))

    p = sub.add_parser("cycle", help="evolve, then run")
    add_run_args(p)
    add_evolve_args(p)
    p.set_defaults(func=lambda a: (evolve(get_llm(a.provider), a.min_trials, a.max_active, a.spawn), run(a)))

    p = sub.add_parser("stats", help="leaderboard of mind variants")
    p.set_defaults(func=stats)

    args = parser.parse_args()
    args.func(args)
