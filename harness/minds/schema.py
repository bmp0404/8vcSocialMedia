"""Typed tool calls for the decide stage, one schema per action, split by lane.

A model answers each opportunity with exactly one JSON tool call. `parse_call` checks it against
the lane's schema and the opportunity it was offered, and raises `ActionSchemaError` with the
field and the reason when anything is off. Nothing is repaired or guessed: a call that names a
target it wasn't offered, or carries a field the tool doesn't have, is rejected.

The lanes need different inputs. The reply lane answers one live X post (a reply target from
Observe), so its tools all point at that post or its author. The post lane starts from a story,
so its tools caption or quote a real source, follow an account the story cites, or ask a person for the take.
"""

from dataclasses import dataclass, field

REPLY_LANE = "reply"
POST_LANE = "post"

# Why we didn't act. Each code names the stage at fault, so eval can blame the right one.
ABSTAIN_REASONS = {
    "no_target": "observe offered no live X post to answer",
    "stale_target": "the post is too old for a reply to be seen",
    "no_angle": "nothing specific to add to this post",
    "gate_rejected": "every draft failed the slop gate",
    "judge_rejected": "the best draft didn't clear the judge's bar",
    "budget_exhausted": "the run or daily action budget is spent",
    "unverifiable": "the draft needs a fact we can't check",
    "off_mission": "the story doesn't fit the mission",
    "mute_risk": "answering could get the account muted or reported",
    "no_source": "nothing real to quote or caption",
    "no_linkable_source": "the only sources are opaque redirect links",
}

# field name -> (type, required)
_TEXT = {"text": (str, True), "rationale": (str, False)}
TOOLS = {
    REPLY_LANE: {
        "reply": {"target_url": (str, True), **_TEXT},
        "quote": {"target_url": (str, True), **_TEXT},
        "follow": {"target_handle": (str, True), "rationale": (str, False)},
        "abstain": {"reason": (str, True), "detail": (str, False)},
    },
    POST_LANE: {
        "post": {**_TEXT},
        "quote": {"target_url": (str, True), **_TEXT},
        "mention": {"target_handle": (str, True), **_TEXT},
        "follow": {"target_handle": (str, True), "rationale": (str, False)},
        "ask_human": {"question": (str, True), "format": (str, False), "rationale": (str, False)},
        "abstain": {"reason": (str, True), "detail": (str, False)},
    },
}


class ActionSchemaError(ValueError):
    """A tool call that doesn't match its schema. The message names the tool, field and reason."""


@dataclass
class ToolCall:
    tool: str
    lane: str
    args: dict = field(default_factory=dict)

    @property
    def ships(self) -> bool:
        """True for calls that become an X action; abstain and ask_human don't."""
        return self.tool not in ("abstain", "ask_human")


@dataclass
class Offer:
    """What the model was allowed to point at for one opportunity."""

    target_urls: frozenset[str] = frozenset()
    target_handles: frozenset[str] = frozenset()


def json_schema(lane: str) -> dict:
    """JSON Schema for the lane's tool calls, for prompts and for other workers to read."""
    if lane not in TOOLS:
        raise ActionSchemaError(f"unknown lane {lane!r}; expected one of {sorted(TOOLS)}")
    variants = []
    for tool, fields in TOOLS[lane].items():
        props = {"tool": {"const": tool}}
        props |= {name: {"type": "string"} for name in fields}
        if tool == "abstain":
            props["reason"] = {"enum": sorted(ABSTAIN_REASONS)}
        variants.append({
            "type": "object",
            "properties": props,
            "required": ["tool"] + [n for n, (_, req) in fields.items() if req],
            "additionalProperties": False,
        })
    return {"oneOf": variants}


def _norm_handle(h: str) -> str:
    return h.strip().lstrip("@").lower()


