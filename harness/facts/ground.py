"""Which names and numbers a set of live facts backs up. Pure functions; the gate calls them.

A fact is live at a moment when its status is "current" and `as_of <= day < valid_until`. The end is
exclusive to match `harness.learning.ranking.is_stale`, which treats a memory as stale on its
`valid_until` day. A fact without `valid_until` is never live: a source that can't say when its claim
expires shouldn't silence the gate.

Names. A capitalized word the gate would flag counts as grounded when it appears as a whole word in a
live fact's answer, ignoring case, curly apostrophes and a trailing possessive ("Fever's").

Numbers are riskier, because a box score or a brand's month of posts holds dozens of them and an
invented stat would often hit one by chance. A number counts as grounded only when a live fact about
something the draft, the post or the target's handle names (an anchor, from the fact's subject id)
holds the same number with the same unit: the word right after it, with stat synonyms folded
together, so "27 pts" matches "27 points" but "dropping 31 tonight" matches nothing in a box score,
and "500mb" doesn't match "500 → 1,000". A hyphenated pair such as a score, a record or "8-16" from
the field is specific enough to match without a unit, but only as written: a 28-16 record must not back
up "16-28". Game facts write the score both ways round, because people write 94-83 or 83-94. Matching
the number alone let 9 of 143 invented WNBA stats and 15 of 235 invented dev-tool numbers through
(action/DESIGN.md), which is why the unit is required.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from harness.learning.kinds import Fact

NUMBER = re.compile(r"(?<![\w@#])\d+(?:[.,:/\-]\d+)*")  # the gate's NUMBER pattern
WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
POSSESSIVE = re.compile(r"'s$")
# Stat words after a number, mapped to one label. Fact answers use the first spelling of each.
STAT_WORDS = {
    "points": ("points", "point", "pts", "pt", "piece"),
    "rebounds": ("rebounds", "rebound", "reb", "rebs", "boards", "board"),
    "assists": ("assists", "assist", "ast", "asts", "dimes", "dime"),
    "steals": ("steals", "steal", "stl", "stls"),
    "blocks": ("blocks", "block", "blk", "blks"),
    "turnovers": ("turnovers", "turnover", "tov", "tovs"),
    "threes": ("threes", "three", "triples"),
    "minutes": ("minutes", "minute", "min", "mins"),
}
STAT_LABEL = {w: label for label, words in STAT_WORDS.items() for w in words}
UNIT_AFTER = re.compile(r"%|\s?-?\s?([A-Za-z]+)")
# Subject-id words too common to anchor a number ("state" in Golden State, "new" in New York).
ANCHOR_STOP = {"entity", "the", "and", "new", "state", "city", "las", "los", "san"}


def _day(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def is_live(fact: Fact, at: date | datetime) -> bool:
    if fact.status != "current" or not fact.valid_until or not fact.as_of:
        return False
    d = _day(at)
    return date.fromisoformat(fact.as_of[:10]) <= d < date.fromisoformat(fact.valid_until[:10])


def live(facts: Iterable[Fact], at: date | datetime) -> list[Fact]:
    return [f for f in facts if is_live(f, at)]


def normalize(text: str) -> str:
    return (text or "").lower().replace("’", "'").replace("‘", "'")


def words(text: str) -> set[str]:
    """Lowercased whole words, apostrophes straightened, with a possessive 's dropped."""
    return {POSSESSIVE.sub("", w) for w in WORD.findall(normalize(text))}


def anchor_words(text: str) -> set[str]:
    """Words that can tie a number to a fact: three letters or more, split on anything but a-z0-9."""
    return {w for w in re.findall(r"[a-z0-9]+", normalize(text)) if len(w) >= 3 and w not in ANCHOR_STOP}


def subject_anchors(fact: Fact) -> set[str]:
    return anchor_words(fact.subject.replace("-", " "))


def split_handle(handle: str) -> str:
    """'IndianaFever' -> 'Indiana Fever', 'CaitlinClark22' -> 'Caitlin Clark 22': the words a handle runs together."""
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[0-9])(?=[A-Za-z])", " ", handle.lstrip("@"))


