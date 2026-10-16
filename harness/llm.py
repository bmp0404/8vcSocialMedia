"""Provider-agnostic LLM wrapper. Swap backends with HARNESS_PROVIDER."""

import hashlib
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
        """Ask for JSON and parse it (one retry on unparseable output). `mock` is for the offline backend."""
        system += "\n\nRespond with JSON only, no prose."
        try:
            return parse_json(self.complete(system, prompt))
        except ValueError:  # includes json.JSONDecodeError and refusals
            return parse_json(self.complete(system, prompt))


class AnthropicLLM(LLM):
    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        # HARNESS_EFFORT: low | medium | high | xhigh | max (unset = the model's default)
        self.effort = os.getenv("HARNESS_EFFORT", "").strip().lower() or None

    def complete(self, system: str, prompt: str) -> str:
        extra = {"output_config": {"effort": self.effort}} if self.effort else {}
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=16000,  # thinking shares this budget; 4096 risked truncating panel/critic JSON
            system=system,
            messages=[{"role": "user", "content": prompt}],
            **extra,
        )
        if resp.stop_reason == "refusal":
            raise ValueError("model declined the request (stop_reason=refusal)")
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


MOCK_LESSONS = """# Lessons

(mock provider: canned lessons so `learn` runs offline)

## General
- Specific beats generic: drafts that name a concrete detail from the source outperform vague hype.
- The audience panel over-predicts long drafts; keep it under ~200 characters.

## Reply lane
- Early replies on high-reply threads earn the most reach for a small account; add a joke or a fact, never just agree.
- Follows only pay off on accounts that already reply to fans.

## Post lane
- One sharp angle per post; threads of hype underperform.
"""


class MockLLM(LLM):
    """Offline backend so the pipeline runs end to end without any API key.

    JSON calls return the caller's `mock()` fixture; free-text calls return canned lessons.
    """

    def complete(self, system: str, prompt: str) -> str:
        return MOCK_LESSONS

    def complete_json(self, system: str, prompt: str, mock: Callable[[], Any] | None = None) -> Any:
        return mock() if mock else {}


def mock_score(*parts: Any, lo: float = 0.2, hi: float = 0.85) -> float:
    """Deterministic pseudo-random score in [lo, hi] so mock rankings aren't all ties."""
    h = int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return round(lo + (hi - lo) * h, 2)


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
