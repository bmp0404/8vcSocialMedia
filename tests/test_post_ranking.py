import unittest
from unittest.mock import patch

from baml_sdk import (
    CandidatePost, CandidateRanking, Confidence, Performance, RankedCandidate,
    RankingCandidate, ReferencePost, Verdict, RankCandidates_spec,
)
from harness.post_judge.ranking import rank_candidates, validate_ranking


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [RankingCandidate(id=f"draft-{i}", post=CandidatePost(
            text=f"A distinct draft {i}", mission="Help developers", context="Release notes", lane="post"))
            for i in range(2)]
        self.refs = [ReferencePost(id=f"ref-{i}", text=f"Historical post {i}", account=f"@a{i}",
                                  performance=label, category=label.value, engagement=i,
                                  media="none", date="2026-09-01")
                     for i, label in enumerate(Performance)]
        self.result = CandidateRanking(ranked=[RankedCandidate(
            candidate_id=c.id, score=80-i*10, verdict=Verdict.Revise, confidence=Confidence.Low,
            reason="Fixture comparison", reference_ids=[r.id for r in self.refs])
            for i, c in enumerate(self.candidates)], limitations=["Offline fixture"])

    def test_one_call_returns_full_provenance(self):
        with patch("harness.post_judge.ranking.RankCandidates", return_value=self.result) as model:
            evaluation = rank_candidates(self.candidates, self.refs, dataset_sha256="train-hash")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(evaluation.dataset_sha256, "train-hash")
        self.assertEqual(evaluation.candidates, self.candidates)
        self.assertEqual(evaluation.ranking.ranked[0].candidate_id, "draft-0")

    def test_missing_duplicate_and_unknown_candidates(self):
        for ids in (["draft-0"], ["draft-0", "draft-0"], ["draft-0", "unknown"]):
            result = self.result.model_copy(deep=True)
            result.ranked = [self.result.ranked[0].model_copy(update={"candidate_id": i}) for i in ids]
            with self.assertRaisesRegex(ValueError, "exactly once"):
                validate_ranking(result, self.candidates, self.refs)

    def test_invalid_score_and_order(self):
        for scores in ([101, 70], [80, -1], [70, 80]):
            result = self.result.model_copy(deep=True)
            for item, score in zip(result.ranked, scores):
                item.score = score
            with self.assertRaisesRegex(ValueError, "descending order"):
                validate_ranking(result, self.candidates, self.refs)

    def test_ties_are_valid(self):
        self.result.ranked[1].score = 80
        validate_ranking(self.result, self.candidates, self.refs)

    def test_citations_require_known_ids_and_both_classes(self):
        for ids in (["invented"], ["ref-0"]):
            self.result.ranked[0].reference_ids = ids
            with self.assertRaises(ValueError):
                validate_ranking(self.result, self.candidates, self.refs)

    def test_mixed_context_rejected_before_model(self):
        self.candidates[1].post.context = "Different target"
        with patch("harness.post_judge.ranking.RankCandidates") as model:
            with self.assertRaisesRegex(ValueError, "same mission"):
                rank_candidates(self.candidates, self.refs, dataset_sha256="hash")
            model.assert_not_called()

    def test_candidate_leakage_rejected(self):
        self.refs[0].text = self.candidates[0].post.text.upper()
        with self.assertRaisesRegex(ValueError, "reference pool"):
            rank_candidates(self.candidates, self.refs, dataset_sha256="hash")

    def test_bounds_and_duplicate_drafts(self):
        with self.assertRaisesRegex(ValueError, "2 to 6"):
            rank_candidates(self.candidates[:1], self.refs, dataset_sha256="hash")
        self.candidates[1].post.text = self.candidates[0].post.text
        with self.assertRaisesRegex(ValueError, "distinct"):
            rank_candidates(self.candidates, self.refs, dataset_sha256="hash")

    def test_baml_ranking_schema_parses(self):
        spec = RankCandidates_spec(self.candidates, self.refs)
        parsed = spec.parse(self.result.model_dump_json())
        validate_ranking(parsed, self.candidates, self.refs)
        self.assertEqual(parsed.ranked[0].candidate_id, "draft-0")

    def test_provider_failure_is_not_a_ranking(self):
        with patch("harness.post_judge.ranking.RankCandidates", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                rank_candidates(self.candidates, self.refs, dataset_sha256="hash")


if __name__ == "__main__":
    unittest.main()
