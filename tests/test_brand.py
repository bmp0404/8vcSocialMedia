import email.message
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from harness.learning import brand
from harness.learning.brand import (
    CORE_QUESTIONS,
    IngestError,
    Page,
    extract_text,
    fact_id,
    fetch_pages,
    ingest_brand,
    normalize,
    same_site_links,
    unsupported_terms,
    verify_quote,
)
from harness.learning.kinds import ID_PATTERN, Entity, Fact, load
from harness.learning.store import FakeStore
from harness.llm import LLM, MockLLM


# normalize


def test_normalize_maps_curly_quotes_to_ascii():
    assert normalize("“Ship it”, she said. ‘Now’") == "\"ship it\", she said. 'now'"


def test_normalize_maps_en_and_em_dashes_to_hyphens():
    assert normalize("fast—and–cheap") == "fast-and-cheap"


def test_normalize_collapses_and_strips_whitespace():
    assert normalize("  two\n\tlines  here  ") == "two lines here"


def test_normalize_casefolds():
    assert normalize("STRASSE Straße École") == "strasse strasse école"


# verify_quote

PAGE = "Resend is the email API for developers.\nWe’re built for “developers” — not  marketers."


def test_verify_quote_accepts_a_quote_on_the_page():
    assert verify_quote("the email API for developers", PAGE)


def test_verify_quote_rejects_a_quote_not_on_the_page():
    assert not verify_quote("the SMS API for developers", PAGE)


def test_verify_quote_rejects_quotes_shorter_than_min_len():
    assert not verify_quote("email API", PAGE)  # 9 characters
    assert verify_quote("email API", PAGE, min_len=9)
    assert verify_quote("Resend is th", PAGE)  # exactly 12
    assert not verify_quote("Resend is t", PAGE)  # 11


def test_verify_quote_ignores_whitespace_case_and_quote_style():
    assert verify_quote('we\'re built for "developers" - not marketers.', PAGE)
    assert verify_quote("EMAIL API FOR\n developers.\nWe’re", PAGE)


# unsupported_terms


def test_unsupported_terms_flags_a_number_missing_from_the_quote():
    assert unsupported_terms("Pro costs $20 a month.", "Pro is $25/mo.", "Acme") == ["20"]


def test_unsupported_terms_matches_numbers_as_whole_tokens():
    assert unsupported_terms("It sends 20 emails.", "It sends 2026 emails.") == ["20"]
    assert unsupported_terms("Uptime is 99.99%.", "a 99.99% uptime SLA") == []


def test_unsupported_terms_keeps_a_name_present_in_the_quote():
    assert unsupported_terms("It was founded by Zeno Rocha in 2023.", "Founded in 2023 by Zeno Rocha.") == []


def test_unsupported_terms_flags_a_name_missing_from_the_quote():
    assert unsupported_terms("It competes with SendGrid.", "We beat legacy tools.") == ["SendGrid"]


def test_unsupported_terms_checks_dates_through_their_parts():
    assert unsupported_terms("The event is on Oct 21, 2026.", "Join us on October 21, 2026") == ["Oct"]


def test_unsupported_terms_skips_sentence_starts_brand_words_and_possessives():
    answer = "Developers pick Acme Rockets for its API. They like Vercel's setup."
    assert unsupported_terms(answer, "the API developers pick, says Vercel", "Acme Rockets") == []


# extract_text


def test_extract_text_drops_hidden_content_and_returns_hrefs():
    html = """<html><head><title> Acme   Mail </title><style>.x { color: red }</style>
    <script>var secret = 1;</script></head>
    <body><nav><a href="/pricing">Pricing</a><a href="https://other.com/x">Other</a></nav>
    <h1>Ship   email</h1><p>Fast &amp; simple, see <a href='/docs#start'>the docs</a>.</p>
    <noscript>Enable JavaScript</noscript><svg><title>icon</title><path d="M0"/></svg>
    <template><p>hidden</p></template><p><span>Jo Doe</span><span>CEO, Acme</span></p></body></html>"""
    title, text, hrefs = extract_text(html)
    assert title == "Acme Mail"
    assert text == "Pricing Other\nShip email\nFast & simple, see the docs.\nJo Doe CEO, Acme"
    assert hrefs == ["/pricing", "https://other.com/x", "/docs#start"]


# same_site_links


