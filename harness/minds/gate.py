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

from harness.facts.ground import FactIndex, anchor_words, grounded_numbers, unit_after
from harness.learning.kinds import Fact
from harness.minds.settings import GateLimits
from harness.xtext import MAX_WEIGHTED_CHARS, overflow_index, weighted_length

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

# X's ranker removes engagement bait from non-followers' feeds (SpamHighRecall, per the context
# research report on xai-org/x-algorithm).
ENGAGEMENT_BAIT = re.compile(
    r"\b(?:like|rt|retweet|repost|share)(?: this)? if\b|\bwho (?:agrees|else)\b|\btag (?:someone|a friend|your)\b"
    r"|\b(?:drop|comment) (?:a|your|below)\b|\bfollow (?:me )?for\b|\bsmash (?:that|the) like\b", re.I)
PLACEHOLDER = re.compile(r"\[(?:insert|todo|tbd|link|name|stat|video|image|photo|gif|media|clip)\b[^\]]*\]|\{[a-z_]+\}", re.I)
# Capitals only: under re.I it flagged 4 real dev-tool replies, 3 of them for the Spanish word "todo".
TODO = re.compile(r"\bTODO\b|\bTBD\b")
# The harness posts text only, so a line that describes media or acts out a stage direction is a draft talking about
# a post instead of being one. The watcher's hot path shipped "(image of Caitlin Clark in defensive stance)" on
# 2026-10-16. These catch media and stage words leading a bracket of any style, a starred media placeholder, and the
# bare forms "image of ...", "video: ...", "insert ... here" and a draft that is only "screenshot". An aside people
# bracket themselves, such as "(not kidding)" or "(and Biome's)", leads with neither, so it passes;
# action/experiments/placeholder_gate.json counts what these patterns reject in real replies.
MEDIA_WORDS = (r"(?:image|img|photo|pic|picture|gif|video|vid|clip|meme|screenshot|screen\s?(?:shot|grab|cap)|media|"
               r"attachment|emoji|sticker|link|url|audio|sound\s?effect|sfx)s?")
STAGE_WORDS = (r"(?:laughs?|laughing|laughter|sighs?|sighing|gasps?|gasping|cries|crying|sobs?|sobbing|screams?|screaming|"
               r"applause|applauds|claps?|clapping|slow\s+clap|shrugs?|shrugging|eye\s?rolls?|rolls\s+eyes|facepalms?|"
               r"dramatic\s+pause)")
_LEAD = (r"(?:(?:a|an|the|insert|add|attach|attached|cue|see|with|posts?|shows?|crowd|audience|fans|everyone|"
         r"nervous|awkward|evil|loud)[:\s]+)*")
