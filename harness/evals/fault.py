"""Fault attribution: when an action underperforms, decide which stage to fix.

The team's rule (CONTRIBUTING.md): a bad draft from good inputs is an action fault, and a sensible
draft about a dead story is an observe fault. This module turns that rule into a decision table over
three pieces of evidence, each measured against something real where possible:

- outcome: our engagement against the account's own spread. Below its 10th percentile is an
  underperformance; inside the 10th-90th band is ordinary variance and nothing gets fixed.
- story: how other accounts' posts on the same narrative did, each relative to its author's median.
  Was the input alive? For a reply, the peers are the other replies to the same target post.
- draft: a quality score for our text (the post judge's score_100 / 100) plus any critic flags.

A verdict names the stage to fix and cites the evidence it used. When evidence a verdict needs is
missing, the verdict is `insufficient_evidence` with the missing fields listed, never a guess.
One verdict proves little: by construction a tenth of ordinary posts land under the 10th percentile.
`aggregate` recommends changing a stage only after `min_repeats` matching verdicts.
All thresholds are in settings.FaultConfig and none has been tested.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field

from harness.evals.settings import FaultConfig

VERDICTS = {
    "no_fault": None,  # outperformed the account's usual band
    "variance": None,  # inside the band: ordinary spread, fix nothing
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
    account_p10: float | None = None  # the account's own spread for the same action type and window
    account_p90: float | None = None
    account_median: float | None = None
    n_baseline_posts: int | None = None
    story_peer_ratio: float | None = None  # median of peers' (engagement + 1) / (their author median + 1)
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
    evidence: dict = field(default_factory=dict)  # the FaultEvidence the verdict was computed from

    def to_dict(self) -> dict:
        return asdict(self)


def outcome_ratio(engagement: float, account_median: float) -> float:
    """(engagement + 1) / (median + 1): defined for a zero median, and 1.0 means a typical post."""
    return (engagement + 1) / (account_median + 1)


def _verdict(e: FaultEvidence, verdict: str, reasons: list[str], missing=()) -> FaultVerdict:
    ratio = outcome_ratio(e.engagement, e.account_median) if None not in (e.engagement, e.account_median) else None
    return FaultVerdict(e.action_id, verdict, VERDICTS[verdict], reasons, list(missing), ratio, asdict(e))


def attribute(e: FaultEvidence, cfg: FaultConfig = FaultConfig()) -> FaultVerdict:
    need = (("engagement", e.engagement), ("account_p10", e.account_p10), ("account_p90", e.account_p90),
            ("n_baseline_posts", e.n_baseline_posts))
    missing = [name for name, value in need if value is None]
    if missing:
        return _verdict(e, "insufficient_evidence", ["can't tell whether the action underperformed"], missing)
    if e.n_baseline_posts < cfg.min_baseline_posts:
        return _verdict(e, "insufficient_evidence",
                        [f"account band rests on {e.n_baseline_posts} posts; need {cfg.min_baseline_posts}"],
                        ["n_baseline_posts"])
    if e.engagement > e.account_p90:
        return _verdict(e, "no_fault", [f"engagement {e.engagement:g} above the account's p90 {e.account_p90:g}"])
    if e.engagement >= e.account_p10:
        return _verdict(e, "variance", [f"engagement {e.engagement:g} inside the account's band "
                                        f"{e.account_p10:g}-{e.account_p90:g}"])
    reasons = [f"underperformed: engagement {e.engagement:g} below the account's p10 {e.account_p10:g}"]

    missing = []
    if e.story_peer_ratio is None or e.n_peer_posts is None:
        missing.append("story_peer_ratio")
    elif e.n_peer_posts < cfg.min_peer_posts:
        missing.append(f"story_peer_ratio (only {e.n_peer_posts} peer posts; need {cfg.min_peer_posts})")
    if e.draft_score is None and not e.critic_flags:
        missing.append("draft_score")
    if missing:
        return _verdict(e, "insufficient_evidence", reasons + ["need both story and draft evidence to pick a stage"],
                        missing)

    if e.story_peer_ratio >= cfg.story_alive_ratio:
        story = "alive"
    elif e.story_peer_ratio < cfg.story_dead_ratio:
        story = "dead"
    else:
        story = "borderline"
    reasons.append(f"story {story}: peers did {e.story_peer_ratio:.2f}x their usual over {e.n_peer_posts} posts")

    if e.critic_flags:
        draft = "bad"
        reasons.append(f"draft bad: critic flagged {', '.join(e.critic_flags)}")
    else:
        draft = "good" if e.draft_score >= cfg.draft_good else "bad" if e.draft_score < cfg.draft_bad else "borderline"
        reasons.append(f"draft {draft}: judge score {e.draft_score:.2f}")

    table = {
        ("alive", "bad"): "action_fault",
        ("dead", "good"): "observe_fault",
        ("dead", "bad"): "both",
        ("alive", "good"): "unexplained",
    }
    return _verdict(e, table.get((story, draft), "uncertain"), reasons)


def aggregate(verdicts: list[FaultVerdict], cfg: FaultConfig = FaultConfig()) -> dict:
    """Count verdicts by stage and name the stages that reached `min_repeats` matching verdicts."""
    stages = Counter(v.stage_to_fix for v in verdicts if v.stage_to_fix)
    return {
        "counts": dict(Counter(v.verdict for v in verdicts)),
        "by_stage": dict(stages),
        "fix": sorted(stage for stage, n in stages.items() if n >= cfg.min_repeats),
    }
