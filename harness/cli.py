import argparse
import json
import sys
from dataclasses import replace

from harness import config, memory
from harness.handoff import deliver
from harness.lanes import LANES, LaneContext
from harness.learn import learn
from harness.llm import get_llm
from harness.models import DECLINE_TYPES
from harness.review import serve
from harness.sources import observe


def run(args) -> None:
    llm = get_llm(args.provider)
    mission = config.mission()
    sources = args.sources.split(",") if args.sources else config.SOURCES
    lanes = list(LANES) if args.lane == "both" else [args.lane]
    if args.send and not args.email:
        raise SystemExit("--send only applies with --email")
    targets = config.TARGETS_PATH.name if config.TARGETS_PATH else "none"
    print(f"[run] mission: {config.mission_name()} | lanes: {', '.join(lanes)} | sources: {', '.join(sources)}"
          f" | targets: {targets} | db: {config.DB_PATH.name}")

    signals = observe(sources, mission)
    if not signals:
        raise SystemExit("No signals observed; check HARNESS_SOURCES.")
    run_id = memory.start_run(lanes, sources, args.provider)
    ctx = LaneContext(llm, mission, run_id, per_mind=args.per_mind)

    ranked = []
    for lane in lanes:
        limit = args.targets if lane == "reply" else args.narratives
        evaluated = LANES[lane](replace(ctx, limit=limit), signals)
        ranked += [a for a in evaluated if not a.vetoed and a.type not in DECLINE_TYPES][: args.top]
    ranked.sort(key=lambda a: a.predicted_score, reverse=True)
    memory.mark_ranked(run_id, ranked)

    # Hand-off contract: a JSON list of Action.to_dict(), best first, vetoed drafts removed.
    config.OUT_DIR.mkdir(exist_ok=True)
    out = config.OUT_DIR / f"recommendations-{config.run_tag(run_id)}.json"
    out.write_text(json.dumps([a.to_dict() for a in ranked], indent=2), encoding="utf-8")

    print(f"\n{len(ranked)} recommendations (run {run_id}) -> {out}\n")
    for i, a in enumerate(ranked, 1):
        target = a.target_url or (f"@{a.target_handle.lstrip('@')}" if a.target_handle else "")
        sent = "/".join(f"{a.sentiment.get(k, 0):.0%}" for k in ("positive", "neutral", "negative")) if a.sentiment else "-"
        print(f"{i:>2}. [{a.predicted_score:.2f} +/=/- {sent}] {a.lane:<5} {a.type:<7} {a.mind:<19} "
              f"{a.text.replace(chr(10), ' / ')}  {target}")

    if args.email:
        print()
        deliver(ranked, config.run_tag(run_id), config.mission_name(), config.OUT_DIR, send_mail=args.send, top=args.email_top)


def record(args) -> None:
    with open(args.file, encoding="utf-8") as f:
        n = memory.record_outcomes(json.load(f))
    print(f"[record] stored {n} outcomes")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; drafts often aren't
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="harness", description="Media harness for a brand account on X")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the reply and/or post lane, write out/recommendations-<run>.json")
    p.add_argument("--lane", choices=["reply", "post", "both"], default="both")
    p.add_argument("--provider", help="override HARNESS_PROVIDER (anthropic|xai|openai|mock)")
    p.add_argument("--sources", help="comma-separated source plugins, overrides HARNESS_SOURCES")
    p.add_argument("--targets", type=int, default=10, help="reply lane: posts considered as targets")
    p.add_argument("--narratives", type=int, default=5, help="post lane: narratives considered")
    p.add_argument("--per-mind", type=int, default=3)
    p.add_argument("--top", type=int, default=5, help="max recommendations kept per lane")
    p.add_argument("--email", action="store_true",
                   help="after writing recommendations, build the hand-off email (dry run: out/handoff-<run>.eml/.txt)")
    p.add_argument("--send", action="store_true",
                   help="with --email: actually send it via SMTP (needs SMTP_* and HANDOFF_EMAIL_TO); otherwise dry run")
    p.add_argument("--email-top", type=int, default=10, help="max actions in the hand-off email")
    p.set_defaults(func=run)

    p = sub.add_parser(
        "record",
        help='store actual metrics: JSON list of {action_id, metrics, post_id?, post_url?, status?}',
    )
    p.add_argument("file")
    p.set_defaults(func=record)

    p = sub.add_parser("learn", help="rewrite the lessons file from predicted vs. actual")
    p.add_argument("--provider")
    p.set_defaults(func=lambda a: learn(get_llm(a.provider)))

    p = sub.add_parser("review", help="open the approve/kill dashboard; writes out/approved-run<id>.json")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=lambda a: serve(a.port, not a.no_browser))

    args = parser.parse_args()
    args.func(args)
