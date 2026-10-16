import pytest

from harness.evals.fault import FaultEvidence, attribute, outcome_ratio


def ev(**kw):
    base = dict(action_id="a1", engagement=0, account_median=9, n_baseline_posts=20,
                story_peer_ratio=1.5, n_peer_posts=6, draft_score=0.8)
    return FaultEvidence(**{**base, **kw})


def test_outcome_ratio_handles_zero_median():
    assert outcome_ratio(0, 0) == 1.0
    assert outcome_ratio(4, 9) == 0.5


@pytest.mark.parametrize(
    "kw, verdict, stage",
    [
        (dict(engagement=9), "no_fault", None),
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


def test_critic_flag_marks_draft_bad_without_a_judge_score():
    v = attribute(ev(draft_score=None, critic_flags=["invented stat"]))
    assert v.verdict == "action_fault"
    assert any("invented stat" in r for r in v.reasons)


def test_underperformance_threshold_is_inclusive_of_half():
    assert attribute(ev(engagement=4)).verdict == "no_fault"  # (4+1)/(9+1) = 0.5
    assert attribute(ev(engagement=3)).verdict == "unexplained"


@pytest.mark.parametrize(
    "kw, missing",
    [
        (dict(engagement=None), ["engagement"]),
        (dict(account_median=None, n_baseline_posts=None), ["account_median", "n_baseline_posts"]),
        (dict(n_baseline_posts=2), ["n_baseline_posts"]),
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
