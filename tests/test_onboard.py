"""Brand onboarding: baselines, exemplars, watch-list ranking, the site reader, the claim check and the profile writer.

Every reader and page here is fake; nothing touches the network."""

import json
import random
import tomllib
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest

from harness import config
from harness.context import snowflake
from harness.context.http import FetchError
from harness.context.xreader import XPost
from harness.corpus.load import Corpus, Item
from harness.llm import MockLLM
from harness.onboard import claims, history, profile, site, validate
from harness.sources.watch import parse_watch
from harness.watcher import watchlist

READ = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
BRAND = "acme"


def sid(at: datetime, n: int = 0) -> str:
    """A Snowflake id that decodes to `at`, plus `n` so two posts in the same millisecond differ."""
    return str(((int(at.timestamp() * 1000) - 1288834974657) << 22) + n)


def post(text="a post with some words", *, days_ago=5.0, author=BRAND, likes=0, reposts=0, replies=0, quotes=0,
         replying_to=None, quoted_author=None, repost=False, context=False, media=(), links=0, n=0, read=READ,
         views=None) -> XPost:
    at = READ - timedelta(days=days_ago)
    pid = sid(at, n)
    return XPost(id=pid, text=text, author_handle=author, created_at=snowflake.to_datetime(pid),
                 fetched_at=read.isoformat(), likes=likes, reposts=reposts, replies=replies, quotes=quotes, views=views,
                 replying_to=replying_to, replying_to_status="1" if replying_to else None,
                 quoted_id="9" if quoted_author else None, quoted_author=quoted_author, is_repost=repost,
                 reposted_by=BRAND if repost else None, timeline_context=context, media_types=list(media),
                 n_links=links)


# ---------------------------------------------------------------- splitting a timeline


def test_split_sorts_each_status_once_and_drops_context_posts():
    orig = post("launch day", days_ago=3, n=1)
    thread = post("and another thing", days_ago=3, replying_to="Acme", n=2)
    reply = post("@bob thanks", days_ago=2, replying_to="bob", n=3)
    rp = post("their news", days_ago=1, author="partner", repost=True, n=4)
    ctx = post("what bob said", days_ago=2, author="bob", context=True, n=5)
    stale = XPost(**(orig.__dict__ | {"likes": 1, "fetched_at": (READ - timedelta(hours=1)).isoformat()}))
    fresh = XPost(**(orig.__dict__ | {"likes": 9}))
    tl = history.split("@acme", [stale, thread, reply, rp, ctx, fresh])
    assert [p.id for p in tl.originals] == [orig.id] and tl.originals[0].likes == 9  # the later read wins
    assert [p.id for p in tl.continuations] == [thread.id]
    assert [p.id for p in tl.replies] == [reply.id]
    assert [p.id for p in tl.reposts] == [rp.id]


# ---------------------------------------------------------------- baselines


def test_baselines_rates_use_each_kinds_own_window_and_medians_skip_young_posts():
    posts = [
        post("photo post words", days_ago=10, likes=10, media=["photo"], views=100, n=1),
        post("video post words", days_ago=8, likes=20, reposts=10, media=["video"], views=300, n=2),
        post("link post words", days_ago=6, likes=4, reposts=8, links=1, n=3),
        post("quote post words", days_ago=4, likes=6, quoted_author="other", n=4),
        post("young post words", days_ago=1, likes=500, n=5),  # under 48 h old: in rates, out of medians
        post("@x reply", days_ago=2, replying_to="x", n=6),
        post("their post", days_ago=4, author="x", repost=True, n=7),
        post("their post 2", days_ago=3, author="y", repost=True, n=8),
    ]
    b = history.baselines(history.split(BRAND, posts))
    assert (b.originals, b.replies, b.reposts, b.continuations) == (5, 1, 2, 0)
    assert b.originals_per_day == 0.5  # 5 over the 10 days back to the oldest original
    assert b.replies_per_day == 0.5  # 1 over 2 days
    assert b.reposts_per_day == 0.5  # 2 over the 4 days back to the oldest repost
    assert b.mature_originals == 4
    assert b.median_engagement == 11.0  # engagements 10, 30, 12, 6
    assert b.median_likes == 8.0  # likes 10, 20, 4, 6
    assert b.median_views == 200.0  # only posts with a view count
    assert b.formats == {"text_only": 0.2, "photo": 0.2, "video": 0.2, "gif": 0.0, "link": 0.2, "quote": 0.2}
    assert b.media_share == 0.4


def test_rate_window_is_at_least_a_day():
    b = history.baselines(history.split(BRAND, [post(days_ago=0.25, n=1), post(days_ago=0.1, n=2)]))
    assert b.originals_per_day == 2.0


