import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pydantic

from baml_sdk import (
    CandidatePost, Comparison, Confidence, Criterion, GradePost_spec, Performance, PostGrade, ReferencePost, Verdict,
)
from harness.post_judge.judge import grade_with_references, validate_grade
from harness.post_judge.references import load_references, select_references


def reference(index, performance=Performance.Successful, **kwargs):
    fields = dict(id=f"https://x.com/post/{index}", text=f"Release feature {index}", account=f"@account{index}",
                  performance=performance, category="Super Viral", engagement=index,
                  media="none", date="2026-09-15")
    return ReferencePost(**(fields | kwargs))


def grade(refs, score=3):
    return PostGrade(
        verdict=Verdict.Revise, confidence=Confidence.Low,
        **{key: Criterion(score=score, reason="Fixture rationale") for key in
           ("relevance", "clarity", "specificity", "engagement_potential", "grounding")},
        comparisons=[Comparison(reference_id=p.id, observation="Fixture comparison") for p in refs],
        strengths=[], improvements=["Add context"], limitations=["Offline fixture"],
    )


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.posts = [reference(i, label) for i, label in enumerate(
            [Performance.Successful] * 4 + [Performance.Poor] * 4, 1)]

    def test_balanced_deterministic_and_diverse(self):
        chosen = select_references(self.posts, "New feature", per_class=2)
        self.assertEqual(chosen, select_references(list(reversed(self.posts)), "New feature", per_class=2))
        self.assertEqual([p.performance for p in chosen], [Performance.Successful, Performance.Poor] * 2)
        self.assertEqual(len({p.account for p in chosen}), 4)

    def test_holdout_cutoff_and_candidate_excluded(self):
        self.posts[1].date = "2026-10-01"
        chosen = select_references(self.posts, "  RELEASE feature 1 ", per_class=1,
                                   before=date(2026, 10, 1), exclude_ids={self.posts[2].id})
        self.assertEqual(chosen[0].id, self.posts[3].id)

    def test_requires_both_classes(self):
        with self.assertRaisesRegex(ValueError, "Poor references"):
            select_references(self.posts[:4], "new post", per_class=1)

    def test_distinct_accounts(self):
        for p in self.posts[:4]:
            p.account = "@same"
        with self.assertRaisesRegex(ValueError, "distinct-account Successful"):
            select_references(self.posts, "new post", per_class=2)

    def test_invalid_size_and_empty_text(self):
        for size in (0, 11):
            with self.assertRaises(ValueError):
                select_references(self.posts, "new", per_class=size)
        with self.assertRaises(ValueError):
            select_references(self.posts, " ")

    def test_loader_validates_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "posts.csv"
            fields = ["Category", "Account", "Post Content", "Engagement", "Post URL", "Date", "Media"]
            row = ["Deadweight", "@a", "Same post", "0", "url1", "2026-09-15", "none"]
            with path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(fields)
                writer.writerow(row)
                writer.writerow(row[:4] + ["url2"] + row[5:])
            loaded = load_references(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].performance, Performance.Poor)
            path.write_text("wrong,column\na,b\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing columns"):
                load_references(path)


class GradeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"HARNESS_JUDGE_PROVIDER": "openai", "HARNESS_JUDGE_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)
        self.refs = [reference(1), reference(2, Performance.Poor)]
        self.candidate = CandidatePost(text="New tool", mission="Help developers", context="Release notes", lane="post")

    def test_score_derived_from_rubric(self):
        self.assertEqual(validate_grade(grade(self.refs, 3), self.refs), 60)
        self.assertEqual(validate_grade(grade(self.refs, 5), self.refs), 100)

    def test_invalid_scores_and_invented_citations_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            Criterion(score=6, reason="Out of range")
        out_of_range = grade(self.refs)
        out_of_range.clarity = Criterion.model_construct(score=6, reason="Bypassed validation")
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_grade(out_of_range, self.refs)
        invalid = grade(self.refs)
        invalid.comparisons[0].reference_id = "invented"
        with self.assertRaisesRegex(ValueError, "unknown reference"):
            validate_grade(invalid, self.refs)

    def test_requires_comparisons_from_both_classes(self):
        invalid = grade(self.refs[:1])
        with self.assertRaisesRegex(ValueError, "both successful and poor"):
            validate_grade(invalid, self.refs)

    def test_explicit_pool_returns_provenance(self):
        with patch("harness.post_judge.judge.GradePost", return_value=grade(self.refs)) as judge:
            result = grade_with_references(self.candidate, self.refs, dataset_sha256="train-only-hash")
        self.assertEqual(result.score_100, 60)
        self.assertEqual(result.dataset_sha256, "train-only-hash")
        judge.assert_called_once_with(self.candidate, self.refs, provider="openai", model="gpt-4.1-mini")
        self.assertEqual(result.references, self.refs)

    def test_invalid_grade_is_retried_once_without_changing_evidence(self):
        invalid = grade(self.refs[:1])
        with patch("harness.post_judge.judge.GradePost", side_effect=[invalid, grade(self.refs)]) as judge:
            result = grade_with_references(self.candidate, self.refs, dataset_sha256="hash")
        self.assertEqual(result.score_100, 60)
        self.assertEqual(judge.call_count, 2)
        self.assertEqual(judge.call_args_list[0], judge.call_args_list[1])

    def test_repeated_invalid_grade_still_fails(self):
        with patch("harness.post_judge.judge.GradePost", return_value=grade(self.refs[:1])) as judge:
            with self.assertRaisesRegex(ValueError, "both successful and poor"):
                grade_with_references(self.candidate, self.refs, dataset_sha256="hash")
        self.assertEqual(judge.call_count, 2)

    def test_leaked_candidate_rejected_before_model_call(self):
        self.candidate.text = " release FEATURE 1 "
        with patch("harness.post_judge.judge.GradePost") as judge:
            with self.assertRaisesRegex(ValueError, "Candidate text"):
                grade_with_references(self.candidate, self.refs, dataset_sha256="hash")
            judge.assert_not_called()

    def test_baml_parses_structured_grade_without_network(self):
        spec = GradePost_spec(self.candidate, self.refs)
        parsed = spec.parse(grade(self.refs).model_dump_json())
        self.assertEqual(validate_grade(parsed, self.refs), 60)

    def test_anthropic_gateway_request_without_network(self):
        with patch.dict("os.environ", {"ANTHROPIC_AUTH_TOKEN": "fixture-token", "ANTHROPIC_BASE_URL": "https://example.com"}):
            spec = GradePost_spec(self.candidate, self.refs, provider="anthropic", model="fixture-model")
            request = spec.build_request()
        self.assertEqual(request.url, "https://example.com/v1/messages")
        self.assertEqual(request.headers["authorization"], "Bearer fixture-token")

    def test_claude_code_is_default(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("harness.post_judge.judge.GradePost", return_value=grade(self.refs)) as judge:
                result = grade_with_references(self.candidate, self.refs, dataset_sha256="hash")
        judge.assert_called_once_with(self.candidate, self.refs, provider="claude_code", model="sonnet")
        self.assertEqual(result.judge_provider, "claude_code")
        self.assertEqual(result.judge_model, "sonnet")

    def test_invalid_provider_fails_before_call(self):
        with patch.dict("os.environ", {"HARNESS_JUDGE_PROVIDER": "unknown"}):
            with patch("harness.post_judge.judge.GradePost") as judge:
                with self.assertRaisesRegex(ValueError, "must be claude_code, openai, or anthropic"):
                    grade_with_references(self.candidate, self.refs, dataset_sha256="hash")
                judge.assert_not_called()

    def test_model_failure_propagates(self):
        with patch("harness.post_judge.judge.GradePost", side_effect=RuntimeError("provider unavailable")):
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                grade_with_references(self.candidate, self.refs, dataset_sha256="hash")


if __name__ == "__main__":
    unittest.main()
