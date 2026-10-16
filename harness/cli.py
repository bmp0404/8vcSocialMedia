import argparse
import json
from datetime import datetime

from harness import config, memory
from harness.learn import learn
from harness.llm import get_llm
from harness.memstore import record_round
from harness.minds import decide, write_decision_log
from harness.orient import orient
from harness.qualify import qualify, ranker_choice, ready as qualify_ready
from harness.review import serve
from harness.sources import observe
from harness.sources.watch import account_handle


def run(args) -> None:
    llm = get_llm(args.provider)
    mission = config.mission()
    sources = args.sources.split(",") if args.sources else config.SOURCES
    # Read before any LLM call, so a broken observe config fails the run before it spends anything.
    account, account_source = account_handle()
    print(f"[run] account @{account} (from {account_source})" if account else f"[run] no account: {account_source} is empty")
    ranker = ranker_choice()
    print(f"[run] qualify orders the post lane by {qualify_ready(ranker)}")  # loads and checks it before any spend

    signals = observe(sources, mission)
    if not signals:
        raise SystemExit("No signals observed; check HARNESS_SOURCES.")
    narratives = orient(llm, mission, signals, top=args.narratives)
    print(f"[orient] {len(narratives)} narratives: " + ", ".join(n.id for n in narratives))
    lessons = config.lessons()
    batch = decide(llm, mission, lessons, narratives, per_mind=args.per_mind, signals=signals)
    actions, kept = batch, qualify(llm, mission, batch, ranker=ranker)
    for _ in range(args.retries):
        if kept or not batch:
            break
        # The critic vetoed every draft. Only persona drafts are revised against the critiques, and persona
        # text no longer ships, so decide() returns [] and the round ends with no recommendations. That is
        # deliberate: rerunning the caption on the same source shipped two captions of one story
        # (Integration run 36; action/DESIGN.md, "Rushi"). The vetoed drafts and critiques stay in the run.
        batch = decide(llm, mission, lessons, narratives, per_mind=args.per_mind, vetoed=batch)
        kept = qualify(llm, mission, batch, ranker=ranker)
        actions = actions + batch
    ranked = kept[: args.top]
    if actions and not kept:
        print(f"[run] the critic vetoed all {len(actions)} drafts, and captions and replies aren't redrafted, "
              "so this round recommends nothing; the critiques are saved with the run")

    # Saved in the round's order, the order the review queue and the hand-off show: what qualify kept, in qualify's
    # order, then what the critic vetoed.
    kept_ids = {a.id for a in kept}
    run_id = memory.save_run(kept + [a for a in actions if a.id not in kept_ids], account=account or None)
    write_decision_log(run_id)  # out/decisions-run<id>.json: abstentions, rejected drafts, questions
    record_round(run_id)  # one episode note per draft in the profile's memory store
    config.OUT_DIR.mkdir(exist_ok=True)
    out = config.OUT_DIR / f"recommendations-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(
        json.dumps({"run_id": run_id, "account": account or None, "recommendations": [a.to_dict() for a in ranked]},
                   indent=2),
        encoding="utf-8",
    )

    print(f"\nTop {len(ranked)} recommendations (run {run_id}) -> {out}\n")
    for i, a in enumerate(ranked, 1):
        target = a.target_url or (f"@{a.target_handle}" if a.target_handle else "")
        # qualify scores only the post lane's posts and quotes, unless the panel ranks (harness/qualify.py).
        score = " -- " if a.predicted_score is None else f"{a.predicted_score:.2f}"
        print(f"{i:>2}. [{score}] {a.type:<7} {a.mind:<18} {a.text}  {target}")


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
    p.add_argument("--retries", type=int, default=2, help="revision rounds when the critic vetoes every draft; only persona drafts are revised, and they don't "
                        "ship, so today a full veto ends the round (action/DESIGN.md)")
    p.set_defaults(func=run)

    # Outcomes: match approved actions to real posts and record their metrics (sam/outcomes).
    from harness.outcomes.cli import add_commands as add_outcomes_commands

    add_outcomes_commands(sub)

    p = sub.add_parser("record", help="store observed metrics: JSON list of {action_id, metrics}")
    p.add_argument("file")
    p.set_defaults(func=record)

    p = sub.add_parser("learn", help="turn reviews, edits and outcomes into tested lessons; rewrite the profile's lessons file")
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

    # Watcher (sam/watch): `python -m harness watch`, plus `watch status` and `watch stop`.
    from harness.watcher.cli import add_commands as add_watch_commands

    add_watch_commands(sub)

    # Improvement loop (sam/improve): `python -m harness improve`, plus `improve status` and `improve stop`.
    from harness.improve.cli import add_commands as add_improve_commands

    add_improve_commands(sub)

    args = parser.parse_args()
    args.func(args)
