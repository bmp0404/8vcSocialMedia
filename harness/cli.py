import argparse
import json
from datetime import datetime

from harness import config, memory
from harness.learn import learn
from harness.llm import get_llm
from harness.minds import decide
from harness.orient import orient
from harness.qualify import qualify
from harness.review import serve
from harness.sources import observe


def run(args) -> None:
    llm = get_llm(args.provider)
    mission = config.mission()
    sources = args.sources.split(",") if args.sources else config.SOURCES

    signals = observe(sources, mission)
    if not signals:
        raise SystemExit("No signals observed; check HARNESS_SOURCES.")
    narratives = orient(llm, mission, signals, top=args.narratives)
    print(f"[orient] {len(narratives)} narratives: " + ", ".join(n.id for n in narratives))
    actions = decide(llm, mission, config.lessons(), narratives, per_mind=args.per_mind)
    ranked = qualify(llm, mission, actions)[: args.top]

    run_id = memory.save_run(actions)
    config.OUT_DIR.mkdir(exist_ok=True)
    out = config.OUT_DIR / f"recommendations-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(
        json.dumps({"run_id": run_id, "recommendations": [a.to_dict() for a in ranked]}, indent=2),
        encoding="utf-8",
    )

    print(f"\nTop {len(ranked)} recommendations (run {run_id}) -> {out}\n")
    for i, a in enumerate(ranked, 1):
        target = a.target_url or (f"@{a.target_handle}" if a.target_handle else "")
        print(f"{i:>2}. [{a.predicted_score:.2f}] {a.type:<7} {a.mind:<18} {a.text}  {target}")


def record(args) -> None:
    with open(args.file, encoding="utf-8") as f:
        n = memory.record_outcomes(json.load(f))
    print(f"[record] stored {n} outcomes")


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness", description="Media harness for the X fan account")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="observe -> orient -> decide -> qualify, write ranked recommendations")
    p.add_argument("--provider", help="override HARNESS_PROVIDER (anthropic|xai|openai|mock)")
    p.add_argument("--sources", help="comma-separated source plugins, overrides HARNESS_SOURCES")
    p.add_argument("--narratives", type=int, default=5)
    p.add_argument("--per-mind", type=int, default=3)
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=run)

    p = sub.add_parser("record", help="store observed metrics: JSON list of {action_id, metrics}")
    p.add_argument("file")
    p.set_defaults(func=record)

    p = sub.add_parser("learn", help="rewrite memory/lessons.md from predicted vs. actual")
    p.add_argument("--provider")
    p.set_defaults(func=lambda a: learn(get_llm(a.provider)))

    p = sub.add_parser("review", help="open the approve/kill dashboard; writes out/approved-run<id>.json")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=lambda a: serve(a.port, not a.no_browser))

    args = parser.parse_args()
    args.func(args)
