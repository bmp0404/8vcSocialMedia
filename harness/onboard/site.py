"""Read a brand's site as text: the homepage and up to ten linked pages on the same domain.

Politeness: every request, redirect hops included, waits its turn on the per-host throttle in
`~/.cache/media-harness/throttle/` that every process on this Mac shares, at a 2-second interval, half the rate the
fxtwitter readers use. Each host's robots.txt is honored for our User-Agent, only text pages are kept, and answers
are cached for a day under `out/onboard/cache/`, so rerunning an onboarding doesn't fetch them again.
"""

import gzip
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from harness import config
from harness.context.http import RETRY_STATUSES, USER_AGENT, FetchError, PoliteClient

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


class Redirect(Exception):
    """A 3xx answer. The getter doesn't follow it, so `read_site` can check the new URL's domain and robots.txt and
    wait a throttle turn for it, as for any other request."""

    def __init__(self, url: str, location: str):
        super().__init__(f"{url} redirects to {location}")
        self.url, self.location = url, location


Getter = Callable[[str], tuple[str, str]]  # url -> (body, fetched_at); raises FetchError or Redirect

REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5  # RFC 9309 asks robots.txt readers to follow at least five
MAX_BYTES = 5_000_000  # supabase.com's homepage was 1.3 MB of HTML; a page past this is cut, not refused
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urlopen then raises HTTPError with the 3xx and its Location header


_OPENER = urllib.request.build_opener(_NoRedirect)


