"""The annotator prompt: shared instructions in a cached system prompt, one item per user message.

One item per call because scoring posts in batches moved this repo's earlier panel by 0.125 AUC
depending on order (research/eval.md, finding 1). The item message never shows engagement counts.
"""

import hashlib
import json

from harness.label.schema import SCHEMA, fields_for

PARENT_CHARS = 700
TEXT_CHARS = 2000


def system_prompt(schema: dict = SCHEMA) -> str:
    lines = [
        "You annotate posts and replies from X (Twitter) for a research dataset. Each message gives you one "
        "item: its text, who wrote it, when, and for a reply the post it answers. Another annotator labels the "
        "same items independently and the two sets are compared, so apply each definition literally and the "
        "same way every time.",
        "",
        "The items come from developer-tool companies, their staff, and the people who reply to them. You never "
        "see likes, views or other counts; judge only what is shown.",
        "",
        "Answer every field that applies to the item's kind. Fields:",
        "",
    ]
    for f in schema["fields"]:
        values = ", ".join(str(v) for v in f["values"])
        scope = "replies only" if f["applies_to"] == ["reply"] else "posts and replies"
        lines.append(f"{f['name']} ({scope}; one of: {values})")
        lines.append(f"{f['question']} {f['guide']}")
        lines.append("")
    example = {f["name"]: f["values"][0] for f in schema["fields"]}
    lines += [
        "Reply with one JSON object and nothing else, in this shape:",
        json.dumps({"labels": example, "rationale": "One sentence, at most 25 words, on the deciding detail."},
                   ensure_ascii=False),
        "Use the exact values listed above; numbers stay numbers.",
    ]
    reply_only = [f["name"] for f in schema["fields"] if f["applies_to"] == ["reply"]]
    if reply_only:
        lines[-1] += f" Leave out fields that do not apply to the item's kind ({', '.join(reply_only)} apply to replies only)."
    return "\n".join(lines)


def _clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def user_message(item: dict, parent: dict | None, schema: dict = SCHEMA) -> str:
    """`item` and `parent` are rows of data/labels/items.jsonl (parent may be None)."""
    followers = item.get("author_followers")
    who = f"@{item['author']}" + (f" ({followers:,} followers)" if isinstance(followers, int) else "")
    lines = [f"Kind: {item['kind']}", f"Author: {who}"]
    ctx = item.get("context") or {}
    if ctx.get("author_bio"):
        lines.append(f"Author bio: {_clip(ctx['author_bio'], 200)}")
    when = item["created_at"][:16].replace("T", " ") + " UTC"
    if item["kind"] == "reply" and parent is not None:
        minutes = ctx.get("minutes_after_parent")
        if minutes is not None:
            when += f", {minutes:,.0f} minutes after the post it answers"
    lines.append(f"Posted: {when}")
    media = ", ".join(ctx.get("media_types") or []) or "none"
    lines.append(f"Media: {media}. Links: {ctx.get('n_links', 0)}. Quotes another post: "
                 f"{'yes' if ctx.get('quoted_id') else 'no'}.")
    if item["kind"] == "reply":
        if parent is not None:
            pmedia = ", ".join((parent.get("context") or {}).get("media_types") or [])
            said = f"whose post (with {pmedia}) said:" if pmedia else "whose post said:"
            lines += [f"Replying to @{parent['author']}, {said}", '"""', _clip(parent["text"], PARENT_CHARS), '"""']
        else:
            lines.append("Replying to a post that is not available.")
    lines += ["Text:", '"""', _clip(item["text"], TEXT_CHARS) or "(no text)", '"""']
    names = ", ".join(f["name"] for f in fields_for(item["kind"], schema))
    lines.append(f"Fields to answer: {names}.")
    return "\n".join(lines)


_SAMPLE_PARENT = {"kind": "post", "author": "acme", "text": "We shipped v2.", "created_at": "2026-10-01T12:00:00+00:00"}
_SAMPLE_REPLY = {"kind": "reply", "author": "fan", "author_followers": 10, "text": "Nice", "created_at": "2026-10-01T12:30:00+00:00",
                 "context": {"author_bio": "bio", "minutes_after_parent": 30.0, "media_types": ["photo"], "n_links": 1, "quoted_id": None}}


def prompt_sha(schema: dict = SCHEMA) -> str:
    """Identifies the instructions an annotation was made under: the system prompt plus the item template,
    rendered on a fixed sample so any template change changes the hash."""
    rendered = system_prompt(schema) + "\n---\n" + user_message(_SAMPLE_REPLY, _SAMPLE_PARENT, schema)
    return hashlib.sha256(rendered.encode()).hexdigest()[:16]
