"""Record the model each LLM call asked for and the model the gateway says answered.

A gateway alias is not proof of the serving model, so every hot draft file lists both. The lane
builds its own Anthropic clients (the plan call on HARNESS_MODEL, the writer, the veto judge), and
none of them returns the response object, so the watcher wraps `anthropic`'s `Messages.create`
once per process and copies (requested, reported, seconds) into whichever recording is open.
The watcher runs one hot job at a time, so one open recording at a time is enough; calls made
while none is open, or from the OpenAI-compatible backends, are not recorded.
"""

import threading
import time
from contextlib import contextmanager

_lock = threading.Lock()
_current: list[dict] | None = None
_installed = False


def install() -> bool:
    """Wrap anthropic's Messages.create. Idempotent. False when the SDK isn't importable."""
    global _installed
    with _lock:
        if _installed:
            return True
        try:
            from anthropic.resources.messages import Messages
        except ImportError:
            return False
        original = Messages.create

        def create(self, *args, **kwargs):
            start = time.monotonic()
            resp = original(self, *args, **kwargs)
            with _lock:
                if _current is not None:
                    _current.append({"requested": kwargs.get("model"), "reported": getattr(resp, "model", None),
                                     "seconds": round(time.monotonic() - start, 2)})
            return resp

        Messages.create = create
        _installed = True
        return True


@contextmanager
def recording():
    """Collect every recorded call inside the block into the yielded list."""
    global _current
    calls: list[dict] = []
    with _lock:
        if _current is not None:
            raise RuntimeError("a model recording is already open; the watcher runs one hot job at a time")
        _current = calls
    try:
        yield calls
    finally:
        with _lock:
            _current = None