_OPEN, _CLOSE = r"[\(\[\{<（【]", r"[\)\]\}>）】]"
MEDIA_PLACEHOLDERS = (
    re.compile(rf"{_OPEN}\s*{_LEAD}(?:{MEDIA_WORDS}|{STAGE_WORDS})\b[^\)\]\}}>）】\n]{{0,120}}{_CLOSE}", re.I),
    re.compile(rf"\*\s*{_LEAD}{MEDIA_WORDS}\b[^*\n]{{0,80}}\*", re.I),
    re.compile(r"^\s*(?:an?\s+)?(?:image|gif|video|photo|pic|picture|meme|clip|screenshot|screen\s?shot)s?\s+of\b",
               re.I | re.M),
    re.compile(r"^\s*(?:image|img|video|gif|photo|pic|clip|screenshot|meme|media|audio)s?\s*:", re.I | re.M),
    re.compile(rf"\binsert\s+(?:\w+\s+){{0,2}}?(?:{MEDIA_WORDS}|name|stat|number|player|team|joke|quote|caption|here)\b",
               re.I),
    re.compile(r"^\s*screen\s?shots?\s*[.!]*\s*$", re.I | re.M),
)
# CLAUDE.md: a fact the model can't ground is written as [VERIFY: ...]. The marker passes the gate so a
# person sees the draft and checks the fact or cuts it; review and the hand-off refuse it unresolved.
VERIFY = re.compile(r"\[VERIFY\b[^\]]*\]", re.I)
URL = re.compile(r"https?://|\bwww\.|\bt\.co/|\bpic\.(?:twitter|x)\.com|\b[\w-]+\.(?:com|net|org|ly|co|io|gg|tv)/", re.I)
HANDLE = re.compile(r"(?<![\w@])@(\w{1,15})")
HASHTAG = re.compile(r"(?<![\w&])#\w+")
NUMBER = re.compile(r"(?<![\w@#])\d+(?:[.,:/\-]\d+)*")  # not digits inside a handle or word (CC22, @kidd33)
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
    post: str | None = None  # lowercased text of the post being answered, when the caller has it
    facts: FactIndex | None = None  # live facts; a name or number they back up counts as grounded


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
    # X's weighted count (harness/xtext.py): a link is 23 and CJK or an emoji is 2, so len() is wrong both ways.
    cap, n = c.limits.max_chars.get(c.kind, MAX_WEIGHTED_CHARS), weighted_length(text)
    return [(f"{n} weighted chars (max {cap} for {c.kind})", overflow_index(text, cap), len(text))] if n > cap else []


def _rule_reply_sentences(text: str, c: _Ctx) -> list[str]:
    cap = c.limits.max_reply_sentences
    if c.kind != "reply" or cap is None:
        return []
    n = len(SENTENCE_END.findall(text.strip())) or 1
    return [(f"{n} sentences in a reply (max {cap})", None, None)] if n > cap else []


def verify_markers(text: str) -> list[str]:
    """The [VERIFY: ...] markers a person still has to resolve."""
    return VERIFY.findall(text or "")


def _unmarked(text: str) -> str:
    """`text` with each [VERIFY: ...] blanked to spaces of the same length, so spans keep their places and the
    grounding rules don't flag the number or name the marker already flags."""
    return VERIFY.sub(lambda m: " " * len(m.group(0)), text)


def _rule_placeholder(text: str, c: _Ctx) -> list[str]:
    out, taken = [], []
    for pattern in (PLACEHOLDER, TODO, *MEDIA_PLACEHOLDERS):
        for m in pattern.finditer(text):
            if any(m.start() < b and a < m.end() for a, b in taken):
                continue  # one finding per span, from the first pattern that matched it
            taken.append((m.start(), m.end()))
            out.append((f"unresolved placeholder {m.group(0).strip()!r}", m.start(), m.end()))
    return sorted(out, key=lambda f: f[1])


def _rule_url(text: str, c: _Ctx) -> list[str]:
    return [(f"link {m.group(0)!r}", m.start(), m.end()) for m in URL.finditer(text)]


def _rule_engagement_bait(text: str, c: _Ctx) -> list:
    return [(f"engagement bait {m.group(0)!r}", m.start(), m.end()) for m in ENGAGEMENT_BAIT.finditer(text)]


def _rule_wrapped_in_quotes(text: str, c: _Ctx) -> list[str]:
    t = text.strip()
    quotes = {'"': '"', "“": "”", "'": "'"}
    return [("whole draft wrapped in quotes", None, None)] if len(t) > 2 and t[0] in quotes and t.endswith(quotes[t[0]]) else []


TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\u2019]*")
REPEATS = re.compile(r"(.)\1{2,}")  # "CANADAAA" -> "CANADA"


def _rule_content_free(text: str, c: _Ctx) -> list:
    """No words at all (ellipses, emoji, punctuation), or a few words that touch nothing in the post."""
    if not text.strip():
        return []  # the empty rule reports this
    words = TOKEN.findall(text)
    if not words:
        return [("no words: only punctuation, ellipses or emoji", None, None)]
    reference = c.post if c.post is not None else c.context
    if len(words) >= c.limits.min_words_without_post_reference or reference is None:
        return []
    ref = REPEATS.sub(r"\1", reference)
    if any(len(w) >= 3 and REPEATS.sub(r"\1", w.lower()) in ref for w in words):
        return []
    return [(f"{len(words)} word(s) that share nothing with the post (min {c.limits.min_words_without_post_reference} "
             "without a reference)", None, None)]


