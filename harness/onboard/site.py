"""Read a brand's site as text: the homepage and up to ten linked pages on the same domain.

Politeness: every request goes through `PoliteClient`, which waits its turn on the per-host throttle in
`~/.cache/media-harness/throttle/` that every process on this Mac shares. Site requests use a 2-second interval
there, half the rate the fxtwitter readers use, and each host's robots.txt is honored for our User-Agent. Pages
are cached for a day under `out/onboard/cache/`, so rerunning an onboarding doesn't fetch them again.
"""

import re
import urllib.parse
from collections import Counter
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser

from harness import config
from harness.context.http import USER_AGENT, FetchError, PoliteClient

MAX_LINKED_PAGES = 10
SITE_INTERVAL_S = 2.0
CACHE_DIR = config.OUT_DIR / "onboard" / "cache"
CACHE_MAX_AGE_S = 86400
# Paths no product description lives on: sign-in, legal and job pages, feeds and build assets.
SKIP_SEGMENTS = {"login", "log-in", "signin", "sign-in", "signup", "sign-up", "register", "auth", "account",
                 "dashboard", "careers", "jobs", "legal", "privacy", "privacy-policy", "terms", "terms-of-service",
                 "cookies", "cookie-policy", "status", "contact", "sitemap", "search", "rss", "feed", "cdn-cgi",
                 "_next", "static", "assets", "images", "img", "fonts", "favicon", "slack", "discord", "discuss",
                 "forum", "chat", "community", "github"}
# Path words that usually mark a page about the product; those pages are read first.
PRODUCT_SEGMENTS = {"product", "products", "features", "feature", "platform", "pricing", "docs", "documentation",
                    "about", "company", "customers", "solutions", "use-cases", "why", "how-it-works", "enterprise",
                    "integrations", "developers", "overview"}
PAGE_EXTENSIONS = {"", ".html", ".htm"}
# At most this many pages under one first path segment, so five customer stories don't fill the ten slots.
MAX_PER_SECTION = 3
# Nav and footer text is menus and legal lines; their links are still followed.
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "nav", "footer"}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "br", "section", "article", "header",
              "main", "tr", "td", "th", "table", "blockquote", "pre", "dt", "dd", "figcaption", "summary", "details"}


class SiteError(RuntimeError):
    """The site's homepage couldn't be read, or robots.txt forbids it. The message says which and why."""


@dataclass
class Page:
    url: str
    title: str
    text: str  # title, meta description and visible body text, one block per line
    fetched_at: str


# ---------------------------------------------------------------- HTML to text