def test_a_pinned_post_years_older_than_the_rest_doesnt_stretch_the_rate():
    pinned = post("our pinned intro post", days_ago=900, n=1)
    recent = [post(f"post number {i} here", days_ago=10 - i, n=10 + i) for i in range(10)]  # one a day for 10 days
    assert [p.id for p in history.recent_run([pinned] + recent)] == [p.id for p in recent]
    assert history.baselines(history.split(BRAND, [pinned] + recent)).originals_per_day == 1.0


def test_a_long_silence_mid_read_stays_in_the_rate():
    # Gaps of 40, 1 and 50 days: the first gap is long but not ten times the 40-day median, so nothing is a stray.
    posts = [post("a b c", days_ago=d, n=i) for i, d in enumerate([100, 60, 59, 9])]
    assert len(history.recent_run(posts)) == 4


def test_baselines_refuse_an_empty_read():
    with pytest.raises(ValueError, match="no posts"):
        history.baselines(history.split(BRAND, []))


# ---------------------------------------------------------------- exemplars


def test_exemplars_are_measured_against_the_accounts_own_median():
    base = [post(f"ordinary post number {i}", days_ago=10 + i, likes=10, n=i) for i in range(7)]  # median 10
    double = post("exactly twice the median here today", days_ago=5, likes=20, n=10)  # 2.0x: the bound is inclusive
    under = post("just under twice the median sadly", days_ago=5, likes=19, n=11)
    big = post("five times the median wow look", days_ago=6, likes=50, n=12)
    young = post("viral but still young post", days_ago=1, likes=900, n=13)  # not settled
    reply = post("@x a reply that did great", days_ago=5, likes=500, replying_to="x", n=14)
    rp = post("a repost that did great", days_ago=5, likes=500, author="x", repost=True, n=15)
    no_words = post("https://t.co/abc @x", days_ago=5, likes=100, n=16)  # nothing to show the voice
    caption = post("Love that guy https://t.co/x", days_ago=5, likes=900, n=17)  # an image's caption
    tl = history.split(BRAND, base + [double, under, big, young, reply, rp, no_words, caption])
    ex = history.exemplars(tl)
    assert [e.id for e in ex] == [big.id, double.id]
    assert [e.multiple for e in ex] == [5.0, 2.0]
    assert ex[0].url == f"https://x.com/{BRAND}/status/{big.id}"


def test_exemplars_cap_and_break_ties_by_id():
    base = [post(f"ordinary post number {i}", days_ago=20 + i, likes=1, n=i) for i in range(9)]
    strong = [post(f"this strong post number {i} reads well here", days_ago=10, likes=10, n=20 + i) for i in range(7)]
    ex = history.exemplars(history.split(BRAND, base + strong), k=5)
    assert [e.id for e in ex] == sorted((p.id for p in strong), key=int)[:5]


def test_a_zero_median_counts_as_one():
    tl = history.split(BRAND, [post(f"quiet post number {i}", days_ago=10 + i, n=i) for i in range(3)]
                       + [post("two likes is twice nothing at all", days_ago=5, likes=2, n=9)])
    assert [e.multiple for e in history.exemplars(tl)] == [2.0]


# ---------------------------------------------------------------- the accounts it engages with


def test_engagements_skip_reply_addressing_and_self_mentions():
    reply = post("@bob @carol thanks @dave", days_ago=2, replying_to="bob", n=1)
    orig = post("hi @Acme and @erin, see @erin", days_ago=3, quoted_author="frank", n=2)
    self_quote = post("as we said", days_ago=3, quoted_author="ACME", n=3)
    rp = post("news", days_ago=4, author="gina", repost=True, n=4)
    found = {(e.account, e.kind) for e in history.engagements(history.split(BRAND, [reply, orig, self_quote, rp]))}
    assert found == {("bob", "reply"), ("dave", "mention"), ("erin", "mention"), ("frank", "quote"), ("gina", "repost")}


