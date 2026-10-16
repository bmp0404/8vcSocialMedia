"""Brand ingest: read a brand's own pages, question them, and keep only answers its pages back up.

`ingest_brand` fetches the sources (URLs or local files). From the first URL it also reads the site's
llms.txt and follows same-site links that robots.txt allows. It asks the model which questions a
strategist would add to `CORE_QUESTIONS`, then asks it to answer all of them from the pages alone. An
answer becomes a `Fact` only if its quote is on the page it cites (after `normalize` evens out case,
whitespace, curly quotes and dashes) and every number, date and name in the answer is in that quote.
A fact is keyed by its subject and question, so a later ingest that finds a different answer supersedes
the old fact instead of adding a second one. The prompts name no brand or domain, so the same code reads
a dev-tool company's website and a sports fan account's notes.
"""

import codecs
import re
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from harness.learning import kinds
from harness.learning.kinds import Entity, Fact, short_hash

USER_AGENT = "media-harness/0.1"
ROBOTS_AGENT = "media-harness"  # the product token robots.txt groups are matched against
# Characters of page text per model call, about 10k tokens, so both calls stay small. No run has tuned it.
MATERIAL_BUDGET = 40_000
# Brand pages change with launches and pricing. A month is a guess; no data backs it.
VALID_DAYS = 30
# Followed pages with less text than this are login forms and empty shells, not material. resend.com/login
# has 195 characters; 300 is a guess above that, not measured across sites. Listed sources are exempt.
MIN_FOLLOWED_CHARS = 300
NOT_STATED = "not stated"
ANSWER_KEYS = ("question", "answer", "quote", "source_url")
HTML_TYPES = ("text/html", "application/xhtml+xml")
PLAIN_TYPES = ("text/plain", "text/markdown", "text/x-markdown")

CORE_QUESTIONS = [
    "What is this brand or account, and what does it do?",
    "Who is it for: which audience, customers or fans does it address?",
    "How does it talk: what tone does it use, and which phrases or slogans does it repeat?",
    "What products, launches or recurring subjects does it focus on?",
    "What concrete claims or numbers does it state?",
    "Which people are associated with it, and in what role?",
    "Which rivals or competitors does it name?",
    "What does it say to avoid, or what is it careful about?",
]


class IngestError(Exception):
    pass


class _Unreadable(Exception):
    """Why one URL or file could not be read, with the HTTP status when there was one."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.status = status


@dataclass
class Page:
    url: str
    title: str
    text: str


SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
# Tags that start a new line in the extracted text, so a heading doesn't run into the paragraph after it.
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "td", "th", "tr", "ul",
}
ASSET_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp",
    ".css", ".js", ".mjs", ".map", ".pdf", ".eot", ".otf", ".ttf", ".woff", ".woff2",
    ".mp3", ".mp4", ".webm", ".zip",
    # Data and scripts that llms.txt files link to, such as resend.com's install.sh and mcp.json.
    ".json", ".sh", ".xml",
}

# Marks where an inline tag opened or closed. Sites lay out spans and links as separate items with CSS,
# so resend.com's markup reads "Log inGet started" and "Zeno RochaCEO, Resend" when tags are simply
# dropped. A marker between a word or closing punctuation and a following word becomes a space; any
# other marker is removed, so "the <a>docs</a>." stays "the docs.".
_TAG_EDGE = "\x00"
_EDGE_TO_SPACE = re.compile(r"(?<=[\w.!?:;,)])\x00+(?=\w)")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.title: list[str] = []
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif self.skip_depth:
            return
        elif tag == "title":
            self.in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and href.strip():
                self.hrefs.append(href.strip())
        self.parts.append("\n" if tag in BLOCK_TAGS else _TAG_EDGE)

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag == "title":
            self.in_title = False
        else:
            self.parts.append("\n" if tag in BLOCK_TAGS else _TAG_EDGE)

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        data = re.sub(r"\s+", " ", data.replace(_TAG_EDGE, ""))
        (self.title if self.in_title else self.parts).append(data)


def extract_text(html: str) -> tuple[str, str, list[str]]:
    """(title, visible text, hrefs). Text keeps one line per block element, with spaces collapsed."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    joined = _EDGE_TO_SPACE.sub(" ", "".join(parser.parts)).replace(_TAG_EDGE, "")
    lines = (" ".join(line.split()) for line in joined.split("\n"))
    text = "\n".join(line for line in lines if line)
    return " ".join("".join(parser.title).split()), text, parser.hrefs