def polite_getter(throttle: PoliteClient | None = None, cache_dir: Path | None = CACHE_DIR) -> Getter:
    """GET one URL, text only, without following redirects, after a turn on the shared per-host throttle.

    `PoliteClient.get` can't be used as it is: urllib follows redirects inside it, so a redirect hop is a request
    without its own throttle turn and can land on a host whose robots.txt was never read. This getter takes the
    client's throttle (`_wait_turn`, the lock every process on the Mac shares) and does the request itself. Answers,
    redirects included, are cached for CACHE_MAX_AGE_S. 429 and 5xx are retried twice with backoff."""
    throttle = throttle or PoliteClient(min_interval_s=SITE_INTERVAL_S, cache_dir=None)

    def get(url: str) -> tuple[str, str]:
        host = urllib.parse.urlsplit(url).netloc
        path = cache_dir / host.replace(":", "_") / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}.json" if cache_dir else None
        if path and path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - datetime.fromisoformat(cached["fetched_at"]).timestamp()
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                cached, age = None, None
            if cached and age is not None and age <= CACHE_MAX_AGE_S:
                if cached.get("location"):
                    raise Redirect(url, cached["location"])
                return cached["body"], cached["fetched_at"]
        record: dict = {}
        for attempt in range(3):
            throttle._wait_turn(host)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"})
                with _OPENER.open(req, timeout=25) as resp:
                    ctype = resp.headers.get_content_type()
                    if ctype not in TEXT_TYPES:
                        raise FetchError(f"{url} is {ctype}, not a text page")
                    raw = resp.read(MAX_BYTES)
                    charset = resp.headers.get_content_charset() or "utf-8"
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:  # a charset name Python doesn't know
                    text = raw.decode("utf-8", errors="replace")
                record = {"body": text}
                break
            except urllib.error.HTTPError as e:
                if e.code in REDIRECT_CODES and e.headers.get("Location"):
                    record = {"location": urllib.parse.urljoin(url, e.headers["Location"])}
                    break
                if e.code not in RETRY_STATUSES or attempt == 2:
                    raise FetchError(f"HTTP {e.code} from {host} for {url}") from e
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                if attempt == 2:
                    raise FetchError(f"{type(e).__name__} reaching {host}: {e} for {url}") from e
            time.sleep(2.0 * 2 ** attempt)
        record["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"url": url} | record), encoding="utf-8")
            os.replace(tmp, path)
        if "location" in record:
            raise Redirect(url, record["location"])
        return record["body"], record["fetched_at"]

    return get


def same_site(url: str, home: str) -> bool:
    """True when `url` is http(s) on `home`'s domain or a subdomain of it."""
    parts, base = urllib.parse.urlsplit(url), _base(urllib.parse.urlsplit(home).netloc)
    host = parts.netloc.lower()
    return parts.scheme in ("http", "https") and (_base(host) == base or host.endswith("." + base))


class _Robots:
    """robots.txt per host, read once. A 4xx means no rules (RFC 9309); any other failure raises SiteError.

    Redirects are followed up to MAX_REDIRECTS times, but only to another /robots.txt. roadmap.vllm.ai's robots.txt
    redirected to a GitHub issue, and the reader fetched that 400 KB page as if it were rules. A redirect anywhere
    else now counts as no robots.txt, which RFC 9309 treats as no rules."""

    def __init__(self, get: Getter):
        self.get = get
        self.parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _read(self, robots_url: str) -> str | None:
        url = robots_url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                return self.get(url)[0]
            except Redirect as r:
                if urllib.parse.urlsplit(r.location).path != "/robots.txt":
                    return None
                url = r.location
        raise FetchError(f"{robots_url} redirected more than {MAX_REDIRECTS} times")

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.lower()
        if host not in self.parsers:
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                body = self._read(robots_url)
            except FetchError as e:
                if not re.search(r"HTTP 4\d\d", str(e)):
                    raise SiteError(f"can't read {robots_url} to check what it allows: {e}") from e
                body = None
            if body is None:
                self.parsers[host] = None
            else:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(body.splitlines())
                self.parsers[host] = rp
        rp = self.parsers[host]
        return rp is None or rp.can_fetch(USER_AGENT, url)


class _Skip(Exception):
    """A page the site policy won't read: off the site, disallowed, or redirected too often."""


def _fetch(url: str, home: str, get: Getter, robots: _Robots) -> tuple[str, str, str]:
    """(final url, body, fetched_at), following redirects that stay on the site and robots.txt allows."""
    start = url
    for _ in range(MAX_REDIRECTS + 1):
        if not same_site(url, home):
            raise _Skip(f"redirects off the site, to {url}")
        if not robots.allowed(url):
            raise _Skip("robots.txt disallows it" if url == start else f"redirects to {url}, which robots.txt disallows")
        try:
            body, fetched_at = get(url)
            return url, body, fetched_at
        except Redirect as r:
            url = r.location
    raise _Skip(f"redirected more than {MAX_REDIRECTS} times")


def read_site(url: str, get: Getter | None = None, max_linked: int = MAX_LINKED_PAGES) -> tuple[list[Page], dict]:
    """The homepage and up to `max_linked` linked pages as text, and a log of what was skipped and why.

    Every request, redirect hops included, stays on the homepage's domain and its subdomains and is checked against
    that host's robots.txt. A homepage that can't be read for any of these reasons raises SiteError. A linked page
    that can't is logged and skipped. A page is cited by the URL it was finally read from."""
    get = get or polite_getter()
    home = url if urllib.parse.urlsplit(url).scheme else "https://" + url
    robots = _Robots(get)
    log: dict = {"skipped": {}}
    try:
        home, body, fetched_at = _fetch(home, home, get, robots)
    except _Skip as e:
        raise SiteError(f"can't read the homepage {url}: {e}") from e
    except FetchError as e:
        raise SiteError(f"can't read the homepage {url}: {e}") from e
    title, text, links = extract(body, home)
    pages = [Page(home, title, text, fetched_at)]
    read = {_canonical(home)}
    for link in pick_links(links, home, max_linked):
        try:
            final, body, fetched_at = _fetch(link, home, get, robots)
        except _Skip as e:
            log["skipped"][link] = str(e)
            continue
        except (SiteError, FetchError) as e:  # a subdomain whose robots.txt is down (perf.vllm.ai answered 522)
            log["skipped"][link] = str(e)
            continue
        if _canonical(final) in read:
            log["skipped"][link] = f"redirects to {final}, already read"
            continue
        read.add(_canonical(final))
        t, x, _ = extract(body, final)
        pages.append(Page(final, t, x, fetched_at))
    return pages, log