def parse_call(obj: object, lane: str, offer: Offer | None = None) -> ToolCall:
    """Validate one tool call against `lane` and `offer`. Raises ActionSchemaError on any mismatch."""
    if lane not in TOOLS:
        raise ActionSchemaError(f"unknown lane {lane!r}; expected one of {sorted(TOOLS)}")
    if not isinstance(obj, dict):
        raise ActionSchemaError(f"a tool call must be a JSON object, got {type(obj).__name__}")
    tool = obj.get("tool")
    if not isinstance(tool, str):
        raise ActionSchemaError(f"missing string field 'tool'; got keys {sorted(obj)}")
    spec = TOOLS[lane].get(tool)
    if spec is None:
        raise ActionSchemaError(f"tool {tool!r} is not allowed in the {lane} lane; allowed: {sorted(TOOLS[lane])}")

    extra = set(obj) - set(spec) - {"tool"}
    if extra:
        raise ActionSchemaError(f"{tool}: unexpected field(s) {sorted(extra)}; allowed: {sorted(spec)}")
    args = {}
    for name, (typ, required) in spec.items():
        if name not in obj or obj[name] is None:
            if required:
                raise ActionSchemaError(f"{tool}: missing required field {name!r}")
            continue
        value = obj[name]
        if not isinstance(value, typ):
            raise ActionSchemaError(f"{tool}.{name} must be {typ.__name__}, got {type(value).__name__}")
        if required and not value.strip():
            raise ActionSchemaError(f"{tool}.{name} is empty")
        args[name] = value.strip()

    if tool == "abstain" and args["reason"] not in ABSTAIN_REASONS:
        raise ActionSchemaError(f"abstain.reason {args['reason']!r} is not one of {sorted(ABSTAIN_REASONS)}")
    offer = offer or Offer()
    if "target_url" in args and args["target_url"] not in offer.target_urls:
        offered = sorted(offer.target_urls) or "none"
        raise ActionSchemaError(f"{tool}.target_url {args['target_url']!r} was not offered; offered: {offered}")
    if "target_handle" in args:
        handle = _norm_handle(args["target_handle"])
        if handle not in {_norm_handle(h) for h in offer.target_handles}:
            offered = sorted(offer.target_handles) or "none"
            raise ActionSchemaError(f"{tool}.target_handle {args['target_handle']!r} was not offered; offered: {offered}")
        args["target_handle"] = handle
        if tool == "mention" and f"@{handle}" not in args["text"].lower():
            raise ActionSchemaError(f"mention.text must contain @{handle}")
    return ToolCall(tool=tool, lane=lane, args=args)


def parse_plan(obj: object, offer: Offer) -> dict[str, dict]:
    """Validate the reply lane's planning call: {"plans": [{"target_url", "tool": "reply"|"abstain",
    "intent"?, "reason"?, "detail"?}]}. Returns {target_url: plan}. The plan picks targets; it writes no text."""
    if not isinstance(obj, dict) or not isinstance(obj.get("plans"), list):
        raise ActionSchemaError(f"plan must be an object with a 'plans' list, got {type(obj).__name__}")
    out = {}
    for i, p in enumerate(obj["plans"]):
        if not isinstance(p, dict):
            raise ActionSchemaError(f"plans[{i}] must be an object, got {type(p).__name__}")
        extra = set(p) - {"target_url", "tool", "intent", "reason", "detail"}
        if extra:
            raise ActionSchemaError(f"plans[{i}]: unexpected field(s) {sorted(extra)}")
        url, tool = p.get("target_url"), p.get("tool")
        if url not in offer.target_urls:
            raise ActionSchemaError(f"plans[{i}].target_url {url!r} was not offered; offered: {sorted(offer.target_urls) or 'none'}")
        if tool not in ("reply", "abstain"):
            raise ActionSchemaError(f"plans[{i}].tool must be 'reply' or 'abstain', got {tool!r}")
        if tool == "abstain" and p.get("reason") not in ABSTAIN_REASONS:
            raise ActionSchemaError(f"plans[{i}].reason {p.get('reason')!r} is not one of {sorted(ABSTAIN_REASONS)}")
        out[url] = {k: v for k, v in p.items() if isinstance(v, str)}
    return out
