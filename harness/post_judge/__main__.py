"""Run with python -m harness.post_judge --help."""

import argparse
import json
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from baml_sdk import CandidatePost
from .judge import DEFAULT_DATASET, grade_post
from .references import load_references, select_references


def main() -> None:
    parser = argparse.ArgumentParser(description="Grade a model's post against historical examples")
    parser.add_argument("candidate", type=Path, help="JSON with text, mission, context, lane")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--per-class", type=int, default=3)
    parser.add_argument("--before", type=date.fromisoformat, help="Only use references strictly before YYYY-MM-DD")
    parser.add_argument("--exclude-id", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true", help="Print selected references without calling the model")
    parser.add_argument("--output", type=Path, help="Write JSON to this file instead of stdout")
    args = parser.parse_args()
    load_dotenv()
    candidate = CandidatePost.model_validate_json(args.candidate.read_text(encoding="utf-8"))
    options = dict(per_class=args.per_class, before=args.before, exclude_ids=set(args.exclude_id))
    if args.dry_run:
        refs = select_references(load_references(args.dataset), candidate.text, **options)
        result = {"dry_run": True, "candidate": candidate.model_dump(mode="json"),
                  "references": [r.model_dump(mode="json") for r in refs]}
    else:
        result = grade_post(candidate, args.dataset, **options).model_dump(mode="json")
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
