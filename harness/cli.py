import argparse
import json
import sys
from dataclasses import replace

from harness import config, feedback, learn, memory
from harness.handoff import deliver
from harness.lanes import LANES, LaneContext
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
          f" | targets: {targets} | db: {config.DB_PATH.name} | evaluator: {'on' if config.EVALUATOR else 'off'}")

    signals = observe(sources, mission)
    if not signals:
        raise SystemExit("No signals observed; check HARNESS_SOURCES.")
    run_id = memory.start_run(lanes, sources, args.provider)
    ctx = LaneContext(llm, mission, run_id, per_mind=args.per_mind)

    # Rank by predicted_score x the learned action-type weight (learn.get_weights). predicted_score is the
    # evaluator's within-lane blend (0.5 panel percentile + 0.5 pairwise-tournament percentile), or the
    # panel's number with HARNESS_EVALUATOR=off. Follows skip the evaluator: the reply lane's follow ranker
    # scores them and they get up to HARNESS_FOLLOW_SLOTS reserved slots after the content actions.
    weights = {lane: learn.get_weights(lane)["type"] for lane in lanes}
    rank_score = {}
    content, follows, said = [], [], []
    for lane in lanes:
        limit = args.targets if lane == "reply" else args.narratives
        evaluated = LANES[lane](replace(ctx, limit=limit, said=tuple(said)), signals)
        keep = [a for a in evaluated if not a.vetoed and a.type not in DECLINE_TYPES]
        said += [a.text for a in keep if a.text]  # later lanes treat these as already said
        for a in keep:
            rank_score[a.id] = a.predicted_score * weights[lane].get(a.type, 1)
        follows += [a for a in keep if a.type == "follow"]
        lane_content = sorted((a for a in keep if a.type != "follow"), key=lambda a: rank_score[a.id], reverse=True)
        content += lane_content[: args.top]
    content.sort(key=lambda a: rank_score[a.id], reverse=True)
    follows = sorted(follows, key=lambda a: rank_score[a.id], reverse=True)[: config.FOLLOW_SLOTS]
    ranked = content + follows
    memory.mark_ranked(run_id, ranked)

    # Hand-off contract: a JSON list of Action.to_dict(), best first, vetoed drafts removed.
    config.OUT_DIR.mkdir(exist_ok=True)
    out = config.OUT_DIR / f"recommendations-{config.run_tag(run_id)}.json"
    out.write_text(json.dumps([a.to_dict() for a in ranked], indent=2), encoding="utf-8")

    print(f"\n{len(ranked)} recommendations (run {run_id}) -> {out}\n")
    for i, a in enumerate(ranked, 1):
        target = a.target_url or (f"@{a.target_handle.lstrip('@')}" if a.target_handle else "")
        sent = "/".join(f"{a.sentiment.get(k, 0):.0%}" for k in ("positive", "neutral", "negative")) if a.sentiment else "-"
        if a.context.get("slot") == "follow":
            sent = "follow slot"
        w = f" x{rank_score[a.id] / a.predicted_score:.2f}" if a.predicted_score and abs(rank_score[a.id] - a.predicted_score) > 1e-9 else ""
        flags = (a.context.get("eval") or {}).get("flags")
        print(f"{i:>2}. [{a.predicted_score:.2f}{w} +/=/- {sent}] {a.lane:<5} {a.type:<7} {a.mind:<19} "
              f"{a.text.replace(chr(10), ' / ')}  {target}" + (f"  [flags: {', '.join(flags)}]" if flags else ""))

    if args.email:
        print()
        deliver(ranked, config.run_tag(run_id), config.mission_name(), config.OUT_DIR, send_mail=args.send, top=args.email_top)


def record(args) -> None:
    with open(args.file, encoding="utf-8") as f:
        n = memory.record_outcomes(json.load(f))
    print(f"[record] stored {n} outcomes")


def ingest_reply(args) -> None:
    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    recs = feedback.ingest_bot_reply(text)
    for r in recs:
        mark = "ok " if r.get("applied") else "-- "
        extra = r.get("post_url") or r.get("post_id") or r.get("reason") or ""
        print(f"  {mark}{r['action_id']:<10} {r['outcome']:<8} {extra}")


def ingest_metrics(args) -> None:
    out = feedback.ingest_metrics(args.source, handle=args.handle)
    if out.get("error"):
        raise SystemExit(1)


def calibrate(args) -> None:
    rows = feedback.outcomes()
    if not rows:
        print("[calibrate] no recommended, posted or rejected actions in memory yet")
        return
    print(feedback.format_calibration(feedback.calibration(rows)))


def run_learn(args) -> None:
    out = learn.learn(get_llm(args.provider))
    if out:
        print()
        print(out["lessons_md"])


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

    p = sub.add_parser("ingest-reply", help="apply the posting bot's reply email (ACTION_ID <id> | <url>|followed|skipped | why)")
    p.add_argument("file", help="the reply as a .txt/.eml body, or - for stdin")
    p.set_defaults(func=ingest_reply)

    p = sub.add_parser("ingest-metrics", help="fill actual metrics for posted actions from X or a scraper file")
    p.add_argument("--from", dest="source", default="x", help="x (X API, needs X_BEARER_TOKEN) or a .json/.csv file")
    p.add_argument("--handle", help="with --from x: the brand's X handle, to match posts the bot didn't link")
    p.set_defaults(func=ingest_metrics)

    p = sub.add_parser("calibrate", help="predicted vs. actual by lane, mind, type, relationship and panelist")
    p.set_defaults(func=calibrate)

    p = sub.add_parser("learn", help="lessons, weights and mission proposals from predicted vs. actual + bot skips")
    p.add_argument("--provider")
    p.set_defaults(func=run_learn)

    p = sub.add_parser("review", help="open the approve/kill dashboard; writes out/approved-run<id>.json")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=lambda a: serve(a.port, not a.no_browser))

    args = parser.parse_args()
    args.func(args)