def test_ranking_counts_posts_then_kinds_then_recency_then_handle():
    E = history.Engagement
    t = READ - timedelta(days=3)
    found = [
        E("Zed", "mention", "1", t), E("zed", "repost", "2", t), E("ZED", "mention", "3", t),  # 3 posts
        E("bee", "reply", "4", t), E("bee", "mention", "4", t), E("bee", "quote", "5", t),  # 2 posts, 3 kinds
        E("cat", "mention", "6", t), E("cat", "mention", "7", t + timedelta(days=1)),  # 2 posts, 1 kind, newer
        E("ant", "mention", "8", t), E("ant", "mention", "9", t),  # 2 posts, 1 kind, older
        E("dog", "mention", "10", t), E("dog", "mention", "11", t),  # ties ant: handle breaks it
        E("acme", "mention", "12", t), E("acme", "mention", "13", t), E("acme", "mention", "14", t),  # the brand
        E("eel", "mention", "15", t),
    ]
    ranked = history.rank_accounts(found, exclude=("@Acme",))
    assert [c.handle for c in ranked] == ["ZED", "bee", "cat", "ant", "dog", "eel"]
    assert ranked[0].events == 3 and ranked[0].kinds == {"mention": 2, "repost": 1}
    assert ranked[1].events == 2 and ranked[1].kinds == {"reply": 1, "quote": 1, "mention": 1}
    # Handles match ignoring case; each spelling appears once, so the first in sort order shows.
    cut = history.watchlist(ranked, max_n=3, min_events=2)
    assert [c.handle for c in cut] == ["ZED", "bee", "cat"]
    assert [c.handle for c in history.watchlist(ranked, max_n=24, min_events=2)] == ["ZED", "bee", "cat", "ant", "dog"]


def test_bio_mentions_flag_accounts_whose_bio_names_the_brand():
    staff = XPost(**(post("their news", author="dana", repost=True, n=1).__dict__ | {"author_description": "eng @Acme"}))
    other = XPost(**(post("hello", author="eli", repost=True, n=2).__dict__ | {"author_description": "I like acme-ish things"}))
    own = XPost(**(post("ours", n=3).__dict__ | {"author_description": "We are @acme"}))
    assert history.bio_mentions([staff, other, own], "acme") == {"dana"}
    assert history.bio_mentions([staff, other, own], "acme", name="acme-ish") == {"dana", "eli"}


def test_most_common_spelling_wins():
    E, t = history.Engagement, READ
    ranked = history.rank_accounts([E("Vercel", "mention", "1", t), E("vercel", "mention", "2", t),
                                    E("Vercel", "repost", "3", t)])
    assert ranked[0].handle == "Vercel" and ranked[0].events == 3


# ---------------------------------------------------------------- the site


HOME = """<html><head><title>Acme | Queues for developers</title>
<meta name="description" content="Acme runs background jobs for web apps."><script>var x = 1;</script></head>
<body><nav><a href="/features">Features</a><a href="/login">Log in</a></nav>
<h1>Queues for <span>developers</span></h1><p>Acme retries failed jobs up to 25 times<a href="/pricing">Pricing</a>.</p>
<svg><title>logo</title></svg>
<a href="/blog/launch">Launch</a><a href="/docs/intro/deep/page">deep</a><a href="https://other.com/x">other</a>
<a href="/logo.png">img</a><a href="https://docs.acme.dev/">Docs</a><a href="/about#team">About</a>
<footer>Copyright Acme <a href="/privacy">Privacy</a></footer></body></html>"""


def test_extract_keeps_visible_text_title_and_description_and_every_link():
    title, text, links = site.extract(HOME, "https://acme.dev/")
    assert title == "Acme | Queues for developers"
    lines = text.splitlines()
    assert lines[:2] == ["Acme | Queues for developers", "Acme runs background jobs for web apps."]
    assert "Queues for developers" in lines and "Acme retries failed jobs up to 25 times Pricing." in lines
    assert "logo" not in text and "var x" not in text and "Copyright" not in text and "Log in" not in text
    assert "https://acme.dev/features" in links and "https://acme.dev/privacy" in links  # nav and footer links count


def test_pick_links_prefers_product_pages_and_skips_assets_auth_and_other_domains():
    _, _, links = site.extract(HOME, "https://acme.dev/")
    # One-segment product pages in page order (a subdomain's name is a segment), then the two-segment blog post.
    assert site.pick_links(links, "https://acme.dev/") == [
        "https://acme.dev/features", "https://acme.dev/pricing", "https://docs.acme.dev/", "https://acme.dev/about",
        "https://acme.dev/blog/launch"]
    assert site.pick_links(links, "https://acme.dev/", n=2) == ["https://acme.dev/features", "https://acme.dev/pricing"]


def test_pick_links_caps_pages_per_section():
    links = [f"https://acme.dev/customers/c{i}" for i in range(6)] + ["https://acme.dev/team"]
    assert site.pick_links(links, "https://acme.dev") == [
        "https://acme.dev/customers/c0", "https://acme.dev/customers/c1", "https://acme.dev/customers/c2",
        "https://acme.dev/team"]


class FakeWeb:
    def __init__(self, pages: dict[str, str], robots: str | None = None):
        self.pages, self.robots, self.urls = pages, robots, []

    def __call__(self, url):
        self.urls.append(url)
        if url.endswith("/robots.txt"):
            if self.robots is None:
                raise FetchError(f"HTTP 404 from host for {url}")
            return self.robots, "2026-10-16T12:00:00+00:00"
        if url not in self.pages:
            raise FetchError(f"HTTP 500 from host for {url}")
        return self.pages[url], "2026-10-16T12:00:00+00:00"