def _page_key(url: str) -> tuple[str, str, str]:
    """What makes two URLs the same page here: host, path and query. Scheme and fragment don't count."""
    parts = urlsplit(url.strip())
    return (parts.hostname or "", parts.path or "/", parts.query)


def same_site_links(base_url: str, hrefs: list[str], limit: int) -> list[str]:
    """Absolute http(s) links on base_url's host, minus fragments, assets, repeats and the base page."""
    host = urlsplit(base_url).hostname
    seen = {_page_key(base_url)}
    links: list[str] = []
    for href in hrefs:
        if len(links) >= limit:
            break
        try:
            url, _ = urldefrag(urljoin(base_url, href.strip()))
            parts = urlsplit(url)
        except ValueError:  # malformed href, such as an unclosed IPv6 bracket
            continue
        if parts.scheme not in ("http", "https") or parts.hostname != host:
            continue
        if PurePosixPath(parts.path).suffix.lower() in ASSET_EXTENSIONS:
            continue
        key = _page_key(url)
        if key not in seen:
            seen.add(key)
            links.append(url)
    return links


def _is_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _site_root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _get(url: str, timeout: int, accept: tuple[str, ...] | None = None) -> tuple[str, str, str]:
    """(final URL, content type, body) of one GET. A type outside `accept` fails before the body is read."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get_content_type()
            if accept and ctype not in accept:
                raise _Unreadable(f"content type {ctype} is not a web page")
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                codecs.lookup(charset)
            except LookupError:
                charset = "utf-8"
            return resp.geturl(), ctype, resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise _Unreadable(f"HTTP {e.code} {e.reason}", status=e.code) from e
    except urllib.error.URLError as e:
        raise _Unreadable(str(e.reason)) from e
    except TimeoutError as e:
        raise _Unreadable(f"timed out after {timeout}s") from e
    except (OSError, ValueError) as e:
        raise _Unreadable(str(e)) from e


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _fetch_url(url: str, timeout: int) -> tuple[Page, list[str]]:
    final_url, ctype, body = _get(url, timeout, accept=HTML_TYPES + PLAIN_TYPES)
    if ctype in PLAIN_TYPES:
        title, text, hrefs = _first_heading(body), body.strip(), []
    else:
        title, text, hrefs = extract_text(body)
    if not text:
        raise _Unreadable("no visible text; the page may build its content with JavaScript")
    return Page(final_url, title, text), hrefs


def _read_file(source: str) -> Page:
    path = Path(source).expanduser()
    suffix = path.suffix.lower()
    if suffix not in (".md", ".markdown", ".txt", ".html", ".htm"):
        raise _Unreadable(f"unsupported file type {suffix or '(none)'}; use .md, .txt or .html")
    if not path.is_file():
        raise _Unreadable("no such file")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise _Unreadable(f"not UTF-8 text ({e.reason} at byte {e.start})") from e
    except OSError as e:
        raise _Unreadable(e.strerror or str(e)) from e
    url = path.resolve().as_uri()
    if suffix in (".html", ".htm"):
        title, text, _ = extract_text(raw)
        page = Page(url, title or path.stem, text)
    else:
        page = Page(url, _first_heading(raw) or path.stem, raw.strip())
    if not page.text:
        raise _Unreadable("file has no text")
    return page


def _robots(base_url: str, timeout: int) -> RobotFileParser | None:
    """The site's robots.txt rules, or None when they can't be read and no link should be followed.

    Follows RFC 9309: a 4xx answer means the site has no rules, while a server or network error means
    the rules are unknown and everything counts as disallowed.
    """
    url = _site_root(base_url) + "/robots.txt"
    rules = RobotFileParser(url)
    try:
        _, _, body = _get(url, timeout)
    except _Unreadable as e:
        if e.status is not None and 400 <= e.status < 500:
            rules.parse([])
            return rules
        print(f"[brand] not following links: {url} is unreachable ({e})")
        return None
    rules.parse(body.splitlines())
    return rules


_LLMS_LINK = re.compile(r"\]\(<?([^)\s>]+)>?\)|(https?://[^\s)\]>\"']+)")


def _llms_txt(base_url: str, timeout: int) -> tuple[Page, list[str]] | None:
    """The site's /llms.txt as a page plus the links it lists, or None when the site has none."""
    url = _site_root(base_url) + "/llms.txt"
    try:
        final_url, ctype, body = _get(url, timeout)
    except _Unreadable as e:
        if e.status is None or e.status >= 500:
            print(f"[brand] skipped {url}: {e}")
        return None
    # Sites that answer every path with their app shell return HTML here, which is not an llms.txt.
    if ctype not in PLAIN_TYPES or not body.strip():
        return None
    links = [(md or bare).rstrip(".,;:") for md, bare in _LLMS_LINK.findall(body)]
    return Page(final_url, _first_heading(body) or "llms.txt", body.strip()), links


