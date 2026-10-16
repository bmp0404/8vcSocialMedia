"""Building blocks for action specs: text limits, grounding context, history checks, schema helpers."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from harness.models import Action, Narrative

MAX_WEIGHTED_CHARS = 280
URL_WEIGHT = 23  # X shortens every link to a t.co URL of this length

STATUS_URL = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/(\w+)/status/(\d+)")
HANDLE = re.compile(r"^\w+$")
MENTION = re.compile(r"(?<!\w)@(\w+)")
LINK = re.compile(r"https?://\S+")
THREAD_SPLIT = re.compile(r"^\s*---\s*$", re.MULTILINE)
TIMESTAMP = re.compile(r"^(?:(\d+):)?([0-5]?\d):([0-5]\d)$")

# Per-channel length limits. X posts are counted the way X counts them, per post in a thread.
POST_LIMITS = {"x": MAX_WEIGHTED_CHARS, "linkedin": 3000, "discord": 2000}
MESSAGE_LIMITS = {"x": 10000, "linkedin": 8000, "discord": 2000}


def weighted_length(text: str) -> int:
    """Character count the way X counts it: links are 23, most non-Latin characters and emoji count 2."""
    n = 0
    for chunk in LINK.split(text):
        for ch in chunk:
            cp = ord(ch)
            light = cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247
            n += 1 if light else 2
    return n + URL_WEIGHT * len(LINK.findall(text))


def thread_parts(text: str) -> list[str]:
    """A thread is several posts separated by a line containing only ---."""
    return [p.strip() for p in THREAD_SPLIT.split(text or "") if p.strip()]


def post_id(url: str | None) -> str | None:
    m = STATUS_URL.match(url or "")
    return m[2] if m else None


def seconds(ts: str) -> int | None:
    m = TIMESTAMP.match((ts or "").strip())
    return None if not m else int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])


def _norm(who: str | None) -> str:
    return (who or "").strip().lstrip("@").lower()


@dataclass
class Context:
    """Everything a draft is checked against: what the minds were shown, the CRM, and past actions."""

    urls: set[str] = field(default_factory=set)
    handles: set[str] = field(default_factory=set)
    corpus: str = ""  # lowercased narrative text, for names that appear in a conversation
    contacts: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # approved actions, each with `reviewed_at`
    swag: dict = field(default_factory=dict)
    media: list[dict] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def build(cls, narratives: list[Narrative], contacts=(), history=(), swag=None, media=()) -> "Context":
        ctx = cls(contacts=list(contacts), history=list(history), swag=swag or {}, media=list(media))
        texts = []
        for n in narratives:
            texts += [n.title, n.summary, n.angle]
            for url in n.signal_urls:
                ctx.urls.add(url)
                if m := STATUS_URL.match(url):
                    ctx.handles.add(m[1].lower())
        for t in texts:
            ctx.handles.update(h.lower() for h in MENTION.findall(t or ""))
        ctx.corpus = " ".join(t or "" for t in texts).lower()
        return ctx

    def contact(self, who: str | None) -> dict | None:
        key = _norm(who)
        if not key:
            return None
        for c in self.contacts:
            ids = [c.get("name"), c.get("x"), c.get("github"), c.get("email"), c.get("linkedin")]
            if key in {_norm(i) for i in ids if i}:
                return c
        return None

    def knows(self, who: str | None) -> bool:
        """True if the person is in the CRM or appears in the conversations the minds were shown."""
        key = _norm(who)
        return bool(key) and (key in self.handles or self.contact(who) is not None or key in self.corpus)

    def recent(self, types: set[str], key: str, value: str | None, days: int) -> list[dict]:
        """Approved actions of these types aimed at the same person/outlet within `days`."""
        want = _norm(value)
        if not want or days <= 0:
            return []
        cutoff = self.now - timedelta(days=days)
        out = []
        for h in self.history:
            got = h.get(key) if key in h else (h.get("details") or {}).get(key)
            if h.get("type") not in types or _norm(got) != want:
                continue
            try:
                when = datetime.fromisoformat(h["reviewed_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if when >= cutoff:
                out.append(h)
        return out


# --- checks shared by specs -------------------------------------------------------------------

Check = Callable[[Action, Context, list[str]], None]


def get(a: Action, name: str):
    return getattr(a, name) if hasattr(a, name) else a.details.get(name)


def require(*names: str) -> Check:
    def check(a: Action, ctx: Context, errors: list[str]) -> None:
        for n in names:
            v = get(a, n)
            if v is None or (isinstance(v, (str, list, dict)) and not v):
                errors.append(f"{n} is empty")
    return check


def text_fits(kind: str = "post") -> Check:
    def check(a: Action, ctx: Context, errors: list[str]) -> None:
        if not (a.text or "").strip():
            errors.append("text is empty")
            return
        if kind == "post" and a.channel == "x":
            parts = thread_parts(a.text)
            for i, part in enumerate(parts, 1):
                if (n := weighted_length(part)) > MAX_WEIGHTED_CHARS:
                    where = f"post {i}" if len(parts) > 1 else "text"
                    errors.append(f"{where} is {n}/{MAX_WEIGHTED_CHARS} characters")
            return
        limit = (POST_LIMITS if kind == "post" else MESSAGE_LIMITS).get(a.channel)
        if limit and len(a.text) > limit:
            errors.append(f"text is {len(a.text)}/{limit} characters for {a.channel}")
    return check


def grounded_url(name: str = "target_url", x_only: bool = False) -> Check:
    """The URL must be one the minds were shown (and an X post URL when the action happens on X)."""
    def check(a: Action, ctx: Context, errors: list[str]) -> None:
        url = get(a, name)
        if not url:
            errors.append(f"{name} is empty")
        elif (x_only or a.channel == "x") and not post_id(url):
            errors.append(f"{name} is not an X post URL: {url!r}")
        elif url not in ctx.urls:
            errors.append(f"{name} was not in the narratives: {url}")
    return check


def known_person(name: str = "target_person") -> Check:
    def check(a: Action, ctx: Context, errors: list[str]) -> None:
        who = get(a, name)
        if not who:
            errors.append(f"{name} is empty")
        elif not ctx.knows(who):
            errors.append(f"{name} {who!r} is not in the CRM or the narratives")
    return check


def x_handle(name: str = "target_handle") -> Check:
    def check(a: Action, ctx: Context, errors: list[str]) -> None:
        h = get(a, name)
        if not HANDLE.match(h or ""):
            errors.append(f"{name} is not a valid handle: {h!r}")
        elif h.lower() not in ctx.handles and not ctx.contact(h):
            errors.append(f"@{h} was not in the narratives or the CRM")
    return check


def grounded_list(name: str, minimum: int) -> Check:
    def check(a: Action, ctx: Context, errors: list[str]) -> None:
        urls = [u for u in get(a, name) or [] if u in ctx.urls]
        if len(urls) < minimum:
            errors.append(f"{name} needs at least {minimum} URLs from the narratives (got {len(urls)})")
    return check


# --- the spec ---------------------------------------------------------------------------------

# Fields that map onto Action attributes; any other tool argument lands in Action.details.
ACTION_FIELDS = {"text", "target_url", "target_handle", "target_person", "channel", "evidence", "timing",
                 "prerequisites", "intended_outcome", "dismiss_if", "missing_info", "narrative_id", "rationale"}

S = lambda d: {"type": "string", "description": d}
STRINGS = lambda d: {"type": "array", "items": {"type": "string"}, "description": d}
ENUM = lambda vals, d: {"type": "string", "enum": list(vals), "description": d}
INT = lambda d: {"type": "integer", "description": d}


def OBJECTS(props: dict, d: str) -> dict:
    return {"type": "array", "description": d, "items": {
        "type": "object", "properties": props, "required": list(props), "additionalProperties": False}}


PLAY_FIELDS = {
    "narrative_id": S("id of the narrative this builds on, or '' if it comes from the CRM or history"),
    "rationale": S("one sentence: why this, why this person or audience, why now"),
    "evidence": STRINGS("URLs or CRM facts that support this action"),
    "timing": S("when to do it, e.g. 'within 2 hours', 'Tuesday 9am PT', 'after v0.9 ships'"),
    "prerequisites": STRINGS("what must be true or ready first (assets, approvals, a shipped feature)"),
    "intended_outcome": S("the specific result this is for, e.g. 'author replies', 'booked call'"),
    "dismiss_if": S("condition under which to drop or pause this, e.g. 'thread goes negative'"),
    "missing_info": STRINGS("information the team must supply before this can go out; [] if none"),
}


@dataclass(frozen=True)
class ActionSpec:
    type: str
    tool: str
    title: str
    tier: int  # 1 = run it now, 2 = relationship work a person approves, 3 = later
    description: str
    fields: dict
    checks: tuple[Check, ...]
    channels: tuple[str, ...] = ("x",)
    audience: str = "public"  # public (panel scores it) | person (recipient sim) | internal (no scoring)
    feedback: tuple[str, ...] = ()  # reasons a reviewer can give, from the action's feedback column
    metrics: dict = field(default_factory=dict)  # outcome metric -> weight, for reward
    measure_days: int = 3
    cooldown_days: int = 0
    cooldown_key: str = "target_person"
    cooldown_types: tuple[str, ...] = ()  # defaults to this type
    outreach: bool = False  # respects contacts' do_not_contact
    play: bool = True  # gets the shared action-selection fields
    to_x: Callable[[Action], dict | None] | None = None  # posting-bot instruction on X; None = team does it
    normalize: Callable[[Action], None] = lambda a: None

    def tool_schema(self) -> dict:
        props = dict(self.fields)
        if len(self.channels) > 1:
            props["channel"] = ENUM(self.channels, "where this happens")
        props.update(PLAY_FIELDS if self.play else {"narrative_id": PLAY_FIELDS["narrative_id"],
                                                    "rationale": PLAY_FIELDS["rationale"]})
        return {
            "name": self.tool,
            "description": self.description,
            "input_schema": {"type": "object", "properties": props, "required": list(props),
                             "additionalProperties": False},
        }

    def executor(self, a: Action) -> str:
        if self.audience == "internal":
            return "none"
        return "x_bot" if a.channel == "x" and self.to_x and self.to_x(a) is not None else "team"

    def intent(self, a: Action) -> dict:
        ex = self.executor(a)
        if ex == "x_bot":
            return {"executor": ex, **self.to_x(a)}
        body = {k: v for k, v in {"op": self.type, "channel": a.channel, "to": a.target_person or a.target_handle,
                                  "target_url": a.target_url, "text": a.text}.items() if v}
        return {"executor": ex, **body, **a.details}
