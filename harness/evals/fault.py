"""Fault attribution: when an action underperforms, decide which stage to fix.

The team's rule (CONTRIBUTING.md): a bad draft from good inputs is an action fault, and a sensible
draft about a dead story is an observe fault. This module turns that rule into a decision table over
three pieces of evidence, each measured against something real where possible:

- outcome: our engagement relative to the account's own median. Did it underperform?
- story: how other accounts' posts on the same narrative did, each relative to its author's median.
  Was the input alive? For a reply, the peers are the other replies to the same target post.
- draft: a quality score for our text (the post judge's score_100 / 100) plus any critic flags.
  Was the draft sensible?

A verdict names the stage to fix and cites the evidence it used. When evidence a verdict needs is
missing, the verdict is `insufficient_evidence` with the missing fields listed, never a guess.
"""

from dataclasses import asdict, dataclass, field

# Hand-set and uncalibrated: no run has tested these cut-offs. The backtest's judge scores on the
# dev-tool holdout are the first evidence that could set DRAFT_* (see eval/DESIGN.md).
UNDERPERFORM_RATIO = 0.5  # below half the account's usual engagement
STORY_ALIVE_RATIO = 1.0  # peers on the story did at least their usual
STORY_DEAD_RATIO = 0.5  # peers on the story did under half their usual
DRAFT_GOOD = 0.6
DRAFT_BAD = 0.4
MIN_BASELINE_POSTS = 5
MIN_PEER_POSTS = 3

VERDICTS = {
    "no_fault": None,
    "action_fault": "action",
    "observe_fault": "observe",
    "both": "observe",  # a better draft on a dead story still flops, so fix the input first
    "unexplained": "eval",  # both stages look fine and it still flopped: our quality measures missed something
    "uncertain": None,
    "insufficient_evidence": None,
}


@dataclass
class FaultEvidence:
    action_id: str
    engagement: float | None = None  # ours, at the end of the measurement window
    account_median: float | None = None  # the account's median for the same action type
    n_baseline_posts: int | None = None
    story_peer_ratio: float | None = None  # median of peers' engagement / their own author median
    n_peer_posts: int | None = None
    draft_score: float | None = None  # 0-1, from the post judge graded after the fact
    critic_flags: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class FaultVerdict:
    action_id: str
    verdict: str
    stage_to_fix: str | None
    reasons: list[str]
    missing: list[str] = field(default_factory=list)
    outcome_ratio: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def outcome_ratio(engagement: float, account_median: float) -> float:
    """(engagement + 1) / (median + 1): defined for a zero median, and 1.0 means a typical post."""
    return (engagement + 1) / (account_median + 1)


def _verdict(e: FaultEvidence, verdict: str, reasons: list[str], missing=(), ratio=None) -> FaultVerdict:
    return FaultVerdict(e.action_id, verdict, VERDICTS[verdict], reasons, list(missing), ratio)


def attribute(e: FaultEvidence) -> FaultVerdict:
    missing = [name for name, value in (("engagement", e.engagement), ("account_median", e.account_median),
                                        ("n_baseline_posts", e.n_baseline_posts)) if value is None]
    if missing:
        return _verdict(e, "insufficient_evidence", ["can't tell whether the action underperformed"], missing)
    if e.n_baseline_posts < MIN_BASELINE_POSTS:
        return _verdict(e, "insufficient_evidence",
                        [f"account median rests on {e.n_baseline_posts} posts; need {MIN_BASELINE_POSTS}"],
                        ["n_baseline_posts"])

    ratio = outcome_ratio(e.engagement, e.account_median)
    if ratio >= UNDERPERFORM_RATIO:
        return _verdict(e, "no_fault", [f"outcome ratio {ratio:.2f} >= {UNDERPERFORM_RATIO}"], ratio=ratio)
    reasons = [f"underperformed: outcome ratio {ratio:.2f} < {UNDERPERFORM_RATIO}"]

    missing = []
    if e.story_peer_ratio is None or e.n_peer_posts is None:
        missing.append("story_peer_ratio")
    elif e.n_peer_posts < MIN_PEER_POSTS:
        missing.append(f"story_peer_ratio (only {e.n_peer_posts} peer posts; need {MIN_PEER_POSTS})")
    if e.draft_score is None and not e.critic_flags:
        missing.append("draft_score")
    if missing:
        return _verdict(e, "insufficient_evidence", reasons + ["need both story and draft evidence to pick a stage"],
                        missing, ratio)

    if e.story_peer_ratio >= STORY_ALIVE_RATIO:
        story = "alive"
    elif e.story_peer_ratio < STORY_DEAD_RATIO:
        story = "dead"
    else:
        story = "borderline"
    reasons.append(f"story {story}: peers did {e.story_peer_ratio:.2f}x their usual over {e.n_peer_posts} posts")

    if e.critic_flags:
        draft = "bad"
        reasons.append(f"draft bad: critic flagged {', '.join(e.critic_flags)}")
    elif e.draft_score >= DRAFT_GOOD:
        draft = "good"
    elif e.draft_score < DRAFT_BAD:
        draft = "bad"
    else:
        draft = "borderline"
    if not e.critic_flags:
        reasons.append(f"draft {draft}: judge score {e.draft_score:.2f}")

    table = {
        ("alive", "bad"): "action_fault",
        ("dead", "good"): "observe_fault",
        ("dead", "bad"): "both",
        ("alive", "good"): "unexplained",
    }
    return _verdict(e, table.get((story, draft), "uncertain"), reasons, ratio=ratio)