def test_same_site_links_resolves_filters_and_dedupes():
    hrefs = [
        "/pricing",
        "docs/start#intro",
        "https://acme.dev/pricing",  # repeat of /pricing
        "http://acme.dev/pricing#plans",  # same page over http
        "https://other.com/x",
        "https://sub.acme.dev/y",
        "/logo.PNG",
        "/app.js",
        "/style.css",
        "/brochure.pdf",
        "/font.woff2",
        "#top",  # the base page
        "/",
        "mailto:hi@acme.dev",
        "javascript:void(0)",
        "http://acme.dev/blog",
        "/about?ref=nav",
    ]
    assert same_site_links("https://acme.dev/", hrefs, limit=10) == [
        "https://acme.dev/pricing",
        "https://acme.dev/docs/start",
        "http://acme.dev/blog",
        "https://acme.dev/about?ref=nav",
    ]


def test_same_site_links_resolves_relative_to_the_page_path():
    assert same_site_links("https://acme.dev/blog/post", ["next", "../team"], limit=5) == [
        "https://acme.dev/blog/next",
        "https://acme.dev/team",
    ]


def test_same_site_links_respects_limit():
    hrefs = ["/a", "/b", "/c"]
    assert same_site_links("https://acme.dev", hrefs, limit=2) == ["https://acme.dev/a", "https://acme.dev/b"]
    assert same_site_links("https://acme.dev", hrefs, limit=0) == []


# fetch_pages


def test_fetch_pages_reads_local_markdown_and_html(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# Acme notes\n\nAcme sells  rockets.\n", encoding="utf-8")
    html = tmp_path / "about.html"
    html.write_text(
        "<title>About Acme</title><script>track()</script><p>Founded in 1949.</p>", encoding="utf-8"
    )
    pages = fetch_pages([str(md), str(html)])
    assert pages == [
        Page(url=md.resolve().as_uri(), title="Acme notes", text="# Acme notes\n\nAcme sells  rockets."),
        Page(url=html.resolve().as_uri(), title="About Acme", text="Founded in 1949."),
    ]


def test_fetch_pages_names_a_missing_file(tmp_path):
    missing = tmp_path / "gone.md"
    with pytest.raises(IngestError, match=r"gone\.md: no such file"):
        fetch_pages([str(missing)])


def test_fetch_pages_rejects_an_unsupported_file_type(tmp_path):
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"%PDF")
    with pytest.raises(IngestError, match=r"deck\.pdf: unsupported file type \.pdf"):
        fetch_pages([str(pdf)])


class FakeResponse:
    def __init__(self, url: str, body: str, content_type: str):
        self.url, self.body = url, body.encode("utf-8")
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def geturl(self):
        return self.url

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_web(monkeypatch, site: dict) -> list[str]:
    """Serve `site` in place of the network: url -> html, (body, content type), or an exception.

    URLs not in `site` answer 404. Returns the list of requested URLs.
    """
    requested = []

    def urlopen(req, timeout):
        assert req.get_header("User-agent") == "media-harness/0.1"
        requested.append(req.full_url)
        page = site.get(req.full_url)
        if page is None:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
        if isinstance(page, Exception):
            raise page
        body, ctype = page if isinstance(page, tuple) else (page, "text/html; charset=utf-8")
        return FakeResponse(req.full_url, body, ctype)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return requested


def page_html(name: str) -> str:
    return f"<title>{name}</title><p>{f'{name} has enough text to keep. ' * 20}</p>"


def links_html(hrefs: list[str]) -> str:
    return "".join(f'<a href="{h}">link</a>' for h in hrefs)


def test_fetch_pages_follows_same_site_links_and_skips_failures(monkeypatch, capsys):
    home = page_html("Home") + links_html(["/a", "/b", "https://x.com/", "/c", "/d", "/e", "/f", "/g"])
    requested = fake_web(
        monkeypatch,
        {
            "https://acme.dev/": home,
            "https://acme.dev/llms.txt": home,  # an app shell served for every path is not an llms.txt
            "https://acme.dev/a": home,  # an alias of the home page
            "https://acme.dev/b": urllib.error.HTTPError("https://acme.dev/b", 404, "Not Found", None, None),
            "https://acme.dev/c": "<form><p>Log in</p></form>",
            "https://acme.dev/d": page_html("D"),
            "https://acme.dev/e": page_html("E"),
            "https://acme.dev/f": page_html("F"),
            "https://acme.dev/g": page_html("G"),
        },
    )
    pages = fetch_pages(["https://acme.dev/"], max_pages=4)
    assert [p.url for p in pages] == [
        "https://acme.dev/", "https://acme.dev/d", "https://acme.dev/e", "https://acme.dev/f"
    ]
    assert requested == ["https://acme.dev/", "https://acme.dev/robots.txt", "https://acme.dev/llms.txt"] + [
        f"https://acme.dev/{c}" for c in "abcdef"
    ]
    out = capsys.readouterr().out
    assert "[brand] skipped https://acme.dev/a: same text as https://acme.dev/\n" in out
    assert "[brand] skipped https://acme.dev/b: HTTP 404 Not Found\n" in out
    assert "[brand] skipped https://acme.dev/c: only 6 characters of text\n" in out


