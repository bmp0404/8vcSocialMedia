"""The action space as a tool set.

Each action type is one `ActionSpec` (see catalog.py): the tool the minds call to propose it, the checks
a draft must pass, who executes it (the X posting bot or the team), the reasons a reviewer can give,
and the metrics that count as success. Add an action type by adding a spec to catalog.SPECS.
"""

from harness.actions.catalog import SPECS
from harness.actions.core import (  # noqa: F401 - re-exported
    ACTION_FIELDS,
    MAX_WEIGHTED_CHARS,
    STATUS_URL,
    ActionSpec,
    Context,
    post_id,
    text_fits,
    thread_parts,
    weighted_length,
)
from harness.models import ACTION_TYPES, Action

BY_TYPE = {s.type: s for s in SPECS}
BY_TOOL = {s.tool: s for s in SPECS}
X_TYPES = sorted(ACTION_TYPES)  # the plain X primitives the posting bot executes


def is_public(a: Action) -> bool:
    """Public actions are judged on spread (panel, tournament, hooks); outreach and internal ones aren't."""
    return BY_TYPE[a.type].audience == "public"


def tool_schemas(types: list[str] | None = None) -> list[dict]:
    return [s.tool_schema() for s in SPECS if types is None or s.type in types]


def from_tool_call(tool: str, args: dict, mind: str) -> Action:
    spec = BY_TOOL.get(tool)
    if not spec:
        raise KeyError(f"unknown tool {tool!r}")
    known = {k: v for k, v in args.items() if k in ACTION_FIELDS}
    a = Action(
        type=spec.type,
        text=known.pop("text", "") or "",
        mind=mind,
        narrative_id=known.pop("narrative_id", "") or "",
        channel=known.pop("channel", None) or spec.channels[0],
        measure_days=spec.measure_days,
        details={k: v for k, v in args.items() if k not in ACTION_FIELDS},
        **{k: v for k, v in known.items() if v not in (None, "")},
    )
    spec.normalize(a)
    return a


def validate(a: Action, ctx: Context) -> list[str]:
    spec = BY_TYPE[a.type]
    errors: list[str] = []
    if a.channel not in spec.channels:
        errors.append(f"channel {a.channel!r} not allowed for {a.type} (use {', '.join(spec.channels)})")
    for check in spec.checks:
        check(a, ctx, errors)
    who = a.target_person or a.target_handle
    if spec.outreach and (c := ctx.contact(who)) and c.get("do_not_contact"):
        errors.append(f"{who} is marked do_not_contact")
    key_value = getattr(a, spec.cooldown_key, None) or a.details.get(spec.cooldown_key)
    types = set(spec.cooldown_types or (spec.type,))
    if spec.cooldown_days and (hits := ctx.recent(types, spec.cooldown_key, key_value, spec.cooldown_days)):
        errors.append(f"already did {hits[0]['type']} for {key_value} within {spec.cooldown_days} days")
    return errors


def _action(a: dict) -> Action:
    return Action(**{k: a[k] for k in Action.__dataclass_fields__ if k in a})


def text_errors(a: dict) -> list[str]:
    """Length checks alone, for a human edit of an action that already passed validation."""
    spec = BY_TYPE[a["type"]]
    if "text" not in spec.fields:
        return []
    errors: list[str] = []
    text_fits("message" if spec.audience == "person" else "post")(_action(a), Context(), errors)
    return errors


def intent(a: dict) -> dict:
    """The instruction for whoever executes an exported action dict (posting bot or team)."""
    action = _action(a)
    return BY_TYPE[action.type].intent(action)


def handoff(a: dict) -> dict:
    """An approved action as exported: the action, its instruction, and how it will be measured."""
    spec = BY_TYPE[a["type"]]
    return {**a, "intent": intent(a), "measure": {"window_days": a.get("measure_days") or spec.measure_days,
                                                  "metrics": list(spec.metrics)}}


def mock_args(spec: ActionSpec, ctx: Context, label: str, narrative_id: str = "") -> dict:
    """Plausible arguments for a tool, built from its schema and the context, for the offline backend."""
    urls = sorted(ctx.urls, key=lambda u: (not STATUS_URL.match(u), u))  # X posts first
    handles = sorted(ctx.handles)
    people = [c.get("x") or c.get("github") or c.get("email") or c["name"] for c in ctx.contacts
              if not c.get("do_not_contact")] or handles
    by_role = {c.get("role"): c.get("x") or c.get("email") or c["name"] for c in ctx.contacts}
    named = {
        "text": f"[{label}] mock {spec.tool}",
        "target_url": urls[0] if urls else "",
        "target_handle": handles[0] if handles else "",
        "target_person": by_role.get({"pitch_journalist": "journalist", "pitch_podcast": "podcast_host",
                                      "request_warm_intro": "prospect"}.get(spec.type)) or (people[0] if people else ""),
        "connector": by_role.get("connector", ""),
        "amplifier": handles[0] if handles else "",
        "source_url": ctx.media[0]["url"] if ctx.media else "",
        "item": ctx.swag.get("items", [{}])[0].get("item", ""),
        "narrative_id": narrative_id,
        "start": "01:10", "end": "01:55",
    }

    def fill(name: str, schema: dict):
        if name in named:
            return named[name]
        if "enum" in schema:
            return schema["enum"][0]
        t = schema.get("type")
        if t == "integer":
            return 24 if "hours" in name else 6
        if t == "boolean":
            return False
        if t == "array":
            item = schema["items"]
            if item.get("type") == "object":
                return [{k: (people[i % len(people)] if k == "person" and people else fill(k, v))
                         for k, v in item["properties"].items()} for i in range(2)]
            if name in ("evidence", "asked_in"):
                return urls[:2]
            return [] if name in ("missing_info", "prerequisites") else [f"mock {name}"]
        return f"mock {name}"

    return {k: fill(k, v) for k, v in spec.tool_schema()["input_schema"]["properties"].items()}


def catalog() -> list[dict]:
    """What the review dashboard needs to know about each action type."""
    return [{"type": s.type, "title": s.title, "tier": s.tier, "channels": list(s.channels),
             "audience": s.audience, "feedback": list(s.feedback), "has_text": "text" in s.fields}
            for s in SPECS]