class _Extractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.in_title = False
        self.title = ""
        self.description = ""
        self.lines: list[str] = [""]
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title" and not self.skip_depth and not self.title:
            self.in_title = True  # the page's title, not an SVG's <title> inside the body
        elif tag == "meta" and (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
            self.description = self.description or (a.get("content") or "").strip()
        if tag == "a" and a.get("href"):
            self.links.append(urllib.parse.urljoin(self.base_url, a["href"]))
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        self._boundary(tag)

    def _boundary(self, tag):
        # A block tag starts a new line. Any other tag gets a space, so "Log in<a>Get started</a>" stays two words.
        if tag in BLOCK_TAGS:
            self.lines.append("")
        else:
            self.lines[-1] += " "

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        else:
            self._boundary(tag)

    def handle_startendtag(self, tag, attrs):
        # A void tag (<br/>, <meta/>) opens nothing, so it mustn't change skip depth.
        if tag in SKIP_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip_depth:
            self.lines[-1] += data


def _tidy(line: str) -> str:
    """Collapse whitespace, and drop the space a tag boundary left before punctuation ("database , the")."""
    line = " ".join(line.split())
    return re.sub(r"\(\s+", "(", re.sub(r"\s+([,.;:!?)])", r"\1", line))


def extract(html: str, base_url: str) -> tuple[str, str, list[str]]:
    """(title, text, links) for one HTML page. Text holds the title, the meta description and every visible
    line outside nav, footer, scripts and styles, whitespace collapsed and repeated lines dropped. Links are
    absolute, in page order, from <a href> only."""
    p = _Extractor(base_url)
    p.feed(html)
    p.close()
    title = " ".join(p.title.split())
    out, seen = [], set()
    for line in [title, " ".join(p.description.split())] + [_tidy(x) for x in p.lines]:
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return title, "\n".join(out), p.links


# ---------------------------------------------------------------- which links to follow


def _base(host: str) -> str:
    return host.lower().removeprefix("www.")


def _canonical(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def pick_links(links: list[str], home: str, n: int = MAX_LINKED_PAGES) -> list[str]:
    """Up to `n` pages to read after the homepage, from its links.

    A page qualifies when it is http(s), on the homepage's domain or a subdomain of it, at most two path segments
    deep (a subdomain's name counts as one), an HTML path rather than an asset, and on no SKIP_SEGMENTS path.
    Order: one-segment pages with a PRODUCT_SEGMENTS word, then two-segment pages with one, then other one-segment
    pages, then other two-segment pages; within each group, the order the homepage links them. At most
    MAX_PER_SECTION pages share a first segment."""
    home_parts = urllib.parse.urlsplit(home)
    base = _base(home_parts.netloc)
    home_key = _canonical(home)
    seen, ranked = set(), []
    for order, link in enumerate(links):
        parts = urllib.parse.urlsplit(link)
        host = parts.netloc.lower()
        if parts.scheme not in ("http", "https") or not (_base(host) == base or host.endswith("." + base)):
            continue
        key = _canonical(link)
        if key == home_key or key in seen:
            continue
        segments = [s for s in parts.path.lower().split("/") if s]
        sub = host.removesuffix(base).rstrip(".").removeprefix("www").strip(".")
        if sub:
            segments = [sub] + segments
        ext = re.search(r"\.[a-z0-9]{1,5}$", segments[-1]) if segments else None
        if not segments or len(segments) > 2 or (ext and ext.group(0) not in PAGE_EXTENSIONS):
            continue
        if any(s in SKIP_SEGMENTS for s in segments):
            continue
        seen.add(key)
        product = any(s in PRODUCT_SEGMENTS for s in segments)
        tier = (0 if product else 2) + len(segments) - 1
        ranked.append((tier, order, key, segments[0]))
    picked, per_section = [], Counter()
    for _, _, key, section in sorted(ranked):
        if len(picked) == n:
            break
        if per_section[section] < MAX_PER_SECTION:
            per_section[section] += 1
            picked.append(key)
    return picked


# ---------------------------------------------------------------- fetching


Getter = Callable[[str], tuple[str, str]]  # url -> (body, fetched_at); raises FetchError


def polite_getter(client: PoliteClient | None = None) -> Getter:
    client = client or PoliteClient(min_interval_s=SITE_INTERVAL_S, cache_dir=CACHE_DIR, max_retries=2)

    def get(url: str) -> tuple[str, str]:
        resp = client.get(url, max_age_s=CACHE_MAX_AGE_S)
        return resp.body, resp.fetched_at

    return get


class _Robots:
    """robots.txt per host, read once. A 4xx means no rules (RFC 9309); any other failure stops the site."""

    def __init__(self, get: Getter):
        self.get = get
        self.parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.lower()
        if host not in self.parsers:
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                body, _ = self.get(robots_url)
            except FetchError as e:
                if not re.search(r"HTTP 4\d\d", str(e)):
                    raise SiteError(f"can't read {robots_url} to check what it allows: {e}") from e
                self.parsers[host] = None
            else:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(body.splitlines())
                self.parsers[host] = rp
        rp = self.parsers[host]
        return rp is None or rp.can_fetch(USER_AGENT, url)


def read_site(url: str, get: Getter | None = None, max_linked: int = MAX_LINKED_PAGES) -> tuple[list[Page], dict]:
    """The homepage and up to `max_linked` linked pages as text, and a log of what was skipped and why.

    A homepage that can't be fetched, or whose robots.txt disallows it or can't be read, raises SiteError. A linked
    page that fails, or whose host's robots.txt can't be read, is logged and skipped."""
    get = get or polite_getter()
    home = url if urllib.parse.urlsplit(url).scheme else "https://" + url
    robots = _Robots(get)
    log: dict = {"skipped": {}}
    if not robots.allowed(home):
        raise SiteError(f"robots.txt disallows {home} for {USER_AGENT}")
    try:
        body, fetched_at = get(home)
    except FetchError as e:
        raise SiteError(f"can't read the homepage {home}: {e}") from e
    title, text, links = extract(body, home)
    pages = [Page(home, title, text, fetched_at)]
    for link in pick_links(links, home, max_linked):
        try:
            allowed = robots.allowed(link)
        except SiteError as e:  # a subdomain whose robots.txt is down (perf.vllm.ai answered 522) costs only its pages
            log["skipped"][link] = str(e)
            continue
        if not allowed:
            log["skipped"][link] = "robots.txt disallows it"
            continue
        try:
            body, fetched_at = get(link)
        except FetchError as e:
            log["skipped"][link] = str(e)
            continue
        t, x, _ = extract(body, link)
        pages.append(Page(link, t, x, fetched_at))
    return pages, log