ROBOTS = "User-agent: *\nDisallow: /\n\nUser-agent: media-harness\nDisallow: /private/\n"
LLMS = "# Acme\n\n- [Pricing](https://acme.dev/pricing): plans\n- [Notes](/private/notes)\n"


def test_fetch_pages_honors_robots_txt_and_prefers_llms_txt_links(monkeypatch, capsys):
    requested = fake_web(
        monkeypatch,
        {
            "https://acme.dev/": page_html("Home") + links_html(["/blog", "/private/x", "/about"]),
            "https://acme.dev/robots.txt": (ROBOTS, "text/plain"),
            "https://acme.dev/llms.txt": (LLMS, "text/markdown; charset=utf-8"),
            "https://acme.dev/pricing": page_html("Pricing"),
            "https://acme.dev/blog": page_html("Blog"),
            "https://acme.dev/about": page_html("About"),
        },
    )
    pages = fetch_pages(["https://acme.dev/"], max_pages=5)
    assert [p.url for p in pages] == [
        "https://acme.dev/",
        "https://acme.dev/llms.txt",
        "https://acme.dev/pricing",
        "https://acme.dev/blog",
        "https://acme.dev/about",
    ]
    assert pages[1] == Page("https://acme.dev/llms.txt", "Acme", LLMS.strip())
    assert not [u for u in requested if "/private/" in u]
    out = capsys.readouterr().out
    assert "[brand] skipped https://acme.dev/private/notes: disallowed by robots.txt\n" in out
    assert "[brand] skipped https://acme.dev/private/x: disallowed by robots.txt\n" in out


def test_fetch_pages_follows_no_links_when_robots_txt_is_unreachable(monkeypatch, capsys):
    error = urllib.error.HTTPError("https://acme.dev/robots.txt", 503, "Service Unavailable", None, None)
    requested = fake_web(
        monkeypatch,
        {"https://acme.dev/": page_html("Home") + links_html(["/a"]), "https://acme.dev/robots.txt": error},
    )
    pages = fetch_pages(["https://acme.dev/"], max_pages=3)
    assert [p.url for p in pages] == ["https://acme.dev/"]
    assert requested == ["https://acme.dev/", "https://acme.dev/robots.txt"]
    assert "not following links: https://acme.dev/robots.txt is unreachable" in capsys.readouterr().out


def test_fetch_pages_raises_when_a_listed_url_fails(monkeypatch):
    fake_web(monkeypatch, {"https://acme.dev/": urllib.error.URLError("Name or service not known")})
    with pytest.raises(IngestError, match=r"could not read https://acme\.dev/: Name or service not known"):
        fetch_pages(["https://acme.dev/"])


# ingest_brand

BRAND_MD = """# Acme Rockets

Acme Rockets builds reusable rockets for small teams.
We launched the Roadrunner engine in 2024.
We never comment on rival launch dates.
"""

FOLLOWUP = "When did Acme launch the Roadrunner engine?"
ENTITY_ID = "entity-acme-rockets"
OCT_1 = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
OCT_15 = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)


class StubLLM(LLM):
    """Returns a fixed follow-up question, then fixed answers, and records each prompt."""

    def __init__(self, answers: list[dict]):
        self.answers = answers
        self.prompts: list[str] = []

    def complete_json(self, system, prompt, mock=None):
        self.prompts.append(prompt)
        if '{"questions"' in prompt:
            return {"questions": [FOLLOWUP]}
        return {"answers": self.answers}


def brand_file(tmp_path, text: str = BRAND_MD):
    md = tmp_path / "acme.md"
    md.write_text(text, encoding="utf-8")
    return md


def stored(store, cls) -> dict:
    """Every stored note that loads as `cls`, by id, whatever kind string the store files it under."""
    items = (load(m) for m in store.list_memories())
    return {item.id: item for item in items if isinstance(item, cls)}


