"""Product facts from a brand's pages, and the check that every claim in a drafted mission traces to a source.

A fact is kept only when the quote the model gives for it appears verbatim in the page and its statement passes
`trace` against that page. The same `trace` checks each bullet of a drafted mission.md against the pages and posts
it cites (`check_mission`), so the check can be rerun after a person edits the file.

What `trace` checks, for one claim and the texts of the sources it cites:
- it cites at least one source, and every cited URL was fetched;
- every name in it appears as a whole word in a cited source: the capitalized words the slop gate treats as names
  (`gate.capitalized_names`, which skips a sentence's first word), any word with a capital after its first letter
  or a digit inside it ("PostgreSQL", "vLLM"), and every @handle;
- every number in it appears in a cited source with the same unit, the rule `harness.facts.ground` uses;
- for factual sections, enough of its content words appear in its cited sources (`CONTENT_OVERLAP`).
A paraphrase that keeps the names and numbers but changes what is claimed about them can still pass. The overlap
rule narrows that gap; it doesn't close it, which is why the report asks a person to read every claim.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

from harness.facts import ground
from harness.learning.kinds import Fact, short_hash, slug
from harness.learning.store import STOPWORDS
from harness.llm import LLM
from harness.minds.gate import capitalized_names
from harness.onboard.site import Page

FACT_KINDS = ("product", "audience", "feature")
# How long a site fact stays live. It copies harness.facts.posts.POST_DAYS; no data says how fast site copy changes.
FACT_DAYS = 30
MAX_FACTS_PER_PAGE = 12
PAGE_CHARS = 12000  # of a page's text sent to the model; the top of a page is where it says what the product is
MIN_QUOTE_WORDS = 3
# Share of a factual claim's content words that must appear in what it cites. No measurement picked these: a
# restated quote keeps most of its words, and a claim about something else keeps few.
CONTENT_OVERLAP = {"site": 0.6, "subjects": 0.5}

HANDLE = re.compile(r"(?<![\w@])@(\w{1,15})")
INNER_CAPITAL = re.compile(r"\b[A-Za-z]+[A-Z0-9][A-Za-z0-9]*\b|\b[A-Za-z]*\d+[A-Za-z]+[A-Za-z0-9]*\b")
CONTENT_WORD = re.compile(r"[a-z][a-z0-9'-]{3,}")


def normalize(text: str) -> str:
    """Lowercase, NFKC, straight quotes and dashes, no zero-width characters, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.translate({0x2019: "'", 0x2018: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-",
                           0x200B: None, 0x200C: None, 0x200D: None, 0xFEFF: None})
    return " ".join(text.split())


def quote_found(quote: str, text: str) -> bool:
    """True when `quote` (three words or more) appears in `text` once both are normalized."""
    q = normalize(quote)
    return len(q.split()) >= MIN_QUOTE_WORDS and q in normalize(text)


def _stem(word: str) -> str:
    word = word.removesuffix("'s")
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 4:
        return word[:-1]
    return word


def content_words(text: str) -> set[str]:
    """Words of four letters or more that aren't stopwords, with a plural s dropped."""
    return {_stem(w.strip("'-")) for w in CONTENT_WORD.findall(normalize(text)) if w.strip("'-") not in STOPWORDS}


def names(text: str) -> list[str]:
    """The names `trace` requires a source for: see the module docstring."""
    found = [w for w, _, _ in capitalized_names(text)]
    found += INNER_CAPITAL.findall(text)
    found += ["@" + h for h in HANDLE.findall(text)]
    return list(dict.fromkeys(found))


def _numbers(text: str) -> set[tuple[str, str]]:
    """(unit, number) for each number, and ("pair", "8-16") for a hyphenated pair, which matches only as written."""
    out = set()
    for m in ground.NUMBER.finditer(text):
        token = m.group(0)
        out.add(("pair", token) if ground.is_pair(token) else (ground.unit_after(text, m.end()), token))
    return out


def trace(claim: str, sources: list[str], overlap: float | None = None) -> list[str]:
    """Why `claim` doesn't trace to `sources` (the texts it cites); an empty list means it does."""
    if not sources:
        return ["cites no source"]
    problems = []
    source_words = set().union(*(ground.words(s) for s in sources))
    for name in names(claim):
        key = ground.POSSESSIVE.sub("", ground.normalize(name.lstrip("@")))
        if key not in source_words and key.removesuffix("s") not in source_words:  # "APIs" against "API"
            problems.append(f"name {name!r} isn't in what it cites")
    have = set().union(*(_numbers(s) for s in sources))
    for unit, token in sorted(_numbers(claim)):
        if (unit, token) not in have:
            shown = token if unit == "pair" else f"{token} ({unit or 'no unit'})"
            problems.append(f"number {shown!r} isn't in what it cites")
    if overlap is not None:
        mine = content_words(claim)
        theirs = set().union(*(content_words(s) for s in sources))
        if mine and len(mine & theirs) / len(mine) < overlap:
            missing = sorted(mine - theirs)
            problems.append(f"{len(mine & theirs)} of {len(mine)} content words are in what it cites (needs "
                            f"{overlap:.0%}); missing {missing}")
    return problems


# ---------------------------------------------------------------- facts from pages


@dataclass
class SiteFact:
    kind: str
    statement: str
    quote: str
    url: str
    id: str = ""  # "F1", "F2"... once every page's facts are collected


EXTRACT_SYSTEM = """You read one page of a company's website and list what it says about the company's product, so another step can describe the company without guessing.

The company runs the X account @{handle}. List what this page states about:
- product: what the product is or does;
- audience: who it is for;
- feature: a named feature, plan or capability.

For each, write a short plain statement, and copy the sentence or phrase from the page that says it into "quote", character for character, so it can be found in the page by search. Use only what the page states. Leave out what you would have to infer, marketing adjectives with no content, testimonials and customer names. At most {max_facts} facts; fewer is fine.

Return JSON {{"facts": [{{"kind": "product|audience|feature", "statement": "...", "quote": "..."}}]}}."""


