"""The knob registry: every tunable in the harness, with its type, allowed values and current default.

A `Config` is one full assignment of values to every registered knob. Its identity is the SHA-256 of
its canonical JSON, so two configs that set the same values hash the same regardless of key order,
and adding a knob to the registry changes every hash (a config with a new knob is a different config).

Each knob names the file that reads it today in `reader`. An empty `reader` means no stage reads the
knob yet: the fitness functions can still test it, but promoting it changes nothing in a live run
until that stage reads `champion().get(path)`. DESIGN.md lists which stages need to do that.

Defaults are the stages' current behavior on `main` at 73d7432 unless the knob only exists on a
worker branch, in which case `reader` names the branch.
"""

import difflib
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "iterate.config/v1"

SONNET = "claude-sonnet-5-5"
OPUS55 = "claude-opus-5-5"
OPUS45 = "claude-opus-4-5-20251101"  # the only gateway model that accepts assistant prefill
GPT_SOL = "gpt-6.1-sol"
MODELS = (SONNET, OPUS55, OPUS45, GPT_SOL)


class KnobError(ValueError):
    """A config or proposal names an unknown knob or a value outside its domain."""


@dataclass(frozen=True)
class Knob:
    path: str
    kind: type  # str, int, float, bool or tuple (a set of choices, stored sorted)
    default: Any
    stage: str
    help: str
    choices: tuple = ()  # for str: allowed values; for tuple: allowed members
    lo: float | None = None
    hi: float | None = None
    reader: str = ""  # file that reads this knob today; "" when nothing does yet

    def check(self, value: Any) -> Any:
        """Return the canonical form of `value`, or raise KnobError naming the knob and the problem."""
        if self.kind is bool:
            if not isinstance(value, bool):
                raise KnobError(f"{self.path}: expected true/false, got {value!r}")
            return value
        if self.kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise KnobError(f"{self.path}: expected an integer, got {value!r}")
            return self._range(value)
        if self.kind is float:
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise KnobError(f"{self.path}: expected a finite number, got {value!r}")
            return self._range(float(value))
        if self.kind is str:
            if not isinstance(value, str):
                raise KnobError(f"{self.path}: expected a string, got {value!r}")
            if self.choices and value not in self.choices:
                raise KnobError(f"{self.path}: {value!r} is not one of {list(self.choices)}")
            return value
        if self.kind is tuple:
            if isinstance(value, str) or not isinstance(value, list | tuple | set | frozenset):
                raise KnobError(f"{self.path}: expected a list, got {value!r}")
            members = sorted(set(value))
            if len(members) != len(list(value)):
                raise KnobError(f"{self.path}: duplicate members in {value!r}")
            bad = [v for v in members if not isinstance(v, str) or (self.choices and v not in self.choices)]
            if bad:
                raise KnobError(f"{self.path}: {bad} not in {list(self.choices)}")
            return tuple(members)
        raise KnobError(f"{self.path}: unsupported kind {self.kind}")

    def _range(self, value):
        if self.lo is not None and value < self.lo or self.hi is not None and value > self.hi:
            raise KnobError(f"{self.path}: {value} outside [{self.lo}, {self.hi}]")
        return value

    def domain(self) -> dict:
        """JSON description of the allowed values, for proposers and the docs."""
        out: dict[str, Any] = {"type": self.kind.__name__ if self.kind is not tuple else "set"}
        if self.choices:
            out["choices"] = list(self.choices)
        if self.lo is not None or self.hi is not None:
            out["range"] = [self.lo, self.hi]
        return out


MINDS = ("community_builder", "meme_lord", "reply_sniper", "rivalry_banterer", "stat_nerd")
VETO_CRITERIA = ("harassment", "factual", "off_mission", "no_target", "ai_register", "mute_risk", "ungrounded")

