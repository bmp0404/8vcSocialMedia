"""Run a BAML function with the harness LLM's provider and model, or the caller's mock on mock runs.

Every harness LLM call goes through an `LLM` from harness/llm.py, which carries the provider, the
model, the offline mock path and the hook a meter reads. Calling a generated BAML function directly
would skip all four. `call` keeps them:

- On a mock run (`llm.is_mock`) it returns `mock()` and never imports BAML.
- Otherwise it passes the LLM's provider and model, and harness.llm.MAX_TOKENS, to the BAML
  function. The function builds its client with `harness_client` (eval/baml_src/orient.baml),
  which reads the same environment variables as harness/llm.py and wraps the client in
  `ai.clients.Retry`. Python can't build the client and pass it in: BAML 0.22.2 rejects a client
  value that crosses from Python ("not a member of declared union ai.Client | null").
- Every BAML event goes to the caller's `on_event` and to `llm.on_baml_event`. Token usage is
  printed and returned.

So a BAML function called through `call` ends with the parameters
`provider: string, model: string, max_tokens: int` and passes them to `harness_client`.
"""

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from harness.baml_reply import ReplyRecorder
from harness.llm import LLM, MAX_TOKENS

T = TypeVar("T")

SDK_MODULE = "baml_sdk"
REGENERATE = "BAML_VERSION=0.22.2 baml generate --directory eval"
PROVIDERS = {"anthropic", "openai", "xai"}


class BamlSdkMissing(RuntimeError):
    """The generated baml_sdk package, or a function in it, isn't there."""


class BamlProviderUnsupported(ValueError):
    """The harness LLM names no provider and model that harness_client can build."""


@dataclass
class BamlResult(Generic[T]):
    value: T
    # The reply BAML parsed, for checks the parser can't make (harness/baml_reply.py). None on mock runs.
    raw: str | None
    input_tokens: int = 0
    output_tokens: int = 0


def client_settings(llm: LLM) -> tuple[str, str]:
    """The provider and model `harness_client` builds a client from."""
    provider, model = getattr(llm, "provider", ""), getattr(llm, "model", "")
    if provider not in PROVIDERS:
        raise BamlProviderUnsupported(
            f"{type(llm).__name__} has provider {provider!r}; BAML calls need one of {sorted(PROVIDERS)}. "
            "A wrapper around a harness LLM has to keep its provider and model attributes."
        )
    if not model:
        raise BamlProviderUnsupported(f"{type(llm).__name__} has no model for provider {provider!r}")
    return provider, model


def load_sdk() -> Any:
    try:
        return importlib.import_module(SDK_MODULE)
    except ModuleNotFoundError as e:
        if e.name == SDK_MODULE:
            raise BamlSdkMissing(f"{SDK_MODULE} isn't generated; run {REGENERATE}") from e
        raise


def spec(llm: LLM, function: str, *args: Any) -> Any:
    """The unrun `function@spec(*args, provider, model, max_tokens)`, for .call, .parse or .build_request."""
    factory = getattr(load_sdk(), f"{function}_spec", None)
    if factory is None:
        raise BamlSdkMissing(f"{SDK_MODULE} has no {function}; regenerate it with {REGENERATE}")
    provider, model = client_settings(llm)
    return factory(*args, provider=provider, model=model, max_tokens=MAX_TOKENS)


def call(
    llm: LLM,
    function: str,
    *args: Any,
    mock: Callable[[], T],
    on_event: Callable[[Any], None] | None = None,
) -> BamlResult[T]:
    """Run `function(*args)` with the LLM's settings, or return `mock()` on a mock run."""
    if getattr(llm, "is_mock", False):
        return BamlResult(mock(), raw=None)
    meter = getattr(llm, "on_baml_event", None)

    def forward(event: Any) -> None:
        for listener in (on_event, meter):
            if listener is not None:
                listener(event)

    reply = ReplyRecorder(forward=forward)
    value = spec(llm, function, *args).call(on_event=reply)
    print(f"[baml] {function}: {reply.replies} reply(s), {reply.input_tokens:,} in / {reply.output_tokens:,} out tokens")
    return BamlResult(value, reply.text, reply.input_tokens, reply.output_tokens)
