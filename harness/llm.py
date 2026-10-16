"""Provider-agnostic LLM wrapper. Swap backends with HARNESS_PROVIDER."""

import json
import os
import re
from typing import Any, Callable

from harness import config

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5"}


class LLM:
    def complete(self, system: str, prompt: str) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, prompt: str, mock: Callable[[], Any] | None = None) -> Any:
        """Ask for JSON and parse it. `mock` supplies canned output for the offline backend."""
        text = self.complete(system + "\n\nRespond with JSON only, no prose.", prompt)
        return parse_json(text)


class AnthropicLLM(LLM):
    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model

    def complete(self, system: str, prompt: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")


class OpenAICompatLLM(LLM):
    def __init__(self, model: str, api_key: str | None, base_url: str | None):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def complete(self, system: str, prompt: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""


class MockLLM(LLM):
    """Offline backend so the pipeline runs end to end without any API key."""

    is_mock = True  # read through wrappers by harness/minds/routing.py; class checks break under tracing

    def complete(self, system: str, prompt: str) -> str:
        return "(mock completion)"

    def complete_json(self, system: str, prompt: str, mock: Callable[[], Any] | None = None) -> Any:
        return mock() if mock else {}


def parse_json(text: str) -> Any:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if not match:
            raise ValueError(f"LLM did not return JSON: {text[:200]}")
        return json.loads(match.group(1))


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
        return OpenAICompatLLM(model, os.getenv("XAI_API_KEY"), "https://api.x.ai/v1")
    if provider == "openai":
        return OpenAICompatLLM(model, os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_BASE_URL") or None)
    raise SystemExit(f"Unknown HARNESS_PROVIDER '{provider}'.")