REGISTRY: tuple[Knob, ...] = (
    # Decide (action)
    Knob("action.minds", tuple, MINDS, "action", "Persona minds that draft.", choices=MINDS,
         reader="harness/minds/__init__.py:load_minds (every prompt file; no filter yet)"),
    Knob("action.lanes", tuple, ("post", "reply"), "action", "Lanes that run.", choices=("post", "reply"),
         reader="harness/minds/lanes.py (sam/action)"),
    Knob("action.reply.generation", str, "persona_json", "action",
         "How reply text is produced: the persona JSON call on main, a short style-rule prompt, continuing "
         "the real thread as a document (research/action.md, finding 2), or sam/action's whole reply lane "
         "(continuation, slop gate, two-order Opus veto, random pick among survivors).",
         choices=("persona_json", "styled", "continuation", "pipeline"), reader="harness/minds/__init__.py:PROMPT"),
    Knob("action.reply.model", str, SONNET, "action", "Model for persona_json and styled generation.",
         choices=MODELS, reader="harness/llm.py:DEFAULT_MODELS"),
    Knob("action.continuation.model", str, OPUS45, "action", "Model for continuation; needs prefill.",
         choices=(OPUS45,), reader=""),
    Knob("action.per_mind", int, 3, "action", "Drafts each mind proposes per run.", lo=1, hi=8,
         reader="harness/cli.py --per-mind"),
    Knob("action.samples", int, 1, "action", "Drafts sampled per reply opportunity before the gate and veto.",
         lo=1, hi=12, reader="harness/minds/settings.py:ActionConfig.reply_samples (sam/action, 6)"),
    Knob("action.page_replies", int, 12, "action", "Real replies on the continuation page.", lo=5, hi=20,
         reader="harness/minds/settings.py:ActionConfig.page_replies (sam/action)"),
    Knob("action.veto.model", str, OPUS55, "action", "Model of the reply lane's veto.", choices=MODELS,
         reader="harness/minds/settings.py:ActionConfig.veto_model (sam/action)"),
    Knob("action.veto.max_p_ai", int, 50, "action", "Veto a draft the judge puts at or above this P(AI), either order.",
         lo=10, hi=90, reader="harness/minds/settings.py:ActionConfig.veto_max_p_ai (sam/action)"),
    Knob("action.gate", str, "off", "action", "Deterministic slop gate before any model sees a draft.",
         choices=("off", "slop", "slop+grounding"), reader="harness/minds/gate.py (sam/action)"),
    Knob("action.ship_rule", str, "critic_pass", "action",
         "When a draft ships: whatever the critic keeps (main), or only a Strong pick in both orders.",
         choices=("critic_pass", "strong_both_orders"), reader="harness/minds/judge.py:select (sam/action)"),
    Knob("action.retries", int, 2, "action", "Revision rounds when every draft is vetoed.", lo=0, hi=4,
         reader="harness/cli.py --retries"),
    Knob("action.top", int, 10, "action", "Recommendations written per run.", lo=1, hi=30,
         reader="harness/cli.py --top"),
    # Judge (the critic that vetoes, and any ranker)
    Knob("judge.model", str, SONNET, "judge", "Model for the critic.", choices=MODELS,
         reader="harness/qualify.py (uses the harness LLM)"),
    Knob("judge.role", str, "rank_and_veto", "judge", "Rank drafts and veto, or veto only (research/action.md, finding 3).",
         choices=("rank_and_veto", "veto_only"), reader="harness/qualify.py:qualify"),
    Knob("judge.veto_criteria", tuple, ("factual", "harassment", "no_target", "off_mission"), "judge",
         "What the critic vetoes on.", choices=VETO_CRITERIA, reader="harness/qualify.py:CRITIC_SYSTEM"),
    Knob("judge.orders", int, 1, "judge", "Orders each batch is judged in; 2 counts a split verdict as none.",
         lo=1, hi=2, reader=""),
    # Qualify (the audience panel that predicts engagement)
    Knob("qualify.panel", str, "personas", "qualify", "Persona panel, or one no-persona reader (research/eval.md, finding 8).",
         choices=("personas", "single_reader"), reader="harness/qualify.py:PANEL"),
    Knob("qualify.model", str, SONNET, "qualify", "Model the panel runs on.", choices=MODELS,
         reader="harness/qualify.py (uses the harness LLM)"),
    Knob("qualify.batch", int, 0, "qualify", "Drafts per panel call; 0 scores every draft in one call (main).",
         lo=0, hi=20, reader="harness/qualify.py:qualify"),
    Knob("qualify.memory", str, "off", "qualify",
         "Show the panel the brand's own older posts with their outcomes: off, on, or on with labels shuffled.",
         choices=("off", "on", "shuffled"), reader=""),
    # Orient
    Knob("orient.budget_chars", int, 60_000, "orient", "Signal payload cap.", lo=5_000, hi=200_000,
         reader="harness/orient.py:orient"),
    Knob("orient.narratives", int, 5, "orient", "Narratives kept.", lo=1, hi=12, reader="harness/cli.py --narratives"),
    Knob("orient.focus", int, 2, "orient", "Top narratives every mind sees.", lo=1, hi=6,
         reader="harness/minds/__init__.py:target_narratives"),
    Knob("orient.explore", int, 1, "orient", "Random lower-ranked narratives added to test the ranking.", lo=0, hi=3,
         reader="harness/minds/__init__.py:target_narratives"),
    # Observe
    Knob("observe.sources", tuple, ("sample",), "observe", "Source plugins.",
         choices=("sample", "reddit", "x_search", "espn", "google_news", "x_targets", "bluesky"),
         reader="harness/config.py:SOURCES"),
    Knob("observe.reply_target.size_band", str, "10k-250k", "observe",
         "Which parent-author sizes a reply target may have (research/observe.md, finding 4).",
         choices=("any", "10k-250k"), reader="harness/sources/reply_targets.py:size_band (sam/observe)"),
    Knob("observe.reply_target.max_age_minutes", int, 90, "observe", "Oldest post offered as a reply target.",
         lo=10, hi=360, reader="harness/sources/reply_targets.py (sam/observe)"),
    Knob("observe.news_max_age_hours", int, 72, "observe", "Oldest news signal orient sees.", lo=6, hi=336,
         reader="harness/orient.py (sam/observe)"),
    # Memory
    Knob("memory.lessons", str, "on", "memory", "Lessons in the minds' prompt: off, on, or shuffled across lessons.",
         choices=("off", "on", "shuffled"), reader="harness/config.py:lessons"),
    Knob("memory.recall", str, "on", "memory", "Memories at round start: off, on, or random memories of the same kinds.",
         choices=("off", "on", "shuffled"), reader="harness/learning/ranking.py:round_start (sam/memory)"),
    Knob("memory.retrieval_k", int, 5, "memory", "Memories retrieved per query.", lo=0, hi=20,
         reader="harness/memstore context_for(k=5) (krish/memory)"),
    Knob("memory.prompt_chars", int, 6000, "memory", "Rendered memory page cap.", lo=0, hi=20_000,
         reader="harness/memstore prompt_block(max_chars=6000) (krish/memory)"),
)

