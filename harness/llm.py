"""Provider-agnostic LLM wrapper. Swap backends with HARNESS_PROVIDER."""

import json
import os
import re
from typing import Any, Callable

from harness import config

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5"}
# The cap covers thinking as well as the answer. On the gateway's claude-sonnet-5-5, thinking used
# every token of the old 4,096 cap and the answer came back empty; the same memory brand-ingest
# call used 8,248 output tokens once the cap was 16,000. Keep it under 21,333: above that the anthropic
# SDK refuses non-streaming requests (_calculate_nonstreaming_timeout).
MAX_TOKENS = int(os.getenv("HARNESS_MAX_TOKENS", "16000"))


class TruncatedError(RuntimeError):
    """The reply stopped at the output-token cap, so it is incomplete (ported from aarav/self-improving).

    A RuntimeError, not a ValueError, so complete_json's JSON retry never catches it: a cut-off answer
    must not be parsed, and the same request would be cut off again."""


def check_stop(stop_reason: str | None, model: str, max_tokens: int | str) -> None:
    """Raise TruncatedError for the stop reasons that mean "out of tokens": Anthropic's max_tokens and
    the OpenAI-compatible length."""
    if stop_reason in ("max_tokens", "length"):
        raise TruncatedError(f"{model} stopped at its output cap ({max_tokens} tokens) with stop reason "
                             f"{stop_reason!r}; the reply is cut off. Ask for less output or raise HARNESS_MAX_TOKENS")


class LLM:
    # Read by harness/baml_adapter.py, which builds a BAML function's client from them:
    # - `provider` ("anthropic", "openai", "xai" or "mock") and `model`, set by each backend below.
    # - `on_baml_event`, optional: it receives every BAML ai.events.Event. A BAML call skips
    #   self.client, so a meter that counts tokens there has to read a BAML call's tokens from these.
    # They are deliberately not class attributes here: a wrapper that forwards unknown attributes to
    # the LLM it wraps through __getattr__ would find base-class defaults instead of forwarding.

    def complete(self, system: str, prompt: str) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, prompt: str, mock: Callable[[], Any] | None = None) -> Any:
        """Ask for JSON and parse it, asking once more if the answer isn't JSON. `mock` supplies
        canned output for the offline backend. One malformed orient answer on 2026-10-15 killed a
        live round after four minutes of observing, so a single retry is cheaper than the rerun."""
        system = system + "\n\nRespond with JSON only, no prose."
        text = self.complete(system, prompt)
        try:
            return parse_json(text)
        except ValueError as e:
            print(f"[llm] answer wasn't JSON ({e}); asking once more")
            retry = f"{prompt}\n\nYour last answer wasn't valid JSON ({e}). Answer again with only the JSON."
            return parse_json(self.complete(system, retry))


class AnthropicLLM(LLM):
    provider = "anthropic"

    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model

    def complete(self, system: str, prompt: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        check_stop(resp.stop_reason, self.model, MAX_TOKENS)
        return "".join(b.text for b in resp.content if b.type == "text")


class OpenAICompatLLM(LLM):
    def __init__(self, model: str, api_key: str | None, base_url: str | None, provider: str = "openai"):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.provider = provider

    def complete(self, system: str, prompt: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        )
        check_stop(resp.choices[0].finish_reason, self.model, "the provider's default")
        return resp.choices[0].message.content or ""


class MockLLM(LLM):
    """Offline backend so the pipeline runs end to end without any API key."""

    # Mock detection has to survive wrappers: a tracing proxy fails isinstance(llm, MockLLM), and a
    # mock run then fetched live data and spent the real budget. Callers read this attribute instead.
    is_mock = True
    provider = "mock"

    def complete(self, system: str, prompt: str) -> str:
        return "(mock completion)"

    def complete_json(self, system: str, prompt: str, mock: Callable[[], Any] | None = None) -> Any:
        return mock() if mock else {}


def parse_json(text: str) -> Any:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Take the first JSON value and ignore what follows it. A greedy {.*} match spanned two
        # objects in one live orient answer and failed with "Extra data".
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if not starts:
            raise ValueError(f"LLM did not return JSON: {text[:200]}")
        value, _ = json.JSONDecoder().raw_decode(text, min(starts))
        return value


def get_llm(provider: str | None = None) -> LLM:
    provider = (provider or config.PROVIDER).lower()
    model = config.MODEL or DEFAULT_MODELS.get(provider, "")
    if provider == "mock":
        return MockLLM()
    if not model:
        raise SystemExit(f"Set HARNESS_MODEL for provider '{provider}'.")
    if provider == "anthropic":
        return AnthropicLLM(model)
    if provider == "xai":
        return OpenAICompatLLM(model, os.getenv("XAI_API_KEY"), "https://api.x.ai/v1", provider="xai")
    if provider == "openai":
        return OpenAICompatLLM(model, os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_BASE_URL") or None)
    raise SystemExit(f"Unknown HARNESS_PROVIDER '{provider}'.")