FEATURES = "<html><head><title>Features</title></head><body><p>Acme ships cron schedules and dead-letter queues.</p></body></html>"


def test_read_site_honors_robots_and_logs_failed_pages():
    web = FakeWeb({"https://acme.dev": HOME, "https://acme.dev/features": FEATURES},
                  robots="User-agent: *\nDisallow: /pricing\n")
    pages, log = site.read_site("acme.dev", web)
    assert [p.url for p in pages] == ["https://acme.dev", "https://acme.dev/features"]
    assert log["skipped"]["https://acme.dev/pricing"] == "robots.txt disallows it"
    assert "HTTP 500" in log["skipped"]["https://acme.dev/about"]
    assert "https://acme.dev/pricing" not in web.urls


def test_read_site_refuses_a_disallowed_or_missing_homepage():
    with pytest.raises(site.SiteError, match="disallows"):
        site.read_site("https://acme.dev", FakeWeb({"https://acme.dev": HOME}, robots="User-agent: *\nDisallow: /\n"))
    with pytest.raises(site.SiteError, match="homepage"):
        site.read_site("https://acme.dev", FakeWeb({}))


def test_a_linked_subdomain_whose_robots_is_down_is_skipped():
    class SubDown(FakeWeb):
        def __call__(self, url):
            if url.startswith("https://docs.acme.dev"):
                self.urls.append(url)
                raise FetchError(f"HTTP 522 from docs.acme.dev for {url}")
            return super().__call__(url)
    pages, log = site.read_site("https://acme.dev", SubDown({"https://acme.dev": HOME, "https://acme.dev/features": FEATURES}))
    assert "https://acme.dev/features" in [p.url for p in pages]
    assert "522" in log["skipped"]["https://docs.acme.dev/"]


def test_robots_that_cannot_be_read_stop_the_site():
    class Down(FakeWeb):
        def __call__(self, url):
            raise FetchError(f"HTTP 503 from host for {url}")
    with pytest.raises(site.SiteError, match="robots.txt"):
        site.read_site("https://acme.dev", Down({}))


# ---------------------------------------------------------------- the claim check


PAGE = ("Acme | Queues for developers\nAcme runs background jobs for web apps.\n"
        "Acme retries failed jobs up to 25 times.\nBuilt for teams on PostgreSQL. 99.9% uptime.")


def test_quotes_must_appear_verbatim_once_normalized():
    assert claims.quote_found("acme  retries failed jobs", PAGE)
    assert claims.quote_found("Acme runs background jobs for web apps.", PAGE)
    assert not claims.quote_found("Acme retries jobs up to 25 times", PAGE)  # a word dropped
    assert not claims.quote_found("Queues", PAGE)  # too short to show where it came from
    assert claims.quote_found("Built for teams on PostgreSQL", "Built for teams on PostgreSQL")


@pytest.mark.parametrize("claim, problem", [
    ("It retries failed jobs up to 25 times.", None),
    ("It retries failed jobs up to 30 times.", "number '30 (time)'"),
    ("It retries failed jobs up to 25 retries.", "number '25 (retrie)'"),  # ground.unit_after drops the s
    ("Uptime is 99.9% for every team.", None),
    ("It runs on PostgreSQL and MySQL.", "name 'MySQL'"),
    ("Retries up to 25 for teams.", None),  # "25 for": a stopword says nothing about the unit
    ("The 2026 conference is for teams.", "number '2026 (conference)'"),  # no 2026 in the page at all
    ("It runs on PostgreSQL for Stripe.", "name 'Stripe'"),
    ("Teams can ask @acmehelp for help.", "name '@acmehelp'"),
])
def test_trace_requires_names_and_numbers_from_the_sources(claim, problem):
    problems = claims.trace(claim, [PAGE])
    assert (problem is None and problems == []) or (problem is not None and any(problem in p for p in problems))


def test_years_and_numbers_followed_by_stopwords_match_on_digits():
    source = "Supabase Select 2026 is sold out. The Pro plan is $25 and up."
    assert claims.trace("Select 2026 conference", [source]) == []
    assert claims.trace("Pro costs $25 per month", [source]) == []  # the source's "25 and" says nothing either
    assert claims.trace("Pro has 25 regions", ["Pro has 25 seats"]) != []  # two real units that differ


def test_trace_content_overlap_and_no_source():
    assert claims.trace("anything", []) == ["cites no source"]
    assert claims.trace("Background jobs for web apps", [PAGE], 0.6) == []
    problems = claims.trace("payments and invoicing for retailers", [PAGE], 0.6)
    assert len(problems) == 1 and "0 of 3 content words" in problems[0]