def answer(question: str, text: str, quote: str = "", source_url: str = "") -> dict:
    return {"question": question, "answer": text, "quote": quote, "source_url": source_url}


def test_ingest_brand_keeps_only_verified_answers(tmp_path):
    md = brand_file(tmp_path)
    url = md.resolve().as_uri()
    what_it_is = "Acme Rockets builds reusable rockets for small teams."
    launch_quote = "we launched the  Roadrunner engine in 2024"
    llm = StubLLM(
        [
            answer(CORE_QUESTIONS[0], "It builds reusable rockets for small teams.", what_it_is, url),
            answer(CORE_QUESTIONS[4], "Acme serves 10,000 customers.", "Acme serves 10,000 customers worldwide.", url),
            answer(CORE_QUESTIONS[6], "not stated"),
            answer(CORE_QUESTIONS[1], "Small teams.", "reusable rockets for small teams", "https://acme.example/about"),
            answer(CORE_QUESTIONS[3], "Acme sells the Roadrunner engine and the Coyote 9 booster.",
                   "We launched the Roadrunner engine in 2024.", url),
            answer(FOLLOWUP, "Acme launched the Roadrunner engine in 2024.", launch_quote, url),
            answer(CORE_QUESTIONS[0], "It never comments on rival launch dates.",
                   "We never comment on rival launch dates.", url),
        ]
    )
    store = FakeStore()

    result = ingest_brand(store, llm, [str(md)], "Acme Rockets", now=OCT_15)

    assert result == {
        "entity": ENTITY_ID,
        "facts": 2,
        "superseded": 0,
        "unchanged": 0,
        "dropped": [
            {"question": CORE_QUESTIONS[4], "reason": "quote not found"},
            {"question": CORE_QUESTIONS[6], "reason": "not stated"},
            {"question": CORE_QUESTIONS[1], "reason": "unknown source_url"},
            {"question": CORE_QUESTIONS[3], "reason": "answer not supported by quote"},
            {"question": CORE_QUESTIONS[0], "reason": "duplicate question"},
        ],
        "pages": [url],
    }
    expected = [
        Fact(id=fact_id(ENTITY_ID, CORE_QUESTIONS[0]), subject=ENTITY_ID, question=CORE_QUESTIONS[0],
             answer="It builds reusable rockets for small teams.", quote=what_it_is, source_url=url,
             as_of="2026-10-15", valid_until="2026-11-14", status="current"),
        Fact(id=fact_id(ENTITY_ID, FOLLOWUP), subject=ENTITY_ID, question=FOLLOWUP,
             answer="Acme launched the Roadrunner engine in 2024.", quote=launch_quote, source_url=url,
             as_of="2026-10-15", valid_until="2026-11-14", status="current"),
    ]
    assert stored(store, Fact) == {f.id: f for f in expected}
    assert stored(store, Entity) == {
        ENTITY_ID: Entity(id=ENTITY_ID, name="Acme Rockets", summary="It builds reusable rockets for small teams.",
                          entity_type="brand", aliases=["Acme Rockets"], updated="2026-10-15")
    }
    assert len(store.items) == 3

    # The follow-up from the first call is asked in the second, and every page is labeled with its URL.
    assert FOLLOWUP in llm.prompts[1]
    assert all(q in llm.prompts[1] for q in CORE_QUESTIONS)
    assert f'<page url="{url}">' in llm.prompts[1]


def test_fact_ids_are_keyed_by_subject_and_normalized_question():
    assert fact_id(ENTITY_ID, "Who is it for?") == fact_id(ENTITY_ID, "  who IS it\nfor?")
    assert fact_id(ENTITY_ID, "Who is it for?") != fact_id("entity-other", "Who is it for?")
    assert ID_PATTERN.match(fact_id(ENTITY_ID, "Who is it for?"))


