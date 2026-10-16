"""Decide what the LLM object can do from what it exposes, not from its class.

The lanes need three answers: is this a mock run, is there an Anthropic messages client to send a
prefill through, and can a different model be reached on the same gateway. Checking the class
broke as soon as a tracing wrapper replaced it (continuation fell back to the styled prompt, the
veto ran on the default model, and mock runs called fxtwitter). So these functions look at
attributes, which any wrapper that forwards attributes passes through. When a check falls back to a
weaker route, it says so once per process, loudly.
"""

from harness.llm import LLM, AnthropicLLM, MockLLM

_warned: set[str] = set()


def warn(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        print(f"[decide] WARNING: {message}")


def _unwrapped(llm: LLM) -> LLM:
    """Follow `inner` or `__wrapped__` through wrappers, a few levels at most."""
    seen = 0
    while seen < 5:
        nxt = getattr(llm, "inner", None) or getattr(llm, "__wrapped__", None)
        if nxt is None or nxt is llm:
            return llm
        llm, seen = nxt, seen + 1
    return llm


def is_mock(llm: LLM) -> bool:
    """True for the offline backend, wrapped or not. Mock runs never touch the network or the budget."""
    return getattr(llm, "is_mock", False) is True or isinstance(_unwrapped(llm), MockLLM)


def messages_client(llm: LLM):
    """The Anthropic SDK client behind `llm`, or None. Callers pass the model on every request."""
    client = getattr(llm, "client", None)
    return client if callable(getattr(getattr(client, "messages", None), "create", None)) else None


def on_model(llm: LLM, model: str, purpose: str) -> LLM:
    """An LLM bound to `model` on the same gateway, for `purpose`. Without an Anthropic client this
    returns `llm` unchanged and warns, because the run is now on a model nobody chose for it."""
    if is_mock(llm):
        return llm
    if messages_client(llm) is not None:
        return AnthropicLLM(model)
    warn(f"{purpose} wants {model}, but {type(llm).__name__} exposes no Anthropic messages client; "
         "using the run's own model instead")
    return llm