BY_PATH: dict[str, Knob] = {k.path: k for k in REGISTRY}
assert len(BY_PATH) == len(REGISTRY), "duplicate knob path"


@dataclass(frozen=True)
class Config:
    """A full, validated assignment of every registered knob."""

    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, values: Mapping[str, Any] | None = None, base: "Config | None" = None) -> "Config":
        """Defaults (or `base`), overridden by `values`. Unknown knobs and bad values raise KnobError."""
        merged = dict(base.values) if base else {k.path: k.default for k in REGISTRY}
        for path, value in (values or {}).items():
            if path not in BY_PATH:
                raise KnobError(f"unknown knob {path!r}; registered knobs are listed in harness/iterate/knobs.py")
            merged[path] = value
        missing = set(BY_PATH) - set(merged)
        extra = set(merged) - set(BY_PATH)
        if missing or extra:
            raise KnobError(f"config must set exactly the registered knobs; missing {sorted(missing)}, extra {sorted(extra)}")
        return cls({p: BY_PATH[p].check(merged[p]) for p in sorted(merged)})

    def get(self, path: str) -> Any:
        if path not in BY_PATH:
            raise KnobError(f"unknown knob {path!r}")
        return self.values[path]

    def with_changes(self, changes: Mapping[str, Any]) -> "Config":
        return Config.build(changes, base=self)

    def canonical(self) -> str:
        return canonical_json({"schema": SCHEMA, "values": self.values})

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    @property
    def ref(self) -> str:
        return f"cfg-{self.hash[:12]}"

    def to_json(self) -> dict:
        return {"schema": SCHEMA, "hash": self.hash, "values": {p: _jsonable(v) for p, v in self.values.items()}}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Config":
        """Rebuild a stored config. Knobs added since it was stored take their defaults; knobs since
        removed raise, because silently dropping one would change what the config means."""
        if data.get("schema") != SCHEMA:
            raise KnobError(f"config schema {data.get('schema')!r}; expected {SCHEMA!r}")
        return cls.build(data["values"])


def _jsonable(v: Any) -> Any:
    return list(v) if isinstance(v, tuple) else v


def canonical_json(obj: Any) -> str:
    """Sorted keys, no whitespace, tuples as lists, ASCII-escaped. NaN and infinity are rejected."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
                      default=lambda o: list(o) if isinstance(o, tuple | frozenset | set) else _fail(o))


def _fail(o: Any):
    raise TypeError(f"not JSON-serializable in a config: {o!r}")


def diff(a: Config, b: Config) -> dict[str, dict[str, Any]]:
    """{knob path: {"from": a's value, "to": b's value}} for every knob that differs."""
    return {p: {"from": _jsonable(a.values[p]), "to": _jsonable(b.values[p])}
            for p in sorted(BY_PATH) if a.values[p] != b.values[p]}


def unified_diff(a: Config, b: Config) -> str:
    left = json.dumps(a.to_json()["values"], indent=1, sort_keys=True).splitlines()
    right = json.dumps(b.to_json()["values"], indent=1, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(left, right, a.ref, b.ref, lineterm="", n=0))


def readers(paths) -> list[str]:
    """Files that read the given knobs, for the ledger's change.files."""
    return sorted({BY_PATH[p].reader for p in paths if BY_PATH[p].reader})


DEFAULT = Config.build()