MISSION = """> Provisional until a person edits it.

# Mission: @acme on X

**What it is:**
- Background jobs for web apps, with retries up to 25 times ([acme.dev](https://acme.dev))
- Payments for retailers ([acme.dev](https://acme.dev))
- Queues with retries ([acme.dev/nope](https://acme.dev/nope))
- Queues, with retries ([post](https://x.com/acme/status/1))

**Voice and tone:**
- Short posts that open with the news ([post](https://x.com/acme/status/1))
- Mentions Stripe a lot ([post](https://x.com/acme/status/1))

**Examples, good:**
- "we shipped retries today" ([post, 3.0x its median engagement](https://x.com/acme/status/1))
- "we shipped retries yesterday" ([post, 3.0x its median engagement](https://x.com/acme/status/1))

**Don't:**
- Invent Stripe stats.
"""


def test_check_mission_traces_every_cited_line_and_ignores_fixed_rules():
    pages = {"https://acme.dev": PAGE}
    posts = {"https://x.com/acme/status/1": "We shipped retries today! Try them."}
    results = claims.check_mission(MISSION, pages, posts)
    assert [(c.section, bool(c.problems)) for c in results] == [
        ("What it is", False), ("What it is", True), ("What it is", True), ("What it is", True),
        ("Voice and tone", False), ("Voice and tone", True), ("Examples, good", False), ("Examples, good", True)]
    assert results[0].urls == ["https://acme.dev"] and results[0].text.endswith("25 times")
    assert "isn't a fetched page" in results[2].problems[0]
    assert "isn't a fetched page" in results[3].problems[0]  # a post can't back a product claim
    assert "word for word" in results[7].problems[0]


# ---------------------------------------------------------------- drafting and writing a profile


class FakeReader:
    def __init__(self, posts_tab, replies_tab=None, fail_replies=False):
        self.posts_tab, self.replies_tab, self.fail_replies, self.calls = posts_tab, replies_tab or [], fail_replies, []

    def profile_statuses(self, handle, limit=20, with_replies=False):
        self.calls.append((handle, limit, with_replies))
        if with_replies and self.fail_replies:
            raise FetchError("fxtwitter /2/profile/acme/statuses returned code 404: not found")
        return list(self.replies_tab if with_replies else self.posts_tab)[:limit]


def timeline():
    posts = [post(f"we shipped background jobs number {i} for web apps", days_ago=10 + i, likes=10, n=i) for i in range(6)]
    posts += [post("Retries up to 25 times are live for web apps", days_ago=4, likes=60, n=20),
              post("hello @partner and @partner2", days_ago=3, likes=5, n=21),
              post("again @partner", days_ago=2.5, likes=5, n=22),
              post("their launch", days_ago=2, author="partner2", repost=True, n=23)]
    replies = [post("@fan thanks!", days_ago=1, replying_to="fan", n=30), post("@fan more", days_ago=0.5, replying_to="fan", n=31)]
    return posts, replies


def pages_web():
    return FakeWeb({"https://acme.dev": HOME, "https://acme.dev/features": FEATURES})


def test_draft_and_write_produce_a_profile_that_loads_and_traces(tmp_path, monkeypatch):
    posts, replies = timeline()
    d = profile.draft("@acme", "acme-draft", reader=FakeReader(posts, replies), llm=MockLLM(), site="https://acme.dev",
                      get=pages_web(), today=date(2026, 10, 16), profiles_dir=tmp_path)
    # partner2 was mentioned and reposted (two kinds); fan's two replies are newer than partner's two mentions.
    assert [c.handle for c in d.watch] == ["partner2", "fan", "partner"]
    assert d.facts and all(claims.quote_found(f.quote, d.page_texts()[f.url]) for f in d.facts)
    target = profile.write(d, profiles_dir=tmp_path, out_dir=tmp_path / "out")
    assert sorted(p.name for p in target.iterdir()) == sorted(config.PROFILE_FILES + ("ONBOARDING.md",))
    mission = (target / "mission.md").read_text(encoding="utf-8")
    assert mission.splitlines()[0].startswith("> Provisional until a person edits it")
    watch = parse_watch(tomllib.loads((target / "observe.toml").read_text(encoding="utf-8")))
    assert watch.brand_handle == "acme" and watch.reply_targets.accounts == ["partner2", "fan", "partner"]
    for name in profile.COPIED_FILES:
        assert (target / name).read_bytes() == (config.PROFILES_DIR / "devtools" / name).read_bytes()
    results = profile.check("acme-draft", profiles_dir=tmp_path, out_dir=tmp_path / "out")
    assert results and all(not c.problems for c in results)
    assert {c.section for c in results} >= {"What it is", "Examples, good"}
    report = (target / "ONBOARDING.md").read_text(encoding="utf-8")
    assert "@partner" in report and "Median engagement" in report
    assert not list(tmp_path.glob(".acme-draft.onboarding-*"))  # the temporary directory was renamed away
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path)
    assert [w.handle for w in watchlist.load("acme-draft").handles] == ["partner2", "fan", "partner"]  # as the watcher reads it


