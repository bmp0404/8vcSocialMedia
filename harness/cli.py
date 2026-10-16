import argparse
import json
from datetime import datetime

from harness import config, memory
from harness.learn import learn
from harness.llm import get_llm
from harness.minds import decide, write_decision_log
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
    lessons = config.lessons()
    batch = decide(llm, mission, lessons, narratives, per_mind=args.per_mind, signals=signals)
    actions, kept = batch, qualify(llm, mission, batch)
    for _ in range(args.retries):
        if kept or not batch:
            break
        # The critic vetoed every draft, so each mind revises its own drafts against the critiques.
        batch = decide(llm, mission, lessons, narratives, per_mind=args.per_mind, vetoed=batch)
        kept = qualify(llm, mission, batch)
        actions = actions + batch
    ranked = kept[: args.top]

    run_id = memory.save_run(actions)
    write_decision_log(run_id)  # out/decisions-run<id>.json: abstentions, rejected drafts, questions
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
    p.add_argument("--retries", type=int, default=2, help="revision rounds when the critic vetoes every draft")
    p.set_defaults(func=run)

    # Outcomes: match approved actions to real posts and record their metrics (sam/outcomes).
    from harness.outcomes.cli import add_commands as add_outcomes_commands

    add_outcomes_commands(sub)

    p = sub.add_parser("record", help="store observed metrics: JSON list of {action_id, metrics}")
    p.add_argument("file")
    p.set_defaults(func=record)

    p = sub.add_parser("learn", help="turn reviews, edits and outcomes into tested lessons; rewrite memory/lessons.md")
    p.add_argument("--provider")
    p.set_defaults(func=lambda a: learn(get_llm(a.provider)))

    p = sub.add_parser("review", help="open the approve/kill dashboard; writes out/approved-run<id>.json")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=lambda a: serve(a.port, not a.no_browser))

    # Eval subcommands (sam/eval). Kept in their own block so merges stay simple.
    from harness.evals.cli import register as register_evals

    register_evals(sub)

    # Memory: ingest, reflect, revalidate, exemplar-check (sam/memory).
    from harness.learning.commands import add_commands

    add_commands(sub)

    # Self-iteration (sam/iterate): `python -m harness iterate ...`
    from harness.iterate.cli import register as register_iterate

    register_iterate(sub)

    args = parser.parse_args()
    args.func(args)