def unit_after(text: str, end: int) -> str:
    """The unit of a number ending at `end`: "%", or the word right after it (one space or hyphen allowed)
    with stat synonyms folded to one label and a plural s dropped, or "" when none follows."""
    m = UNIT_AFTER.match(text, end)
    if not m:
        return ""
    if m.group(0) == "%":
        return "%"
    word = m.group(1).lower()
    single = word[:-1] if len(word) > 3 and word.endswith("s") else word
    return STAT_LABEL.get(word) or STAT_LABEL.get(single) or single


def is_pair(token: str) -> bool:
    """A hyphenated pair of numbers: a score, a record, a shooting line."""
    return token.count("-") == 1


@dataclass(frozen=True)
class _Indexed:
    anchors: frozenset[str]
    pairs: frozenset[str]  # hyphenated pairs, as written
    numbers: frozenset[tuple[str, str]]  # (unit, number) for every other number
    words: frozenset[str]


class FactIndex:
    """The names and numbers in a set of facts, ready for the gate. Pass only live facts. `anchors` are
    words that tie a number to a fact for every draft checked against this index, such as the words of
    the target's handle and post (`anchor_words(split_handle(handle) + " " + post)`)."""

    def __init__(self, facts: Sequence[Fact], anchors: Iterable[str] = ()):
        self.facts = list(facts)
        self.anchors = frozenset(anchors)
        self._words: set[str] = set()
        self._indexed: list[_Indexed] = []
        for f in self.facts:
            fact_words = words(f.answer)
            self._words |= fact_words
            pairs, numbers = set(), set()
            for m in NUMBER.finditer(f.answer):
                if is_pair(m.group(0)):
                    pairs.add(m.group(0))
                else:
                    numbers.add((unit_after(f.answer, m.end()), m.group(0)))
            self._indexed.append(_Indexed(frozenset(subject_anchors(f)), frozenset(pairs), frozenset(numbers),
                                          frozenset(fact_words)))

    def __bool__(self) -> bool:
        return bool(self.facts)

    def grounds_name(self, name: str) -> bool:
        key = POSSESSIVE.sub("", normalize(name))
        return bool(key) and key in self._words

    def grounds_number(self, token: str, unit: str, anchors: set[str]) -> bool:
        """True when a fact about one of `anchors` holds `token`: a hyphenated pair as written, or any other
        number with the same unit (see unit_after)."""
        return bool(self.facts_for_number(token, unit, anchors))

    def facts_for_name(self, name: str) -> list[Fact]:
        """The facts that back `name` the way grounds_name does."""
        key = POSSESSIVE.sub("", normalize(name))
        return [f for f, ix in zip(self.facts, self._indexed) if key and key in ix.words]

    def facts_for_number(self, token: str, unit: str, anchors: set[str]) -> list[Fact]:
        """The facts that back `token` the way grounds_number does."""
        hyphenated = is_pair(token)
        return [f for f, ix in zip(self.facts, self._indexed) if ix.anchors & anchors
                and ((token in ix.pairs) if hyphenated else ((unit, token) in ix.numbers))]


def grounded_numbers(index: FactIndex, text: str, post: str = "") -> set[int]:
    """Start offsets of the numbers in `text` that `index` backs up, for the draft and the post it answers."""
    anchors = anchor_words(text) | anchor_words(post) | index.anchors
    return {m.start() for m in NUMBER.finditer(text)
            if index.grounds_number(m.group(0), unit_after(text, m.end()), anchors)}


def about(facts: Sequence[Fact], text: str, limit: int) -> list[Fact]:
    """Up to `limit` facts whose subject `text` names, newest first: what the planner sees for one target."""
    named = anchor_words(text)
    hits = [f for f in facts if subject_anchors(f) & named]
    return sorted(hits, key=lambda f: (f.as_of, f.id), reverse=True)[:limit]