def test_onboarding_refuses_an_existing_profile_before_reading_anything(tmp_path):
    (tmp_path / "taken").mkdir()
    reader = FakeReader(*timeline())
    with pytest.raises(profile.ProfileExists, match="never overwrites"):
        profile.draft("acme", "taken", reader=reader, llm=MockLLM(), profiles_dir=tmp_path)
    assert reader.calls == []


def test_write_refuses_a_profile_created_after_drafting(tmp_path):
    d = profile.draft("acme", "late", reader=FakeReader(*timeline()), llm=MockLLM(), profiles_dir=tmp_path,
                      today=date(2026, 10, 16))
    (tmp_path / "late").mkdir()
    (tmp_path / "late" / "mission.md").write_text("a person's mission", encoding="utf-8")
    with pytest.raises(profile.ProfileExists):
        profile.write(d, profiles_dir=tmp_path, out_dir=tmp_path / "out")
    assert (tmp_path / "late" / "mission.md").read_text(encoding="utf-8") == "a person's mission"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["late"]


def test_a_tab_that_fails_deep_is_read_as_far_as_it_goes():
    class Shallow(FakeReader):  # every read deeper than 15 statuses hits a page that 404s
        def profile_statuses(self, handle, limit=20, with_replies=False):
            self.calls.append((handle, limit, with_replies))
            if limit > 15:
                raise FetchError("HTTP 404 from api.fxtwitter.com for https://api.fxtwitter.com/2/profile/acme/statuses?cursor=x")
            return list(self.posts_tab)[:limit]
    posts = [post(f"post number {i}", days_ago=i + 1, n=i) for i in range(30)]
    reader = Shallow(posts)
    got, log = profile.read_timeline(reader, "acme", max_posts=800)
    assert [limit for _, limit, replies in reader.calls if not replies] == [800, 400, 200, 100, 50, 25, 12]
    assert log["posts_tab"] == 12 and log["posts_tab_log"]["limit"] == 12 and len(log["posts_tab_log"]["errors"]) == 6
    assert len(got) == 24  # 12 from each tab


def test_a_tab_whose_first_page_fails_raises_without_retrying():
    class Down(FakeReader):
        def profile_statuses(self, handle, limit=20, with_replies=False):
            self.calls.append(limit)
            raise FetchError("HTTP 404 from api.fxtwitter.com for https://api.fxtwitter.com/2/profile/acme/statuses")
    reader = Down([])
    with pytest.raises(FetchError):
        profile.read_timeline(reader, "acme")
    assert reader.calls == [800]


def test_a_failed_replies_tab_is_reported_not_hidden(tmp_path):
    posts, _ = timeline()
    d = profile.draft("acme", "no-replies", reader=FakeReader(posts, fail_replies=True), llm=MockLLM(),
                      profiles_dir=tmp_path, today=date(2026, 10, 16))
    assert "404" in d.read_log["replies_tab_error"] and "fan" not in [c.handle for c in d.watch]
    assert "replies tab couldn't be read" in profile.render_report(d)


def test_mission_items_without_a_usable_source_are_left_out():
    sources = {"F1": {"url": "https://acme.dev", "text": PAGE, "label": "acme.dev", "says": "", "listing": ""},
               "P1": {"url": "https://x.com/acme/status/1", "text": "we love queues", "label": "post", "says": "",
                      "listing": ""}}
    answer = {"what_it_is": [{"text": "Background jobs for web apps", "sources": ["F1"]},
                             {"text": "We love queues", "sources": ["P1"]},  # a post can't back a product claim
                             {"text": "Payments for retailers", "sources": ["F1"]},
                             {"text": "Background jobs", "sources": ["F9"]}],
              "tone": [{"text": "Short and warm", "sources": ["P1"]}], "keywords": ["queues"]}
    kept, left_out = profile.validate_items(answer, sources)
    assert kept["what_it_is"] == [{"text": "Background jobs for web apps", "sources": ["F1"]}]
    assert kept["tone"] == [{"text": "Short and warm", "sources": ["P1"]}]
    assert len(left_out) == 3


def test_keywords_must_be_strings_found_in_posts_or_pages():
    own = [post("we love postgres", n=1), post("postgres again", n=2), post("edge functions once", n=3)]
    pages = [site.Page("https://acme.dev", "Acme", "Row Level Security for all", "2026-10-16")]
    answer = {"keywords": ["Postgres", "edge functions", "row level security", {"text": "mcp", "sources": ["P1"]},
                           "postgres", "quantum"]}
    kept, dropped = profile.pick_keywords(answer, own, pages)
    assert kept == ["postgres", "row level security"]
    assert len(dropped) == 3 and "isn't a plain string" in dropped[1]


