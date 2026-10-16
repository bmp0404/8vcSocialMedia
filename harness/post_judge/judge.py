"""Grade posts with BAML and validate evidence before returning scores."""

import hashlib
import os
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from baml_sdk import CandidatePost, GradePost, PostGrade, Performance, ReferencePost
from .references import load_references, normalized_text, select_references

DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "data/devtools_virality_30d.csv"
CRITERIA = ("relevance", "clarity", "specificity", "engagement_potential", "grounding")


class Evaluation(BaseModel):
    schema_version: str = "1"
    rubric_version: str = "1"
    judge_provider: str
    judge_model: str
    dataset_sha256: str
    candidate: CandidatePost
    references: list[ReferencePost]
    grade: PostGrade
    score_100: int


def validate_grade(grade: PostGrade, references: list[ReferencePost]) -> int:
    """Reject invalid scores and invented citations rather than silently repairing them."""
    scores = [getattr(grade, name).score for name in CRITERIA]
    if any(not 0 <= score <= 5 for score in scores):
        raise ValueError("Judge returned a rubric score outside 0..5")
    by_id = {p.id: p for p in references}
    cited_labels = set()
    for comparison in grade.comparisons:
        if comparison.reference_id not in by_id:
            raise ValueError(f"Judge cited an unknown reference: {comparison.reference_id}")
        cited_labels.add(by_id[comparison.reference_id].performance)
    if cited_labels != set(Performance):
        raise ValueError("Judge must compare against both successful and poor references")
    return sum(scores) * 4


def grade_post(candidate: CandidatePost, dataset: Path = DEFAULT_DATASET, *,
               per_class: int = 3, before: date | None = None,
               exclude_ids: set[str] | None = None) -> Evaluation:
    if candidate.lane not in {"post", "reply", "quote"}:
        raise ValueError("lane must be post, reply, or quote")
    if not candidate.mission.strip():
        raise ValueError("mission must not be empty")
    references = select_references(load_references(dataset), candidate.text,
                                   per_class=per_class, before=before, exclude_ids=exclude_ids)
    return grade_with_references(candidate, references,
                                 dataset_sha256=hashlib.sha256(dataset.read_bytes()).hexdigest())


def grade_with_references(candidate: CandidatePost, references: list[ReferencePost], *,
                          dataset_sha256: str) -> Evaluation:
    """Accept a train-only reference pool selected by the backtest caller."""
    if candidate.lane not in {"post", "reply", "quote"} or not candidate.mission.strip() or not candidate.text.strip():
        raise ValueError("Candidate needs text, mission, and a post/reply/quote lane")
    if {p.performance for p in references} != set(Performance):
        raise ValueError("References must contain both performance classes")
    if len({p.id for p in references}) != len(references):
        raise ValueError("Reference IDs must be unique")
    if any(normalized_text(p.text) == normalized_text(candidate.text) for p in references):
        raise ValueError("Candidate text must not appear in the reference pool")
    provider = os.getenv("HARNESS_JUDGE_PROVIDER", "claude_code").strip().lower()
    if provider not in {"claude_code", "openai", "anthropic"}:
        raise ValueError("HARNESS_JUDGE_PROVIDER must be claude_code, openai, or anthropic")
    model = os.getenv("HARNESS_JUDGE_MODEL", "").strip()
    if not model:
        if provider == "anthropic":
            raise ValueError("Set HARNESS_JUDGE_MODEL to your Anthropic or gateway model ID")
        model = "sonnet" if provider == "claude_code" else "gpt-4.1-mini"
    grade = GradePost(candidate, references, provider=provider, model=model)
    score = validate_grade(grade, references)
    return Evaluation(judge_provider=provider, judge_model=model,
                      dataset_sha256=dataset_sha256, candidate=candidate,
                      references=references, grade=grade, score_100=score)
