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


_FENCE = re.compile(r"```[A-Za-z]*[ \t]*\n?(.*?)(?:```|\Z)", re.S)


def _array_key(key: str) -> re.Pattern:
    """`key` opening an array, with the key quoted or bare (SAP repairs unquoted keys)."""
    return re.compile(r'(?:"' + re.escape(key) + r'"|(?<![\w"])' + re.escape(key) + r")\s*:\s*\[")


def reply_json(text: str) -> str:
    """The part of a reply SAP parses: the first fenced block when there is one, else the whole text.

    Probed on 0.22.2 with OrientSignals: a bare object before a fenced one loses to the fenced one,
    and of two fenced blocks the first wins. A fence cut off before it closes runs to the end.
    """
    match = _FENCE.search(text)
    return match.group(1) if match else text


def _scan(text: str, start: int):
    """Yield (index, char, depth) for characters outside strings, from `start`."""
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        yield i, ch, depth
        if ch in _OPENERS:
            depth += 1
        elif ch in "]}":
            depth = max(depth - 1, 0)


def _object_start(text: str, key: str) -> int | None:
    """Where the object holding the first `key` array starts: the outermost brace still open there.

    Braces or brackets in prose before the JSON, such as "rubric [v1]" or "{verdict}", close before
    the key and don't count. Falls back to the first brace when the key never appears.
    """
    match = _array_key(key).search(text)
    if match is None:
        brace = text.find("{")
        return brace if brace >= 0 else None
    stack = []
    for i, ch, _ in _scan(text, 0):
        if i >= match.start():
            break
        if ch in _OPENERS:
            stack.append((ch, i))
        elif ch in "]}" and stack:
            stack.pop()
    braces = [i for ch, i in stack if ch == "{"]
    return braces[0] if braces else None


def json_closes(text: str, key: str) -> bool:
    """Whether the JSON object holding the `key` array, in the block SAP parses, closes.

    Every bracket and string it opens has to close. Unquoted keys and trailing commas, which SAP
    repairs on purpose, don't matter here. A reply that stops partway does: SAP would still parse it.
    """
    span = reply_json(text)
    start = _object_start(span, key)
    if start is None:
        return False
    stack = []
    for _, ch, _ in _scan(span, start):
        if ch in _OPENERS:
            stack.append(_OPENERS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return True
    return False


def raw_array_length(text: str, key: str) -> int | None:
    """How many elements the reply's `"key": [...]` array holds, counting a partial one.

    Reads the block SAP parses, inside the object that holds the key. Counts element starts, so a
    malformed element or one cut off at the end still counts. That is the number to compare with
    the parsed length. None when the reply has no such array.
    """
    span = reply_json(text)
    start = _object_start(span, key) or 0
    match = _array_key(key).search(span, start)
    if match is None:
        return None
    count, expecting = 0, True
    for _, ch, depth in _scan(span, match.end()):
        if ch.isspace():
            continue
        if depth == 0:
            if ch == "]":
                break
            if ch == ",":
                expecting = True
                continue
            if expecting and ch not in "]}":
                count += 1
                expecting = False
        if ch in "]}" and depth == 1:
            expecting = True  # an element just closed; SAP keeps the next one even without a comma
    return count


def fractional_values(text: str, key: str) -> list[float]:
    """Every non-integer number the reply gave for `key`, which SAP rounds into an int field."""
    values = [float(v) for v in re.findall(r'"' + re.escape(key) + r'"\s*:\s*(-?\d+\.\d+)', text)]
    return [v for v in values if not v.is_integer()]