def test_memory_fact_carries_its_source_and_validity():
    f = claims.SiteFact("product", "Acme runs background jobs", "Acme runs background jobs for web apps.",
                        "https://acme.dev")
    m = claims.memory_fact(f, "@AcmeHQ", date(2026, 10, 16))
    assert (m.source_url, m.as_of, m.valid_until) == ("https://acme.dev", "2026-10-16", "2026-11-15")
    assert m.subject == "entity-acme-hq" and m.quote == f.quote and m.id.startswith("fact-site-")


def test_onboard_cli_parses(monkeypatch):
    import argparse

    from harness.onboard.cli import add_commands

    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["onboard", "--handle", "acme", "--site", "acme.dev", "--profile", "acme-x"])
    assert (args.action, args.handle, args.profile, args.max_posts) == ("draft", "acme", "acme-x", 800)
    assert parser.parse_args(["onboard", "check", "--profile", "acme-x"]).action == "check"


def test_draft_json_has_no_page_text(tmp_path):
    posts, replies = timeline()
    d = profile.draft("acme", "acme-j", reader=FakeReader(posts, replies), llm=MockLLM(), site="https://acme.dev",
                      get=pages_web(), today=date(2026, 10, 16), profiles_dir=tmp_path)
    profile.write(d, profiles_dir=tmp_path, out_dir=tmp_path / "out")
    summary = json.loads((tmp_path / "out" / "acme-j" / "draft.json").read_text(encoding="utf-8"))
    assert summary["watch"] == ["partner2", "fan", "partner"] and "pages" not in summary


# ---------------------------------------------------------------- validation on the corpus


CUT = READ - timedelta(days=10)


def item(author, days_ago, n, text="some words in a post", kind="post", parent_author=None, parent_id=None,
         reposted_by=(), sources=("author_timelines",), quoted_id=None, likes=0) -> Item:
    pid = sid(READ - timedelta(days=days_ago), n)
    return Item(status_id=pid, kind=kind, author=author, text=text, created_at=snowflake.to_datetime(pid),
                author_followers=None, author_verified=None, author_description=None, parent_id=parent_id,
                parent_author=parent_author, quoted_id=quoted_id, media_types=[], n_links=0, n_mentions=0, lang="en",
                metrics={"likes": likes, "reposts": 0, "replies": 0, "quotes": 0, "bookmarks": 0, "views": 0,
                         "fetched_at": READ.isoformat()}, sources=list(sources), reposted_by=list(reposted_by))


def corpus_fixture():
    other_post = item("bob", 5, 1, "bob ships a thing")
    quoted = item("carol", 4, 2, "carol says something")
    items = [
        item(BRAND, 20, 3, "old post with @dave"),
        item(BRAND, 12, 4, "still before the cutoff @dave"),
        item(BRAND, 9.5, 5, "just after the cutoff, hi @erin"),
        item(BRAND, 3, 6, "quoting carol here", quoted_id=quoted.status_id),
        item(BRAND, 3, 7, "and a thread reply", kind="reply", parent_author=BRAND, parent_id="1"),
        item(BRAND, 2, 8, "@bob nice one", kind="reply", parent_author="bob", parent_id=other_post.status_id,
             sources=("reply_samples",)),
        item("frank", 6, 9, "frank's post the brand reposted", reposted_by=[BRAND]),
        item("zed", 1, 10, "a peer post with words"),
        item("zed", 15, 11, "a peer post before the cutoff"),
        other_post, quoted,
    ]
    return Corpus(sorted(items, key=lambda i: int(i.status_id)), Counter(), Counter())


def test_corpus_reader_replays_a_window_of_the_brands_tabs():
    c = corpus_fixture()
    before = validate.CorpusReader(c, end=CUT).profile_statuses(BRAND, limit=100)
    assert [p.text for p in before] == ["still before the cutoff @dave", "old post with @dave"]
    assert all(p.fetched_at == CUT.isoformat() for p in before)  # read no later than the cutoff
    after = validate.CorpusReader(c, start=CUT).profile_statuses(BRAND, limit=100)
    assert "@bob nice one" not in [p.text for p in after]  # replies to others are only on the replies tab
    tabs = validate.CorpusReader(c, start=CUT).profile_statuses(BRAND, limit=100, with_replies=True)
    by_text = {p.text: p for p in tabs}
    assert by_text["frank's post the brand reposted"].is_repost
    assert by_text["quoting carol here"].quoted_author == "carol"
    assert by_text["@bob nice one"].replying_to == "bob"
    assert "and a thread reply" in by_text and "bob ships a thing" not in by_text