def fetch_pages(sources: list[str], max_pages: int = 6, timeout: int = 15) -> list[Page]:
    """Read every source, then crawl from the first URL until there are max_pages pages.

    Every listed source is read even if there are more than max_pages of them; the crawl only fills the
    room that is left. A listed source that fails raises IngestError. The crawl reads the site's
    robots.txt, adds its llms.txt as a page when there is one, and follows same-site links, those listed
    in llms.txt first. It skips, with a printed reason, links robots.txt disallows and pages that fail,
    repeat an earlier page's text, or have almost no text.
    """
    if not sources:
        raise IngestError("no sources given; pass at least one URL or file path")
    pages: list[Page] = []
    crawl: tuple[str, list[str]] | None = None
    for source in sources:
        try:
            if _is_url(source):
                page, hrefs = _fetch_url(source, timeout)
                if crawl is None:
                    crawl = (page.url, hrefs)
            elif "://" in source:
                raise _Unreadable("only http and https URLs are supported")
            else:
                page = _read_file(source)
        except _Unreadable as e:
            raise IngestError(f"could not read {source}: {e}") from e
        pages.append(page)

    if crawl is None or len(pages) >= max_pages:
        return pages
    base_url, hrefs = crawl
    robots = _robots(base_url, timeout)
    if robots is None:
        return pages
    host = urlsplit(base_url).hostname
    fetched = {_page_key(p.url) for p in pages}
    texts = {p.text: p.url for p in pages}

    llms_url = _site_root(base_url) + "/llms.txt"
    if _page_key(llms_url) not in fetched and robots.can_fetch(ROBOTS_AGENT, llms_url):
        llms = _llms_txt(base_url, timeout)
        if llms:
            page, listed = llms
            pages.append(page)
            fetched.add(_page_key(page.url))
            texts[page.text] = page.url
            hrefs = listed + hrefs  # llms.txt names the pages the site wants read first

    # Twice the room left, so a few dead links don't end the crawl early while a site whose links all
    # fail costs a bounded number of timeouts.
    attempts = 2 * (max_pages - len(pages))
    for url in same_site_links(base_url, hrefs, limit=len(hrefs)):
        if len(pages) >= max_pages or attempts <= 0:
            break
        if _page_key(url) in fetched:
            continue
        if not robots.can_fetch(ROBOTS_AGENT, url):
            print(f"[brand] skipped {url}: disallowed by robots.txt")
            continue
        attempts -= 1
        try:
            page, _ = _fetch_url(url, timeout)
            if urlsplit(page.url).hostname != host:
                raise _Unreadable(f"redirected off-site to {page.url}")
            if _page_key(page.url) in fetched:
                raise _Unreadable(f"redirected to {page.url}, which is already fetched")
            if page.text in texts:
                raise _Unreadable(f"same text as {texts[page.text]}")
            if len(page.text) < MIN_FOLLOWED_CHARS:
                raise _Unreadable(f"only {len(page.text)} characters of text")
        except _Unreadable as e:
            print(f"[brand] skipped {url}: {e}")
            continue
        pages.append(page)
        fetched.add(_page_key(page.url))
        texts[page.text] = page.url
    return pages


_TO_ASCII = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
        # Soft hyphens and zero-width characters hide inside HTML words and break substring matches.
        "­": None, "​": None, "‌": None, "‍": None, "﻿": None,
    }
)


def normalize(s: str) -> str:
    """Fold the differences a model introduces when it copies text: quote style, dashes, spacing, case.

    NFKC comes first so accented letters compare equal whether composed or not, and so a non-breaking
    space or an ellipsis character matches its plain form.
    """
    s = unicodedata.normalize("NFKC", s).translate(_TO_ASCII)
    return " ".join(s.split()).casefold()


