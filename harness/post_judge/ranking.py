"""Compare alternatives for one target in a single model request."""

from pydantic import BaseModel

from baml_sdk import CandidateRanking, Performance, RankCandidates, RankingCandidate, ReferencePost
from .judge import judge_settings
from .references import normalized_text


class RankingEvaluation(BaseModel):
    schema_version: str = "1"
    rubric_version: str = "ranking-1"
    judge_provider: str
    judge_model: str
    dataset_sha256: str
    candidates: list[RankingCandidate]
    references: list[ReferencePost]
    ranking: CandidateRanking


def validate_ranking(result: CandidateRanking, candidates: list[RankingCandidate],
                     references: list[ReferencePost]) -> None:
    ids = [item.candidate_id for item in result.ranked]
    if len(ids) != len(candidates) or set(ids) != {c.id for c in candidates}:
        raise ValueError("Ranking must contain each candidate ID exactly once")
    scores = [item.score for item in result.ranked]
    if any(not 0 <= score <= 100 for score in scores) or scores != sorted(scores, reverse=True):
        raise ValueError("Ranking scores must be in 0..100 and descending order")
    by_id = {post.id: post for post in references}
    for item in result.ranked:
        if any(ref_id not in by_id for ref_id in item.reference_ids):
            raise ValueError("Ranking cited an unknown reference")
        if {by_id[ref_id].performance for ref_id in item.reference_ids} != set(Performance):
            raise ValueError("Each ranked candidate must cite both reference classes")


def rank_candidates(candidates: list[RankingCandidate], references: list[ReferencePost], *,
                    dataset_sha256: str) -> RankingEvaluation:
    """Rank 2–6 alternatives sharing a mission, lane and observation context."""
    if not 2 <= len(candidates) <= 6:
        raise ValueError("Provide 2 to 6 candidates")
    if any(not c.id.strip() for c in candidates) or len({c.id for c in candidates}) != len(candidates):
        raise ValueError("Candidate IDs must be nonempty and unique")
    texts = {normalized_text(c.post.text) for c in candidates}
    if "" in texts or len(texts) != len(candidates):
        raise ValueError("Candidate texts must be nonempty and distinct")
    contexts = {(c.post.mission, c.post.context, c.post.lane) for c in candidates}
    if len(contexts) != 1:
        raise ValueError("Candidates must share the same mission, context and lane")
    first = candidates[0].post
    if not first.mission.strip() or first.lane not in {"post", "reply", "quote"}:
        raise ValueError("Candidates need a mission and a post/reply/quote lane")
    if {r.performance for r in references} != set(Performance):
        raise ValueError("References must include both performance classes")
    if len({r.id for r in references}) != len(references):
        raise ValueError("Reference IDs must be unique")
    if any(normalized_text(r.text) in texts for r in references):
        raise ValueError("Candidate text must not appear in the reference pool")
    provider, model = judge_settings()
    ranking = RankCandidates(candidates, references, provider=provider, model=model)
    validate_ranking(ranking, candidates, references)
    return RankingEvaluation(judge_provider=provider, judge_model=model,
                             dataset_sha256=dataset_sha256, candidates=candidates,
                             references=references, ranking=ranking)
