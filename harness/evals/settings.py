"""Every tunable in the eval stage, in one place, with the values the code runs today.

The self-iteration loop (sam/iterate) reads and varies these. A default here is a choice someone made,
not a tested optimum; each says where it came from. Fault thresholds have never been tested.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BacktestConfig:
    # Eligibility (harness/evals/dataset.py explains the evidence behind both values).
    min_chars: int = 40  # shorter cleaned texts are mostly a link or a hashtag
    thread_gap_minutes: float = 3.0  # 82 of 83 posts under 3 min were self-replies on fxtwitter
    min_age_hours: float = 48.0  # viral rate still climbing at 24-48 h in the research audit
    # Sampling and statistics.
    per_tier: int = 4  # viral and Deadweight posts per account; the audit's protocol section 13
    n_boot: int = 2000
    seed: int = 0
    # Model predictors.
    models: tuple[str, ...] = ("claude-sonnet-5-5",)
    batch: int = 1  # one post per call; batching biased the panel by 0.12 AUC
    judge_references_per_class: int = 3  # Dhilan's select_references default
    memory: str = "on"  # "on" or "shuffled" judge references


@dataclass(frozen=True)
class FaultConfig:
    # Untested. Every value below is a hand-set starting point.
    band_low_percentile: float = 10.0  # under this percentile of the account's own posts = underperformed
    band_high_percentile: float = 90.0  # over it = outperformed; between = ordinary variance
    min_baseline_posts: int = 10  # fewer and the account's percentiles mean little
    min_peer_posts: int = 3
    story_alive_ratio: float = 1.0  # peers on the story did at least their usual
    story_dead_ratio: float = 0.5  # peers did under half their usual
    draft_good: float = 0.6  # judge score_100 / 100
    draft_bad: float = 0.4
    min_repeats: int = 3  # change a stage only after this many matching verdicts


@dataclass(frozen=True)
class EvalConfig:
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    fault: FaultConfig = field(default_factory=FaultConfig)


DEFAULT = EvalConfig()