# ---- grounding rules (skipped when the caller passes no context)


def _rule_unknown_handle(text: str, c: _Ctx) -> list[str]:
    if c.context is None:
        return []
    return [(f"@{m.group(1)} isn't in the post or thread", m.start(), m.end())
            for m in HANDLE.finditer(_unmarked(text)) if f"@{m.group(1).lower()}" not in c.context]


def _rule_ungrounded_number(text: str, c: _Ctx) -> list[str]:
    if c.context is None:
        return []
    unmarked = _unmarked(text)
    backed = grounded_numbers(c.facts, unmarked, c.post or "") if c.facts else set()
    return [(f"number {m.group(0)!r} isn't in the context", m.start(), m.end())
            for m in NUMBER.finditer(unmarked) if m.group(0) not in c.context and m.start() not in backed]


def _rule_ungrounded_name(text: str, c: _Ctx) -> list[str]:
    if c.context is None:
        return []
    return [(f"name {w!r} isn't in the context", a, b) for w, a, b in capitalized_names(_unmarked(text))
            if w.lower() not in c.context and not (c.facts and c.facts.grounds_name(w))]


def facts_used(text: str, *, context: str, facts: FactIndex, post: str | None = None) -> list[tuple[Fact, str]]:
    """(fact, the name or number it backs) for every name or number in `text` that `context` lacks and a fact in
    `facts` backs: what the grounding rules leaned on to let `text` through. Same matching as the rules."""
    text = _unmarked(unicodedata.normalize("NFC", text))
    ctx = unicodedata.normalize("NFC", context).lower()
    out = [(f, w) for w, _, _ in capitalized_names(text) if w.lower() not in ctx for f in facts.facts_for_name(w)]
    anchors = anchor_words(text) | anchor_words(post or "") | facts.anchors
    for m in NUMBER.finditer(text):
        if m.group(0) not in ctx:
            out += [(f, m.group(0)) for f in facts.facts_for_number(m.group(0), unit_after(text, m.end()), anchors)]
    return out


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
    "engagement_bait": _rule_engagement_bait,
    "wrapped_in_quotes": _rule_wrapped_in_quotes,
    "content_free": _rule_content_free,
    "unknown_handle": _rule_unknown_handle,
    "ungrounded_number": _rule_ungrounded_number,
    "ungrounded_name": _rule_ungrounded_name,
}
GROUNDING_RULES = ("unknown_handle", "ungrounded_number", "ungrounded_name")


def check(text: str, kind: str = "reply", *, context: str | None = None, limits: GateLimits | None = None,
          rules: dict | None = None, post: str | None = None, facts: FactIndex | None = None) -> GateResult:
    """Run every rule on `text`. `kind` is the action type. `context` is everything the writer
    saw (post, thread, facts); without it the grounding rules are skipped. `post` is the text being
    answered; the content-free rule checks short drafts against it (falling back to `context`).
    `facts` indexes live facts (harness/facts/ground.py): a name or number they back up passes the
    grounding rules even when the context lacks it."""
    if kind not in KINDS:
        raise ValueError(f"gate.check: unknown kind {kind!r}; expected one of {list(KINDS)}")
    text = unicodedata.normalize("NFC", text)
    norm = lambda x: unicodedata.normalize("NFC", x).lower() if x is not None else None  # noqa: E731
    ctx = _Ctx(kind, limits or GateLimits(), norm(context), norm(post), facts)
    findings = [Finding(name, d, a, b) for name, rule in (rules or RULES).items() for d, a, b in rule(text, ctx)]
    return GateResult(passed=not findings, findings=findings)
