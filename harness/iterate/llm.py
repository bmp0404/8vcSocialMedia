"""Metered model access for experiments: every call is counted against the run's budget, by arm.

`harness.llm.AnthropicLLM` drops the response's token usage and can't send an assistant prefill, and
both matter here, so experiments go through `MeteredLLM`. It still subclasses `harness.llm.LLM`, so
anything that takes the harness LLM (the minds, the eval worker's PanelPredictor) accepts it.
"""

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from harness.llm import LLM, parse_json


class BudgetExceeded(RuntimeError):
    """The run hit its call cap. The arm in progress stops; finished arms keep their results."""


class Meter:
    """Thread-safe call and token counts per arm, with a hard cap on total calls."""

    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.lock = threading.Lock()
        self.by_arm: dict[str, dict[str, int]] = defaultdict(lambda: {"llm_calls": 0, "input_tokens": 0,
                                                                     "output_tokens": 0, "errors": 0})

    @property
    def calls(self) -> int:
        return sum(a["llm_calls"] for a in self.by_arm.values())

    def reserve(self, arm: str) -> None:
        with self.lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(f"run cap of {self.max_calls} calls reached during arm {arm!r}")
            self.by_arm[arm]["llm_calls"] += 1

    def record(self, arm: str, input_tokens: int = 0, output_tokens: int = 0, error: bool = False) -> None:
        with self.lock:
            self.by_arm[arm]["input_tokens"] += input_tokens
            self.by_arm[arm]["output_tokens"] += output_tokens
            self.by_arm[arm]["errors"] += int(error)

    def remaining(self) -> int:
        with self.lock:
            return self.max_calls - self.calls


class MeteredLLM(LLM):
    """One model on the Anthropic-compatible gateway, charged to one arm of one run."""

    def __init__(self, model: str, meter: Meter, arm: str, max_tokens: int = 2000, retries: int = 3):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model, self.meter, self.arm, self.max_tokens, self.retries = model, meter, arm, max_tokens, retries

    def _create(self, **kw) -> str:
        import anthropic

        for attempt in range(self.retries + 1):
            self.meter.reserve(self.arm)
            try:
                resp = self.client.messages.create(model=self.model, **kw)
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
                self.meter.record(self.arm, error=True)
                if attempt == self.retries:
                    raise
                print(f"[iterate] {self.arm} {self.model} retry after {type(e).__name__}: {str(e)[:100]}")
                time.sleep(2 * (attempt + 1))
                continue
            usage = getattr(resp, "usage", None)
            self.meter.record(self.arm, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
            return "".join(b.text for b in resp.content if b.type == "text")
        raise AssertionError("unreachable")

    def complete(self, system: str, prompt: str) -> str:
        return self._create(max_tokens=self.max_tokens, system=system, messages=[{"role": "user", "content": prompt}])

    def continue_text(self, user: str, prefill: str, stop: list[str], max_tokens: int = 80) -> str:
        """Assistant-prefill continuation. Only prefill-capable models accept this (claude-opus-4-5)."""
        return self._create(max_tokens=max_tokens, stop_sequences=stop, messages=[
            {"role": "user", "content": user}, {"role": "assistant", "content": prefill}])


class MockMeteredLLM(LLM):
    """Offline stand-in. `respond(system, prompt)` returns canned text; calls still count, so a mock
    run exercises the budget path. Mock numbers are fake and never reach eval/experiments/."""

    is_mock = True  # the flag harness.llm.MockLLM carries; check it with getattr, not isinstance

    def __init__(self, meter: Meter, arm: str, respond: Callable[[str, str], Any], model: str = "mock"):
        self.meter, self.arm, self.respond, self.model = meter, arm, respond, model

    def complete(self, system: str, prompt: str) -> str:
        self.meter.reserve(self.arm)
        out = self.respond(system, prompt)
        return out if isinstance(out, str) else __import__("json").dumps(out)

    def complete_json(self, system: str, prompt: str, mock: Callable[[], Any] | None = None) -> Any:
        self.meter.reserve(self.arm)
        if mock is not None:
            return mock()
        out = self.respond(system, prompt)
        return parse_json(out) if isinstance(out, str) else out

    def continue_text(self, user: str, prefill: str, stop: list[str], max_tokens: int = 80) -> str:
        return self.complete("", prefill)


class _MeteredMessages:
    """`client.messages` with every `create` charged to one arm."""

    def __init__(self, inner, meter: Meter, arm: str):
        self.inner, self.meter, self.arm = inner, meter, arm

    def create(self, **kw):
        self.meter.reserve(self.arm)
        try:
            resp = self.inner.create(**kw)
        except Exception:
            self.meter.record(self.arm, error=True)
            raise
        usage = getattr(resp, "usage", None)
        self.meter.record(self.arm, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
        return resp


def metered_anthropic_class(meter: Meter, arm: str) -> type:
    """A `harness.llm.AnthropicLLM` subclass whose client is metered, for code that type-checks for
    AnthropicLLM, reaches into `llm.client`, or builds `AnthropicLLM(model)` itself (sam/action's
    writer and veto do all three). Swap it in for the module's `AnthropicLLM` name while that code runs."""
    import anthropic

    from harness.llm import AnthropicLLM

    class MeteredAnthropicLLM(AnthropicLLM):
        def __init__(self, model: str):
            client = anthropic.Anthropic()
            client.messages = _MeteredMessages(client.messages, meter, arm)
            self.client, self.model = client, model

    return MeteredAnthropicLLM
