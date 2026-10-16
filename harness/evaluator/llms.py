"""Model tiers for the evaluator: a cheap `fast` model and the configured `judge` model.

llm.py has no per-call model override, so the fast tier builds its own AnthropicLLM:
  fast  = HARNESS_FAST_MODEL (default claude-haiku-5-5) at HARNESS_FAST_EFFORT (default low)
  judge = whatever get_llm() built from config (HARNESS_MODEL, default claude-sonnet-5-5)
Non-Anthropic providers use their configured model for both tiers; mock stays mock.
Both tiers are wrapped in the disk cache.
"""

import os

from harness.evaluator.cache import CachedLLM
from harness.llm import LLM, AnthropicLLM, MockLLM

FAST_MODEL = os.getenv("HARNESS_FAST_MODEL", "claude-haiku-5-5")
FAST_EFFORT = os.getenv("HARNESS_FAST_EFFORT", "low").strip().lower() or None

_fast: dict[int, CachedLLM] = {}


def _unwrap(llm: LLM) -> LLM:
    return llm.inner if isinstance(llm, CachedLLM) else llm


def _label(llm: LLM) -> str:
    if isinstance(llm, MockLLM):
        return "mock"
    return f"{type(llm).__name__}:{getattr(llm, 'model', '?')}"


def judge_llm(base: LLM) -> CachedLLM:
    if isinstance(base, CachedLLM):
        return base
    return CachedLLM(base, _label(base))


def fast_llm(base: LLM) -> CachedLLM:
    inner = _unwrap(base)
    if id(inner) not in _fast:
        if isinstance(inner, AnthropicLLM):
            fast = AnthropicLLM(FAST_MODEL)
            fast.effort = FAST_EFFORT
        else:
            fast = inner
        _fast[id(inner)] = CachedLLM(fast, _label(fast))
    return _fast[id(inner)]


def model_name(llm: LLM) -> str:
    return getattr(_unwrap(llm), "model", "mock" if isinstance(_unwrap(llm), MockLLM) else "?")