def test_engaged_accounts_after_the_cutoff_count_every_kind_once_per_post():
    after = validate.engaged_accounts(corpus_fixture(), BRAND, CUT, None)
    assert after == {"erin": 1, "carol": 1, "bob": 1, "frank": 1}
    assert validate.engaged_accounts(corpus_fixture(), BRAND, None, CUT) == {"dave": 2}


def test_recall_counts_accounts_and_engagements():
    r = validate.recall(["Erin", "carol", "nobody"], {"erin": 3, "carol": 1, "bob": 4})
    assert r == {"size": 3, "accounts": 3, "hit": 2, "recall": 0.667, "events": 8, "events_hit": 4, "event_recall": 0.5}
    assert validate.recall([], {})["recall"] is None


def test_popular_list_leaves_out_the_brand_and_its_own_history():
    c = corpus_fixture()
    extra = [item("zed", 14, 20, "hey @dave"), item("yan", 14, 21, "hey @dave and @zed"), item("yan", 13, 22, "@zed again")]
    c = Corpus(sorted(c.items + extra, key=lambda i: int(i.status_id)), Counter(), Counter())
    assert validate.popular_list(c, BRAND, CUT, (BRAND, "zed", "yan")) == ["dave", "zed"]


def test_fit_posts_split_the_brands_later_posts_its_engagements_and_peers():
    posts = validate.fit_posts(corpus_fixture(), BRAND, CUT, random.Random(1), (BRAND, "zed", "bob"))
    groups = {p.text: p.group for p in posts}
    assert groups == {"just after the cutoff, hi @erin": "own", "quoting carol here": "own",
                      "frank's post the brand reposted": "engaged", "carol says something": "engaged",
                      "bob ships a thing": "engaged", "a peer post with words": "peer"}


def test_off_mission_rates_and_pairs():
    def row(group, onb, dev):
        verdict = {"off": {"tool": "abstain", "reason": "off_mission"}, "reply": {"tool": "reply", "reason": ""},
                   "angle": {"tool": "abstain", "reason": "no_angle"}, "error": {"tool": "error", "reason": ""}}
        return {"group": group, "onboarded": verdict[onb], "devtools": verdict[dev]}
    rows = [row("own", "reply", "off"), row("own", "off", "off"), row("own", "angle", "reply"),
            row("engaged", "reply", "off"), row("engaged", "error", "off"), row("peer", "off", "reply")]
    rates = validate.off_mission_rates(rows, "onboarded")
    assert rates["own"] == {"judged": 3, "off_mission": 1, "rate": 0.333, "errors": 0}
    assert rates["engaged"] == {"judged": 1, "off_mission": 0, "rate": 0.0, "errors": 1}
    assert rates["brand_engaged"] == {"judged": 4, "off_mission": 1, "rate": 0.25}
    assert validate.off_mission_rates(rows, "devtools")["brand_engaged"] == {"judged": 5, "off_mission": 4, "rate": 0.8}
    assert validate.paired(rows, ("own", "engaged")) == {"pairs": 4, "only_devtools_off": 2, "only_onboarded_off": 0}


def test_judge_uses_the_planner_and_survives_a_failed_call():
    p = validate.FitPost("https://x.com/zed/status/1", "zed", "a post", "peer")
    assert validate.judge(MockLLM(), "MISSION", p)["tool"] == "reply"

    class Down(MockLLM):
        def complete_json(self, system, prompt, mock=None):
            raise RuntimeError("gateway 502")
    assert validate.judge(Down(), "MISSION", p) == {"tool": "error", "reason": "", "detail": "RuntimeError: gateway 502"}


def test_run_brand_resumes_from_its_draft_and_verdicts_without_llm_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(validate, "timeline_accounts", lambda: (BRAND, "zed", "bob"))
    web = FakeWeb({"https://acme.dev": HOME, "https://acme.dev/features": FEATURES})
    first, rows = validate.run_brand(corpus_fixture(), BRAND, "https://acme.dev", MockLLM(), tmp_path, CUT, get=web)
    assert (tmp_path / f"verdicts-{BRAND}.jsonl").exists() and len(rows) == 6
    assert first.fit["onboarded"]["brand_engaged"]["judged"] == 5

    class NoCalls(MockLLM):
        def complete_json(self, system, prompt, mock=None):
            raise AssertionError("a resumed run made an LLM call")
    again, _ = validate.run_brand(corpus_fixture(), BRAND, "https://acme.dev", NoCalls(), tmp_path, CUT, get=web)
    assert again == first
    assert validate.planned_calls(corpus_fixture(), {BRAND: ""}, CUT, tmp_path) == 0
