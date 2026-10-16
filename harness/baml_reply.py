"""The raw text a BAML function's model sent, for the checks BAML's parser can't make.

BAML parses replies with schema-aligned parsing (SAP), which repairs format noise but also hides
damage. Probed on 0.22.2 (eval/baml_src/tests.baml):

- an array element that fails to parse is dropped, and a missing array becomes [];
- a reply cut off mid-string parses: the string is closed where it stopped and later arrays are [];
- a fraction in an int or literal field is rounded (4.6 becomes 5).

`ReplyRecorder` keeps the reply as an `on_event` listener, and the functions below read it, so a
caller can compare the raw reply with the parsed value and fail on a mismatch.
"""

import re
from typing import Any, Callable

_OPENERS = {"{": "}", "[": "]"}


class ReplyRecorder:
    """An `on_event` listener that keeps the last assistant reply's text and sums token usage.

    The last reply is the one BAML parsed: when a reply fails to parse, BAML asks again once and
    parses the second. A listener that raises can't fail a BAML run (`ai.events.guard` swallows the
    error), so this one does nothing that can raise beyond reading the event.
    """

    def __init__(self, forward: Callable[[Any], None] | None = None):
        self.text: str | None = None
        self.replies = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.forward = forward

    def __call__(self, event: Any) -> None:
        # Imported here so the text helpers below load without BAML: mock runs must never import it.
        from baml_sdk.ai import content, events

        if isinstance(event, events.AssistantMessage):
            self.replies += 1
            self.text = "".join(b.text for b in event.content if isinstance(b, content.Text))
        elif isinstance(event, events.Usage):
            self.input_tokens += event.input_tokens
            self.output_tokens += event.output_tokens
        if self.forward is not None:
            self.forward(event)


def _first_value(text: str) -> int | None:
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    return min(starts) if starts else None


def json_closes(text: str) -> bool:
    """Whether the first JSON value in `text` closes: every bracket and string it opens is closed.

    Unquoted keys and trailing commas, which SAP repairs on purpose, don't matter here. A reply
    that stops partway does: SAP would still parse it.
    """
    start = _first_value(text)
    if start is None:
        return False
    stack, in_string, escaped = [], False, False
    for ch in text[start:]:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in _OPENERS:
            stack.append(_OPENERS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return True
    return False


def raw_array_length(text: str, key: str) -> int | None:
    """How many elements the reply's first `"key": [...]` array holds, counting a partial one.

    Counts element starts, so a malformed element or one cut off at the end still counts. That is
    the number to compare with the parsed length. None when the reply has no such array.
    """
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', text)
    if match is None:
        return None
    count, depth, in_string, escaped, expecting = 0, 0, False, False, True
    for ch in text[match.end():]:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch.isspace():
            continue
        if depth == 0:
            if ch == "]":
                break
            if ch == ",":
                expecting = True
                continue
            if expecting:
                count += 1
                expecting = False
        if ch == '"':
            in_string = True
        elif ch in _OPENERS:
            depth += 1
        elif ch in "]}":
            depth -= 1
    return count


def fractional_values(text: str, key: str) -> list[float]:
    """Every non-integer number the reply gave for `key`, which SAP rounds into an int field."""
    values = [float(v) for v in re.findall(r'"' + re.escape(key) + r'"\s*:\s*(-?\d+\.\d+)', text)]
    return [v for v in values if not v.is_integer()]
