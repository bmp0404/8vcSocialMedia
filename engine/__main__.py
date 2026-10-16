"""Run with python -m engine --help."""

import argparse
from pathlib import Path

from harness import config
from harness.llm import get_llm
from harness.sources import REGISTRY

from .evaluation import panel_evaluate, reference_evaluate
from .runner import Components, Options, run_loop
from .runtime import runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded observe/action/eval/memory rounds")
    parser.add_argument("--provider", default=config.PROVIDER)
    parser.add_argument("--sources", help="Comma-separated registered source names")
    parser.add_argument("--mission", type=Path, default=config.MISSION_PATH)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0)
    parser.add_argument("--narratives", type=int, default=2)
    parser.add_argument("--per-mind", type=int, default=1)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--eval", choices=("panel", "reference"), dest="evaluator")
    parser.add_argument("--state-dir", type=Path, help="Isolate DB, lessons, output, and memory store here")
    args = parser.parse_args()
    provider = args.provider.lower()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else (["sample"] if provider == "mock" else config.SOURCES)
    unknown = set(sources) - set(REGISTRY)
    if unknown:
        parser.error(f"Unknown sources: {', '.join(sorted(unknown))}")
    evaluator = args.evaluator or ("panel" if provider == "mock" else "reference")
    if provider == "mock" and evaluator == "reference":
        parser.error("mock mode uses the offline panel; use a real provider for reference evaluation")
    if provider == "mock" and sources != ["sample"]:
        parser.error("mock mode uses only the sample source to stay offline")
    try:
        options = Options(args.rounds, args.interval, args.narratives, args.per_mind, args.top, args.retries)
    except ValueError as exc:
        parser.error(str(exc))
    mission = args.mission.read_text(encoding="utf-8")
    with runtime(provider, sources, args.state_dir) as output_dir:
        print(f"[engine] output: {output_dir}")
        components = Components(evaluate=panel_evaluate if evaluator == "panel" else reference_evaluate)
        run_loop(get_llm(provider), mission, sources, options, components, output_dir)


if __name__ == "__main__":
    main()