def _mock_facts(page: Page) -> dict:
    line = next((x for x in page.text.splitlines() if len(x.split()) >= 5), "")
    return {"facts": [{"kind": "product", "statement": line, "quote": line}] if line else []}


def extract_facts(llm: LLM, handle: str, page: Page) -> tuple[list[SiteFact], list[str]]:
    """One call per page. Returns the facts that passed and a reason for each one dropped."""
    user = f"PAGE {page.url}\n\n{page.text[:PAGE_CHARS]}"
    out = llm.complete_json(EXTRACT_SYSTEM.format(handle=handle, max_facts=MAX_FACTS_PER_PAGE), user,
                            mock=lambda: _mock_facts(page))
    raw = out.get("facts") if isinstance(out, dict) else None
    if not isinstance(raw, list):
        return [], [f"{page.url}: the answer had no 'facts' list"]
    kept, dropped, seen = [], [], set()
    for f in raw[:MAX_FACTS_PER_PAGE]:
        if not isinstance(f, dict):
            dropped.append(f"{page.url}: a fact that isn't an object")
            continue
        kind, statement, quote = f.get("kind"), str(f.get("statement") or "").strip(), str(f.get("quote") or "").strip()
        if kind not in FACT_KINDS or not statement:
            dropped.append(f"{page.url}: kind {kind!r} or an empty statement: {statement[:80]!r}")
        elif not quote_found(quote, page.text):
            dropped.append(f"{page.url}: quote not found in the page: {quote[:80]!r}")
        elif problems := trace(statement, [page.text], CONTENT_OVERLAP["site"]):
            dropped.append(f"{page.url}: {statement[:80]!r}: {'; '.join(problems)}")
        elif normalize(statement) not in seen:
            seen.add(normalize(statement))
            kept.append(SiteFact(kind, statement, quote, page.url))
    return kept, dropped


def memory_fact(f: SiteFact, handle: str, as_of: date) -> Fact:
    """The fact as the facts store keeps it, live for FACT_DAYS from the day the page was read."""
    handle = handle.lstrip("@")
    return Fact(id=f"fact-site-{short_hash(f.url, f.quote)}", subject="entity-" + slug(ground.split_handle(handle)),
                question=f"What does {f.url} say about @{handle}'s {f.kind}?", answer=f.statement, quote=f.quote,
                source_url=f.url, as_of=as_of.isoformat(), valid_until=(as_of + timedelta(days=FACT_DAYS)).isoformat())


# ---------------------------------------------------------------- checking a mission.md


@dataclass(frozen=True)
class SectionRule:
    sources: str  # "site": only fetched pages; "post": only the account's posts; "any": either
    overlap: float | None = None
    verbatim: bool = False  # the bullet quotes a post, which must appear in it word for word


# The sections `profile.render_mission` writes and every bullet in them must trace. The other sections (action
# space, do, don't) are fixed rules copied from the devtools mission, not claims about the brand.
SECTIONS = {
    "What it is": SectionRule("site", CONTENT_OVERLAP["site"]),
    "Who it's for": SectionRule("site", CONTENT_OVERLAP["site"]),
    "Core subjects": SectionRule("any", CONTENT_OVERLAP["subjects"]),
    "Voice and tone": SectionRule("post"),
    "Goals": SectionRule("any"),
    "Examples, good": SectionRule("post", verbatim=True),
}
LABEL = re.compile(r"^\*\*(.+?):\*\*")
LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
CITATIONS = re.compile(r"\s*\((?:\s*\[[^\]]*\]\(https?://[^)\s]+\)\s*,?)+\)\s*$")


@dataclass
class ClaimCheck:
    section: str
    text: str
    urls: list[str]
    problems: list[str] = field(default_factory=list)


def parse_mission(md: str) -> list[ClaimCheck]:
    """Every bullet under a SECTIONS label, with the URLs it cites and its text without the citations."""
    out, section = [], None
    for line in md.splitlines():
        m = LABEL.match(line.strip())
        if m:
            section = m.group(1).strip() if m.group(1).strip() in SECTIONS else None
            continue
        if line.startswith("#"):
            section = None
        elif section and line.lstrip().startswith("- "):
            body = line.lstrip()[2:].strip()
            out.append(ClaimCheck(section, CITATIONS.sub("", body).strip(), [u for _, u in LINK.findall(body)]))
    return out


def check_mission(md: str, pages: dict[str, str], posts: dict[str, str]) -> list[ClaimCheck]:
    """Trace every claim in `md`. `pages` maps each fetched page URL to its text, `posts` each post URL to its text."""
    claims = parse_mission(md)
    for c in claims:
        rule = SECTIONS[c.section]
        allowed = {"site": pages, "post": posts, "any": pages | posts}[rule.sources]
        unknown = [u for u in c.urls if u not in allowed]
        c.problems += [f"cites {u}, which isn't a fetched {'page' if rule.sources == 'site' else 'post' if rule.sources == 'post' else 'page or post'}"
                       for u in unknown]
        texts = [allowed[u] for u in c.urls if u in allowed]
        if rule.verbatim:
            quoted = c.text.strip().removeprefix('"').removesuffix('"')
            if not texts or not any(normalize(quoted) in normalize(t) for t in texts):
                c.problems.append("the quoted post isn't word for word in the post it links")
        elif not unknown or texts:
            c.problems += trace(c.text, texts, rule.overlap)
    return claims