def test_ingest_brand_supersedes_a_changed_answer(tmp_path):
    md = brand_file(tmp_path)
    url = md.resolve().as_uri()
    question = CORE_QUESTIONS[3]
    store = FakeStore()
    first = answer(question, "It launched the Roadrunner engine in 2024.",
                   "We launched the Roadrunner engine in 2024.", url)
    ingest_brand(store, StubLLM([first]), [str(md)], "Acme Rockets", now=OCT_1)
    [old] = stored(store, Fact).values()

    md.write_text(BRAND_MD + "We launched the Coyote booster in 2026.\n", encoding="utf-8")
    second = answer(question, "It launched the Coyote booster in 2026.", "We launched the Coyote booster in 2026.", url)
    result = ingest_brand(store, StubLLM([second]), [str(md)], "Acme Rockets", now=OCT_15)

    assert (result["facts"], result["superseded"], result["unchanged"]) == (1, 1, 0)
    facts = stored(store, Fact)
    current = facts.pop(fact_id(ENTITY_ID, question))
    [archived] = facts.values()
    assert archived == replace(old, id=archived.id, status="superseded")
    assert ID_PATTERN.match(archived.id) and archived.id != old.id
    assert current == Fact(id=old.id, subject=ENTITY_ID, question=question,
                           answer="It launched the Coyote booster in 2026.",
                           quote="We launched the Coyote booster in 2026.", source_url=url,
                           as_of="2026-10-15", valid_until="2026-11-14", supersedes=archived.id, status="current")


def test_ingest_brand_refreshes_dates_of_an_unchanged_answer(tmp_path):
    md = brand_file(tmp_path)
    url = md.resolve().as_uri()
    question = CORE_QUESTIONS[3]
    store = FakeStore()
    first = answer(question, "It launched the Roadrunner engine in 2024.",
                   "We launched the Roadrunner engine in 2024.", url)
    ingest_brand(store, StubLLM([first]), [str(md)], "Acme Rockets", now=OCT_1)
    [old] = stored(store, Fact).values()

    # Same claim, written with different case and spacing.
    again = answer(question, "it launched the  Roadrunner engine in 2024.", "we launched the Roadrunner engine in 2024.", url)
    result = ingest_brand(store, StubLLM([again]), [str(md)], "Acme Rockets", now=OCT_15)

    assert (result["facts"], result["superseded"], result["unchanged"]) == (1, 0, 1)
    assert stored(store, Fact) == {old.id: replace(old, as_of="2026-10-15", valid_until="2026-11-14")}


def test_ingest_brand_falls_back_to_brand_name_when_the_first_answer_is_dropped(tmp_path):
    md = brand_file(tmp_path)
    store = FakeStore()
    result = ingest_brand(store, StubLLM([answer(CORE_QUESTIONS[0], "not stated")]), [str(md)], "Acme Rockets")
    assert result["facts"] == 0
    assert stored(store, Entity)[ENTITY_ID].summary == "Acme Rockets"


def test_ingest_brand_keeps_an_earlier_summary_when_a_rerun_verifies_none(tmp_path):
    md = brand_file(tmp_path)
    url = md.resolve().as_uri()
    store = FakeStore()
    first = answer(CORE_QUESTIONS[0], "It builds reusable rockets for small teams.",
                   "Acme Rockets builds reusable rockets for small teams.", url)
    ingest_brand(store, StubLLM([first]), [str(md)], "Acme Rockets", now=OCT_1)
    ingest_brand(store, StubLLM([answer(CORE_QUESTIONS[0], "not stated")]), [str(md)], "Acme Rockets", now=OCT_15)
    entity = stored(store, Entity)[ENTITY_ID]
    assert (entity.summary, entity.updated) == ("It builds reusable rockets for small teams.", "2026-10-15")


def test_ingest_brand_rejects_a_brand_name_without_ascii_letters_or_digits(tmp_path):
    with pytest.raises(IngestError, match="needs at least one ASCII letter or digit"):
        ingest_brand(FakeStore(), StubLLM([]), [str(brand_file(tmp_path))], "!!!")


def test_ingest_brand_with_mock_llm_writes_a_verified_fact(tmp_path):
    md = brand_file(tmp_path)
    store = FakeStore()
    result = ingest_brand(store, MockLLM(), [str(md)], "Acme Rockets")
    assert result["facts"] >= 1
    facts = stored(store, Fact).values()
    assert facts and all(verify_quote(f.quote, BRAND_MD) for f in facts)
    # The mock answers the first core question with the first full sentence, skipping the heading.
    assert stored(store, Entity)[ENTITY_ID].summary == "Acme Rockets builds reusable rockets for small teams."


def test_question_brand_caps_material_and_gives_short_pages_their_full_text():
    long_page = Page("https://acme.dev/long", "Long", "word " * 30_000)
    short_page = Page("https://acme.dev/short", "Short", "The short page says something specific.")
    llm = StubLLM([])
    brand.question_brand(llm, [long_page, short_page], "Acme", n_followups=0)
    [prompt] = llm.prompts
    assert short_page.text in prompt
    assert len(prompt) < brand.MATERIAL_BUDGET + 3_000
