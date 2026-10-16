import pytest

from harness.evals.fault import FaultEvidence, aggregate, attribute, outcome_ratio
from harness.evals.settings import FaultConfig


def ev(**kw):
    base = dict(action_id="a1", engagement=0, account_p10=2, account_p90=40, account_median=9, n_baseline_posts=20,
                story_peer_ratio=1.5, n_peer_posts=6, draft_score=0.8)
    return FaultEvidence(**{**base, **kw})


def test_outcome_ratio_handles_zero_median():
    assert outcome_ratio(0, 0) == 1.0
    assert outcome_ratio(4, 9) == 0.5


@pytest.mark.parametrize(
    "kw, verdict, stage",
    [
        (dict(engagement=41), "no_fault", None),
        (dict(engagement=2), "variance", None),  # the band is inclusive at p10
        (dict(engagement=40), "variance", None),  # and at p90
        (dict(story_peer_ratio=1.5, draft_score=0.2), "action_fault", "action"),
        (dict(story_peer_ratio=0.2, draft_score=0.8), "observe_fault", "observe"),
        (dict(story_peer_ratio=0.2, draft_score=0.2), "both", "observe"),
        (dict(story_peer_ratio=1.5, draft_score=0.8), "unexplained", "eval"),
        (dict(story_peer_ratio=0.7, draft_score=0.8), "uncertain", None),
        (dict(story_peer_ratio=1.5, draft_score=0.5), "uncertain", None),
    ],
)
def test_decision_table(kw, verdict, stage):
    v = attribute(ev(**kw))
    assert (v.verdict, v.stage_to_fix) == (verdict, stage)


def test_verdict_carries_the_evidence_it_used():
    v = attribute(ev(draft_score=0.2))
    assert v.evidence["engagement"] == 0 and v.evidence["story_peer_ratio"] == 1.5
    assert v.outcome_ratio == pytest.approx(0.1)


def test_critic_flag_marks_draft_bad_without_a_judge_score():
    v = attribute(ev(draft_score=None, critic_flags=["invented stat"]))
    assert v.verdict == "action_fault"
    assert any("invented stat" in r for r in v.reasons)


@pytest.mark.parametrize(
    "kw, missing",
    [
        (dict(engagement=None), ["engagement"]),
        (dict(account_p10=None, account_p90=None), ["account_p10", "account_p90"]),
        (dict(n_baseline_posts=5), ["n_baseline_posts"]),
        (dict(story_peer_ratio=None), ["story_peer_ratio"]),
        (dict(draft_score=None), ["draft_score"]),
    ],
)
def test_missing_evidence_is_named_not_guessed(kw, missing):
    v = attribute(ev(**kw))
    assert v.verdict == "insufficient_evidence"
    assert v.missing == missing


def test_too_few_peers_is_insufficient():
    v = attribute(ev(n_peer_posts=2))
    assert v.verdict == "insufficient_evidence"
    assert v.missing[0].startswith("story_peer_ratio")


def test_thresholds_come_from_config():
    strict = FaultConfig(min_baseline_posts=50)
    assert attribute(ev(), strict).verdict == "insufficient_evidence"


def test_aggregate_waits_for_repeats():
    verdicts = [attribute(ev(draft_score=0.2)) for _ in range(2)] + [attribute(ev(story_peer_ratio=0.2))]
    assert aggregate(verdicts)["fix"] == []
    verdicts.append(attribute(ev(draft_score=0.1)))
    summary = aggregate(verdicts)
    assert summary["fix"] == ["action"]
    assert summary["by_stage"] == {"action": 3, "observe": 1}
