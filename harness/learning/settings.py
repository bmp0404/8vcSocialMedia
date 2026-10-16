"""Every tunable in the memory stage, in one typed object.

Defaults are the values the code ran with on 2026-10-15. None of them was tuned; where a value has
evidence behind it, the comment says what. `memory/settings.json`, when present, overrides any field,
so a tuning loop can promote a proven change by writing that file instead of editing code.
"""

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from harness import config

SETTINGS_PATH = config.ROOT / "memory" / "settings.json"


@dataclass
class ReflectSettings:
    n_batches: int = 2  # proposal batches per run; one LLM call each
    groups_per_batch: int = 4  # account strata per batch
    per_side: int = 3  # failures and successes shown per stratum
    n_hypotheses: int = 5  # hypotheses asked for per batch
    fdr_q: float = 0.1  # Benjamini-Hochberg false discovery rate across a run's hypotheses
    min_posts_with_trait: int = 5  # held-out posts with the trait needed before a verdict counts
    heldout_min_per_side: int = 2  # a stratum joins the held-out half only with this many of each outcome
    label_chunk: int = 25  # posts per labeling call
    # Retest a lesson once this many new labeled episodes exist in its domain. A guess, no evidence.
    min_new_labeled: int = 50
    valid_days: int = 30  # date fallback for the retest; a guess, no evidence
    # Thread continuations: 27% of posts and 4% viral (8vc-context/research/eval.md).
    thread_gap_seconds: int = 180
    min_label_age_hours: float = 48  # younger posts' engagement is still growing
    require_boost_flags: bool = True  # refuse to run on the dataset without the context worker's boost flags
    # "category": the CSV's raw cutoffs (Deadweight vs viral). "author_prior": the context worker's within-account
    # label, which leaves far more comparable posts (477 in 33 accounts vs about 150 in 18 after filters).
    # `learn` sets "review" for the account's own drafts. Every test records its label source.
    label_source: str = "category"
    proposer_model: str = ""  # "" uses HARNESS_MODEL or the provider default
    label_model: str = ""  # use a different model from the proposer when the gateway has one


@dataclass
class RecallSettings:
    mode: str = "on"  # "on", "off" (no memories) or "shuffled" (same budget, random memories) for ablation
    search_k: int = 40  # search hits considered per narrative
    budget_chars: int = 2500  # cap on the rendered memory block per narrative
    # Generative Agents (Park et al. 2023) set all three weights to 1.
    weight_relevance: float = 1.0
    weight_trust: float = 1.0
    weight_recency: float = 1.0
    kind_caps: dict = field(default_factory=lambda: {"lesson": 4, "procedure": 1, "fact": 3, "episode": 2,
                                                       "reflection": 1, "entity": 2})
    kind_trust: dict = field(default_factory=lambda: {"procedure": 1.0, "fact": 1.0, "entity": 0.8,
                                                        "reflection": 0.5})
    other_domain_factor: float = 0.6  # general-scope lessons from another domain; no evidence for 0.6
    # Past posts by other accounts. exemplar_check on the dataset (5 random 400-post splits, k=10): keyword
    # neighbors from the same account beat the account's own majority label (0.853 vs 0.798 on the same
    # posts); neighbors from other accounts fell short of it (0.649 vs 0.787). The 0.5 itself is a guess.
    other_author_factor: float = 0.5
    # Lessons that passed only on the dataset's Category label, whose cutoffs mostly track account size. Shown,
    # marked as confounded, at reduced trust. No evidence for 0.5.
    confounded_label_factor: float = 0.5
    half_life_days: float = 30.0
    undated_recency: float = 0.5
    # Domains whose lessons and past posts apply at full weight. "brand" is the account's own runs; add
    # "devtools" when the account is a dev-tool brand. Other domains' episodes are excluded, and their lessons
    # count only once they pass on a second domain (scope "general").
    home_domains: list = field(default_factory=lambda: ["brand"])
    home_authors: list = field(default_factory=lambda: ["brand"])  # usernames whose past posts count as ours


@dataclass
class IngestSettings:
    exemplars_per_category: int = 5  # top and bottom posts of each dataset Category written as notes
    brand_max_pages: int = 6
    fact_valid_days: int = 30  # a guess, no evidence


@dataclass
class MemorySettings:
    reflect: ReflectSettings = field(default_factory=ReflectSettings)
    recall: RecallSettings = field(default_factory=RecallSettings)
    ingest: IngestSettings = field(default_factory=IngestSettings)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MemorySettings":
        """Defaults overridden by `data`; an unknown section or key raises, so a typo can't pass silently."""
        out = cls()
        for section, values in data.items():
            if section not in {f.name for f in fields(cls)}:
                raise KeyError(f"unknown memory settings section {section!r}")
            target = getattr(out, section)
            known = {f.name for f in fields(target)}
            for key, value in values.items():
                if key not in known:
                    raise KeyError(f"unknown memory setting {section}.{key}")
                setattr(target, key, value)
        if out.recall.mode not in ("on", "off", "shuffled"):
            raise ValueError(f"recall.mode must be on, off or shuffled, got {out.recall.mode!r}")
        return out


def load_settings(path: Path = SETTINGS_PATH) -> MemorySettings:
    if not path.exists():
        return MemorySettings()
    return MemorySettings.from_dict(json.loads(path.read_text(encoding="utf-8")))
