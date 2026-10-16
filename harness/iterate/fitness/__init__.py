"""Fitness functions: frozen measurement instruments the loop scores configs with.

An instrument's own settings (the detector model, the lineup prompt, the dataset filters) are not
knobs. Proposers can't touch them, and the ledger records them in `eval.instrument`, so a promotion
can never come from changing the ruler instead of the harness.

Each instrument returns per-item results for one arm. The runner pairs arms on the items they all
scored and turns those results into a metric with `stat()`.
"""

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.iterate.knobs import Config
from harness.iterate.stats import Stat
from harness.llm import LLM

LLMFor = Callable[[str], LLM]  # model id -> an LLM charged to the current arm


@dataclass(frozen=True)
class Item:
    id: str
    cluster: Hashable  # the resampling unit: an account, a thread
    payload: Any = field(compare=False, hash=False, repr=False)


@dataclass
class ArmResult:
    values: dict[str, float]  # item id -> the per-item value `stat` reads (an outcome or a score)
    detail: dict[str, dict] = field(default_factory=dict)  # item id -> what produced it, for the report
    failures: dict[str, str] = field(default_factory=dict)  # item id -> why it has no value
    secondary: dict[str, dict[str, float]] = field(default_factory=dict)  # metric -> item id -> value


@dataclass(frozen=True)
class Metric:
    name: str
    higher_is_better: bool
    guardrail: bool = False  # a secondary the challenger must be shown non-inferior on to be promoted
    margin: float = 0.0  # how much worse a guardrail may get, in the metric's units


class Fitness(Protocol):
    name: str
    stage: str
    unit: str  # what an item is: "post", "thread"
    cluster_unit: str  # what gets resampled: "account", "thread"
    primary: Metric
    secondaries: tuple[Metric, ...]
    knobs: tuple[str, ...]  # the knobs this instrument reads; a change to any other knob can't move it

    def items(self) -> list[Item]: ...

    def instrument(self) -> dict: ...

    def run_arm(self, config: Config, items: list[Item], llm_for: LLMFor, *, seed: int, repeat: bool) -> ArmResult: ...

    def stat(self, items: list[Item], values: dict[str, float], metric: Metric) -> Stat: ...

    def estimate_calls(self, n_items: int) -> int: ...