def verify_quote(quote: str, page_text: str, min_len: int = 12) -> bool:
    q = normalize(quote)
    return len(q) >= min_len and q in normalize(page_text)


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[^\W\d_][\w'’-]*")


def unsupported_terms(answer: str, quote: str, brand_name: str = "") -> list[str]:
    """Numbers, dates and capitalized names in the answer that its quote doesn't contain.

    A date is checked through its parts: digit runs and the capitalized month name. The first word of
    each sentence and the words of the brand name are skipped, because they are capitalized for other
    reasons. Each term must appear in the quote as a whole word, after `normalize`.
    """
    haystack = normalize(quote)
    brand_words = set(normalize(brand_name).split())
    terms = _NUMBER.findall(answer)
    for sentence in re.split(r"(?<=[.!?])\s+", answer.strip()):
        lead = len(sentence) - len(sentence.lstrip("\"'“‘([*"))
        for m in _WORD.finditer(sentence):
            word = re.sub(r"['’]s$", "", m.group()).rstrip("'’-")
            if m.start() == lead or not word[:1].isupper() or normalize(word) in brand_words:
                continue
            terms.append(word)
    missing: list[str] = []
    for term in terms:
        found = re.search(rf"(?<!\w){re.escape(normalize(term))}(?!\w)", haystack)
        if not found and term not in missing:
            missing.append(term)
    return missing


SYSTEM = (
    "You help a social media strategist learn a brand or account from its own pages before anyone writes "
    "posts or replies for it. You work only from the pages you are given."
)

FOLLOWUP_PROMPT = """{material}

Brand or account: {brand}

The pages above come from {brand}'s own site or notes. Each sits inside a <page> tag that carries its url. These core questions will be asked about them:
{core}

Write {n} more questions that a strategist would want answered before writing posts and replies for {brand}, and that the core questions miss. Make each question specific to this brand by naming the product, person, event, claim or phrase it asks about, and ask it plainly without stating its answer or listing details from the pages. Prefer questions one of the pages can answer, because the next step answers from these pages alone and discards any answer it can't quote.

Return JSON: {{"questions": ["...", "..."]}}"""

ANSWER_PROMPT = """{material}

Brand or account: {brand}

Answer each question below using only the pages above. The answers become facts that drafts for this account repeat in public, so a guessed answer does harm and "not stated" costs nothing.

- Don't infer, guess, or add what you know about {brand} from elsewhere. If no page says it, it is not stated.
- Copy the quote verbatim from one page: a contiguous run of its exact words, at least a full clause, with no ellipses, paraphrase or corrected punctuation. Code looks for every quote in that page's text and drops the answer when it can't find it.
- Set source_url to the url of the page the quote comes from, exactly as it appears in that page's <page> tag.
- Write the answer as one or two sentences that restate the quote. Every number, date and name in the answer must appear in the quote, because code drops answers that add any. When the pages give several facts for one question, answer with the one the best quote supports.
- When the pages don't answer a question, set answer to "not stated" and set quote and source_url to "".
- Return one entry per question, in the order given, with the question copied exactly.

Questions:
{questions}

Return JSON: {{"answers": [{{"question": "...", "answer": "...", "quote": "...", "source_url": "..."}}]}}"""


def _split_budget(lengths: list[int], budget: int) -> list[int]:
    """An equal share per page; a page shorter than its share hands what it doesn't use to longer pages."""
    shares = [0] * len(lengths)
    remaining = budget
    for n, i in enumerate(sorted(range(len(lengths)), key=lambda i: lengths[i])):
        shares[i] = min(lengths[i], remaining // (len(lengths) - n))
        remaining -= shares[i]
    return shares


def _clip(text: str, limit: int) -> str:
    """Cut text to at most `limit` characters, at a space when one is near the end."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    return cut[:space] if space > limit // 2 else cut


def _material(pages: list[Page], budget: int = MATERIAL_BUDGET) -> str:
    blocks = []
    for page, share in zip(pages, _split_budget([len(p.text) for p in pages], budget)):
        text = page.text if share >= len(page.text) else _clip(page.text, share) + "\n[rest of page cut]"
        blocks.append(f'<page url="{page.url}">\nTitle: {page.title}\n\n{text}\n</page>')
    return "\n\n".join(blocks)


def _numbered(questions: list[str]) -> str:
    return "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))


def _first_sentence(pages: list[Page], min_len: int = 12, max_len: int = 300) -> tuple[str, str]:
    """(url, text) of the first full sentence long enough to verify, else the first such line.

    Feeds the offline mocks. Preferring a sentence skips headings and navigation labels.
    """
    pieces = [
        (page.url, piece.strip())
        for page in pages
        for piece in re.split(r"(?<=[.!?])\s+|\n+", page.text)
        if len(normalize(piece)) >= min_len
    ]
    for url, piece in pieces:
        if piece.endswith((".", "!", "?")):
            return url, _clip(piece, max_len)
    return (pieces[0][0], _clip(pieces[0][1], max_len)) if pieces else ("", "")


def _mock_followups(pages: list[Page], n: int) -> dict:
    return {"questions": [f'What does the page "{p.title or p.url}" say about the brand?' for p in pages][:n]}


def _mock_answers(pages: list[Page], questions: list[str]) -> dict:
    """Answers only the first question, with the first sentence of the material; the rest are not stated."""
    answers = [{"question": q, "answer": NOT_STATED, "quote": "", "source_url": ""} for q in questions]
    url, sentence = _first_sentence(pages)
    if sentence:
        answers[0] = {"question": questions[0], "answer": sentence, "quote": sentence, "source_url": url}
    return {"answers": answers}


def _read_followups(result, n: int) -> list[str]:
    questions = result.get("questions") if isinstance(result, dict) else None
    if not isinstance(questions, list):
        raise IngestError(f"follow-up call returned no 'questions' list: {str(result)[:200]}")
    seen = {normalize(q) for q in CORE_QUESTIONS}
    out = []
    for q in questions:
        if isinstance(q, str) and q.strip() and normalize(q) not in seen:
            seen.add(normalize(q))
            out.append(q.strip())
    return out[:n]


def _read_answers(result) -> list[dict]:
    items = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise IngestError(f"answer call returned no 'answers' list: {str(result)[:200]}")
    answers = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            print(f"[brand] ignored an answer without a question: {str(item)[:100]}")
            continue
        answers.append({k: str(item.get(k) or "").strip() for k in ANSWER_KEYS})
    return answers


def _ask(llm, step: str, prompt: str, mock):
    try:
        return llm.complete_json(SYSTEM, prompt, mock=mock)
    except ValueError as e:  # parse_json raises ValueError when the reply holds no JSON
        raise IngestError(f"the {step} call returned no usable JSON: {e}") from e


def question_brand(llm, pages: list[Page], brand_name: str, n_followups: int = 8) -> list[dict]:
    """Ask follow-ups, then answer them and CORE_QUESTIONS from the pages. Returns unchecked answers.

    Each answer is {"question", "answer", "quote", "source_url"}, all strings. `ingest_brand` decides
    which to keep.
    """
    if not pages:
        raise IngestError("no pages to question")
    material = _material(pages)
    followups: list[str] = []
    if n_followups > 0:
        prompt = FOLLOWUP_PROMPT.format(
            material=material, brand=brand_name, core=_numbered(CORE_QUESTIONS), n=n_followups
        )
        result = _ask(llm, "follow-up", prompt, mock=lambda: _mock_followups(pages, n_followups))
        followups = _read_followups(result, n_followups)
    questions = CORE_QUESTIONS + followups
    prompt = ANSWER_PROMPT.format(material=material, brand=brand_name, questions=_numbered(questions))
    result = _ask(llm, "answer", prompt, mock=lambda: _mock_answers(pages, questions))
    return _read_answers(result)


def _entity_id(brand_name: str) -> str:
    entity_id = "entity-" + kinds.slug(brand_name)
    if entity_id == "entity-":
        raise IngestError(f"brand name {brand_name!r} needs at least one ASCII letter or digit for its id")
    if len(entity_id) > 80:
        raise IngestError(f"brand name {brand_name!r} makes an id over the store's 80-character limit")
    return entity_id


def fact_id(subject: str, question: str) -> str:
    """The id of the current fact answering `question` about `subject`."""
    return "fact-" + short_hash(subject, normalize(question))


def _url_key(url: str) -> str:
    """Lookup key for a cited URL, so "https://x.com" and "https://x.com/" find the same page."""
    return url.strip().rstrip("/")


def _drop_reason(answer: dict, page: Page | None, brand_name: str) -> str:
    if normalize(answer["answer"]).rstrip(" .") in ("", NOT_STATED):
        return "not stated"
    if page is None:
        return "unknown source_url"
    if not verify_quote(answer["quote"], page.text):
        return "quote not found"
    if unsupported_terms(answer["answer"], answer["quote"], brand_name):
        return "answer not supported by quote"
    return ""


def _facts_about(store, subject: str) -> dict[str, Fact]:
    facts = {}
    for memory in store.list_memories(Fact.KIND):
        item = kinds.load(memory)
        if isinstance(item, Fact) and item.subject == subject:
            facts[item.id] = item
    return facts


def _same_claim(old: Fact, new: Fact) -> bool:
    return normalize(old.answer) == normalize(new.answer) and normalize(old.quote) == normalize(new.quote)


def _archive_id(old: Fact) -> str:
    """A new id for an old fact being superseded. Hashing the old fact's own `supersedes` keeps the chain
    free of cycles when an answer changes back and forth within one day."""
    parts = (old.as_of, normalize(old.answer), normalize(old.quote), old.supersedes)
    return "fact-" + short_hash(old.subject, normalize(old.question), *parts)


def _merge_existing_entity(store, entity: Entity, have_summary: bool) -> None:
    """Carry over what an earlier write holds, since write_memory replaces the whole note.

    Keeps aliases, stats and relations, and the old summary when this run verified no new one.
    """
    for memory in store.list_memories(Entity.KIND):
        if memory.id == entity.id:
            old = kinds.load(memory)
            entity.aliases, entity.stats, entity.relations = old.aliases, old.stats, old.relations
            if not have_summary:
                entity.summary = old.summary


def ingest_brand(
    store, llm, sources: list[str], brand_name: str, now: datetime | None = None, max_pages: int = 6
) -> dict:
    """Fetch the brand's pages, question them, and save the entity plus one Fact per verified answer.

    A fact is keyed by subject and question (`fact_id`). When the store already holds a fact for the
    question, an unchanged answer and quote only refresh its dates; a changed one moves the old fact to an
    archive id with status "superseded" and writes the new fact with `supersedes` pointing at it.

    Returns {"entity": id, "facts": answers kept, "superseded": n, "unchanged": n,
    "dropped": [{"question", "reason"}], "pages": [urls]}.
    """
    entity_id = _entity_id(brand_name)
    today = (now or datetime.now(timezone.utc)).date()
    pages = fetch_pages(sources, max_pages=max_pages)
    answers = question_brand(llm, pages, brand_name)

    by_url = {_url_key(p.url): p for p in pages}
    existing = _facts_about(store, entity_id)
    writes: list[Fact] = []
    kept: set[str] = set()
    dropped: list[dict] = []
    superseded = unchanged = 0
    summary = ""
    for answer in answers:
        page = by_url.get(_url_key(answer["source_url"]))
        reason = _drop_reason(answer, page, brand_name)
        fid = fact_id(entity_id, answer["question"])
        if not reason and fid in kept:
            reason = "duplicate question"
        if reason:
            dropped.append({"question": answer["question"], "reason": reason})
            continue
        kept.add(fid)
        new = Fact(
            id=fid,
            subject=entity_id,
            question=answer["question"],
            answer=answer["answer"],
            quote=answer["quote"],
            source_url=page.url,
            as_of=today.isoformat(),
            valid_until=(today + timedelta(days=VALID_DAYS)).isoformat(),
            status="current",
        )
        old = existing.get(fid)
        if old is None:
            writes.append(new)
        elif _same_claim(old, new):
            writes.append(replace(old, as_of=new.as_of, valid_until=new.valid_until))
            unchanged += 1
        else:
            archived = replace(old, id=_archive_id(old), status="superseded")
            writes += [archived, replace(new, supersedes=archived.id)]
            superseded += 1
        if not summary and normalize(answer["question"]) == normalize(CORE_QUESTIONS[0]):
            summary = answer["answer"]

    entity = Entity(
        id=entity_id, name=brand_name, summary=summary or brand_name, entity_type="brand",
        updated=today.isoformat(),
    )
    _merge_existing_entity(store, entity, have_summary=bool(summary))
    kinds.save(store, entity)
    for fact in writes:
        kinds.save(store, fact)

    reasons = Counter(d["reason"] for d in dropped)
    detail = ", ".join(f"{r} {n}" for r, n in reasons.items())
    print(
        f"[brand] {brand_name}: kept {len(kept)} facts from {len(pages)} pages "
        f"({superseded} superseded, {unchanged} unchanged), dropped {len(dropped)}"
        + (f" ({detail})" if detail else "")
    )
    return {
        "entity": entity_id,
        "facts": len(kept),
        "superseded": superseded,
        "unchanged": unchanged,
        "dropped": dropped,
        "pages": [p.url for p in pages],
    }
