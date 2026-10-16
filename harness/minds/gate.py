"""Slop gate: deterministic checks that reject a draft before any model or person sees it.

Two kinds of rule. Style rules catch the patterns from the `unslop` catalog that real X replies
almost never contain (em dashes, "not X but Y", AI vocabulary, assistant voice). Grounding rules
need the context the writer saw (the post, the thread and the facts block) and reject anything a
continuation could have invented: links, media placeholders, @handles, numbers and capitalized
names that appear nowhere in that context.

A pass means "nothing obviously wrong", not "reads human". The veto judge decides register.
`harness.minds.lab.gate_calibration` measures every rule on real replies and writes
action/experiments/gate_calibration.json.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from harness.minds.settings import GateLimits

KINDS = ("reply", "quote", "post", "mention")

EM_DASH = re.compile(r"—|\s–\s|\s--\s")

NOT_X_BUT_Y = [
    re.compile(r"\bnot (?:just|only|merely|simply)\b[^.!?]{0,80}\bbut\b", re.I),
    re.compile(r"\b(?:it'?s|this is|that'?s)\s+not\s+(?:about\s+)?[^.!?,;]{1,40}[,;.]\s*(?:it'?s|this is|that'?s)\b", re.I),
    re.compile(r"\b(?:isn'?t|aren'?t|wasn'?t)\s+just\b", re.I),
    re.compile(r"\bmore than just\b", re.I),
]

# Phrases that read as assistant voice or meta-commentary about the draft itself.
ASSISTANT_VOICE = [
    r"\bas an ai\b", r"\bhere'?s (?:a|my|the|your) (?:reply|post|draft|take|tweet)\b", r"^\s*(?:reply|draft|post|tweet)\s*:",
    r"\boption \d\b", r"\bi hope this helps\b", r"\blet me know\b", r"\bgreat question\b", r"^\s*(?:certainly|absolutely)[!,.]",
    r"\bit'?s (?:important|worth) (?:to note|noting|remembering)\b", r"\bin conclusion\b", r"\bremember,", r"\*\*",
    r"\bfeel free to\b", r"\bi'?d be happy to\b",
]

# The unslop vocabulary list and its sports-writing cousins. None of these fired on the 670 real
# replies in the calibration run.
AI_VOCAB = [
    r"\bdelve", r"\btestament\b", r"\btapestry\b", r"\bpivotal\b", r"\bshowcas", r"\bunderscor", r"\bvibrant\b",
    r"\bfoster", r"\bgarner", r"\bintricate\b", r"\binterplay\b", r"\bunwavering\b", r"\bembark", r"\bnavigat(?:e|ing) (?:the|this|her|his)\b",
    r"\bresilien", r"\bspeaks volumes\b", r"\bis undeniable\b", r"\bmake no mistake\b", r"\blet that sink in\b",
    r"\bjust getting started\b", r"\bthe future is bright\b", r"\bwhat a journey\b", r"\bhere'?s to\b", r"\bcan'?t wait to see what\b",
    r"\bnext chapter\b", r"\bstay tuned\b", r"\bthe (?:energy|vibes?) (?:is|are) (?:unmatched|immaculate|electric)\b",
    r"\belevat(?:e|es|ing) (?:the|her|his|their)\b", r"\bgame[- ]changer\b", r"\bin the (?:best|worst) way\b",
]

PLACEHOLDER = re.compile(
    r"\[(?:verify|insert|todo|tbd|link|name|stat|video|image|photo|gif|media|clip)\b[^\]]*\]|\{[a-z_]+\}|\bTODO\b|\bTBD\b", re.I)
URL = re.compile(r"https?://|\bwww\.|\bt\.co/|\bpic\.(?:twitter|x)\.com|\b[\w-]+\.(?:com|net|org|ly|co|io|gg|tv)/", re.I)
HANDLE = re.compile(r"(?<![\w@])@(\w{1,15})")
HASHTAG = re.compile(r"(?<![\w&])#\w+")
NUMBER = re.compile(r"\d+(?:[.,:/\-]\d+)*")
WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
# Capitalized words that aren't names. Sentence-initial words are skipped separately.
NOT_NAMES = {"i", "i'm", "i'll", "i've", "i'd", "ok", "okay", "lol", "lmao", "omg", "tbh", "idk"}


@dataclass
class Finding:
    rule: str
    detail: str
    start: int | None = None  # character span in the checked text, when the rule matched one
    end: int | None = None


@dataclass
class GateResult:
    passed: bool
    findings: list[Finding] = field(default_factory=list)

    def reasons(self) -> list[str]:
        return [f"{f.rule}: {f.detail}" for f in self.findings]


@dataclass
class _Ctx:
    kind: str
    limits: GateLimits
    context: str | None  # lowercased, or None when the caller has no context to ground against


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or cp in (0x2B50, 0x2B55, 0x2934, 0x2935, 0x3030, 0x303D, 0x3297, 0x3299)
    )


def distinct_emoji(text: str) -> set[str]:
    """Distinct emoji, ignoring skin tones and joiners; a pair of regional indicators is one flag."""
    out: set[str] = set()
    chars = list(text)
    i = 0
    while i < len(chars):
        cp = ord(chars[i])
        if 0x1F1E6 <= cp <= 0x1F1FF:
            pair = chars[i] + (chars[i + 1] if i + 1 < len(chars) and 0x1F1E6 <= ord(chars[i + 1]) <= 0x1F1FF else "")
            out.add(pair)
            i += len(pair)
            continue
        if _is_emoji(chars[i]) and not (0x1F3FB <= cp <= 0x1F3FF):
            out.add(chars[i])
        i += 1
    return out


def capitalized_names(text: str) -> list[tuple[str, int, int]]:
    """Capitalized words that don't start a sentence or a quote and aren't all-caps, a handle or a hashtag."""
    out = []
    for m in WORD.finditer(text):
        w = m.group(0)
        before = text[: m.start()].rstrip(" \t")
        initial = not re.search(r"[A-Za-z0-9]", before) or before[-1] in ".!?\n:\"'\u201c\u2018("
        if initial or before.endswith(("@", "#")) or not w[0].isupper() or w.isupper() or w.lower() in NOT_NAMES:
            continue
        out.append((w, m.start(), m.end()))
    return out


# ---- style rules


def _rule_empty(text: str, c: _Ctx) -> list[str]:
    return [("empty text", None, None)] if not text.strip() else []


def _rule_em_dash(text: str, c: _Ctx) -> list[str]:
    return [(f"dash at char {m.start()}", m.start(), m.end()) for m in EM_DASH.finditer(text)]


def _rule_not_x_but_y(text: str, c: _Ctx) -> list[str]:
    return [(repr(m.group(0)), m.start(), m.end()) for p in NOT_X_BUT_Y for m in p.finditer(text)]


def _rule_assistant_voice(text: str, c: _Ctx) -> list[str]:
    return [(repr(m.group(0)), m.start(), m.end()) for p in ASSISTANT_VOICE for m in re.finditer(p, text, re.I | re.M)]


def _rule_ai_vocab(text: str, c: _Ctx) -> list[str]:
    return [(repr(m.group(0)), m.start(), m.end()) for p in AI_VOCAB for m in re.finditer(p, text, re.I)]


def _rule_emoji(text: str, c: _Ctx) -> list[str]:
    found, cap = distinct_emoji(text), c.limits.max_distinct_emoji
    return [(f"{len(found)} distinct emoji (max {cap})", None, None)] if len(found) > cap else []


def _rule_hashtags(text: str, c: _Ctx) -> list[str]:
    tags, cap = list(HASHTAG.finditer(text)), c.limits.max_hashtags.get(c.kind, 1)
    if len(tags) <= cap:
        return []
    return [(f"{len(tags)} hashtags (max {cap}): {' '.join(t.group(0) for t in tags)}", tags[cap].start(), tags[-1].end())]


def _rule_length(text: str, c: _Ctx) -> list[str]:
    cap = c.limits.max_chars.get(c.kind, 280)
    return [(f"{len(text)} chars (max {cap} for {c.kind})", cap, len(text))] if len(text) > cap else []


def _rule_reply_sentences(text: str, c: _Ctx) -> list[str]:
    cap = c.limits.max_reply_sentences
    if c.kind != "reply" or cap is None:
        return []
    n = len(SENTENCE_END.findall(text.strip())) or 1
    return [(f"{n} sentences in a reply (max {cap})", None, None)] if n > cap else []


def _rule_placeholder(text: str, c: _Ctx) -> list[str]:
    return [(f"unresolved placeholder {m.group(0)!r}", m.start(), m.end()) for m in PLACEHOLDER.finditer(text)]


def _rule_url(text: str, c: _Ctx) -> list[str]:
    return [(f"link {m.group(0)!r}", m.start(), m.end()) for m in URL.finditer(text)]


def _rule_wrapped_in_quotes(text: str, c: _Ctx) -> list[str]:
    t = text.strip()
    quotes = {'"': '"', "“": "”", "'": "'"}
    return [("whole draft wrapped in quotes", None, None)] if len(t) > 2 and t[0] in quotes and t.endswith(quotes[t[0]]) else []


# ---- grounding rules (skipped when the caller passes no context)


def _rule_unknown_handle(text: str, c: _Ctx) -> list[str]:
    if c.context is None:
        return []
    return [(f"@{m.group(1)} isn't in the post or thread", m.start(), m.end())
            for m in HANDLE.finditer(text) if f"@{m.group(1).lower()}" not in c.context]


def _rule_ungrounded_number(text: str, c: _Ctx) -> list[str]:
    if c.context is None:
        return []
    return [(f"number {m.group(0)!r} isn't in the context", m.start(), m.end())
            for m in NUMBER.finditer(text) if m.group(0) not in c.context]


def _rule_ungrounded_name(text: str, c: _Ctx) -> list[str]:
    if c.context is None:
        return []
    return [(f"name {w!r} isn't in the context", a, b) for w, a, b in capitalized_names(text) if w.lower() not in c.context]


RULES = {
    "empty": _rule_empty,
    "em_dash": _rule_em_dash,
    "not_x_but_y": _rule_not_x_but_y,
    "assistant_voice": _rule_assistant_voice,
    "ai_vocab": _rule_ai_vocab,
    "emoji": _rule_emoji,
    "hashtags": _rule_hashtags,
    "length": _rule_length,
    "reply_sentences": _rule_reply_sentences,
    "placeholder": _rule_placeholder,
    "url": _rule_url,
    "wrapped_in_quotes": _rule_wrapped_in_quotes,
    "unknown_handle": _rule_unknown_handle,
    "ungrounded_number": _rule_ungrounded_number,
    "ungrounded_name": _rule_ungrounded_name,
}
GROUNDING_RULES = ("unknown_handle", "ungrounded_number", "ungrounded_name")


def check(text: str, kind: str = "reply", *, context: str | None = None, limits: GateLimits | None = None,
          rules: dict | None = None) -> GateResult:
    """Run every rule on `text`. `kind` is the action type. `context` is everything the writer
    saw (post, thread, facts); without it the grounding rules are skipped."""
    if kind not in KINDS:
        raise ValueError(f"gate.check: unknown kind {kind!r}; expected one of {list(KINDS)}")
    text = unicodedata.normalize("NFC", text)
    ctx = _Ctx(kind, limits or GateLimits(), unicodedata.normalize("NFC", context).lower() if context is not None else None)
    findings = [Finding(name, d, a, b) for name, rule in (rules or RULES).items() for d, a, b in rule(text, ctx)]
    return GateResult(passed=not findings, findings=findings)
