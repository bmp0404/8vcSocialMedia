"""Draft a provisional mission profile from a brand's timeline and site, and write it without overwriting anything.

`draft` reads and decides; `write` puts the six profile files and ONBOARDING.md in place in one rename, and the
sources the claim check needs in `out/onboard/<profile>/`. The CLI in `cli.py` saves the site facts to the
profile's memory store after `write`, because the store is found by profile name.
"""

import json
import os
import shutil
import tomllib
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from harness import config
from harness.context.http import FetchError
from harness.context.xreader import XPost, XReader
from harness.facts import ground
from harness.llm import LLM
from harness.onboard import claims, history
from harness.onboard.claims import SiteFact
from harness.onboard.site import Getter, Page, read_site
from harness.sources.watch import parse_watch

TEMPLATE_PROFILE = "devtools"
# The profile files that start as copies of the devtools profile. They hold the audience panel, the brand phrases
# the action prompts insert, the reply framing and the post ranker, none of which a timeline or site decides.
COPIED_FILES = ("panel.json", "reply_framing.json", "prompts.toml", "qualify.toml")
MAX_POSTS = 800  # per tab; fxtwitter pages hold about 20, so up to 40 requests each
TAB_FLOOR = 20  # one page: a tab that fails at this limit can't be read at all
RECENT_POSTS = 40  # recent originals the mission call sees besides the exemplars
MAX_ITEMS = {"what_it_is": 8, "who_for": 4, "subjects": 8, "tone": 6, "goals": 4}
SECTION_OF = {"what_it_is": "What it is", "who_for": "Who it's for", "subjects": "Core subjects",
              "tone": "Voice and tone", "goals": "Goals"}
ALLOWED_IDS = {"what_it_is": "F", "who_for": "F", "subjects": "FEP", "tone": "EP", "goals": "FEP"}
MAX_KEYWORDS = 8

# The rules every brand keeps, from the devtools mission with its coding-agent examples removed.
FIXED_RULES = """**Action space:** original posts, replies, quote posts, mentions, follows. X only.

**Do:**
- Reply with a concrete take, tip or answer that stands on its own.
- Treat other companies' accounts as colleagues: congratulate launches, riff on their posts, credit good ideas.

**Don't:**
- Make false or disparaging claims about any product. Contrast approaches, never trash-talk.
- Invent stats, benchmarks, customers, testimonials or user counts. Write `[VERIFY: ...]` for any fact you can't check.
- Describe what a product does beyond what this mission's sources or the post you answer say.
- Use engagement bait ("RT if...", "drop a fire emoji", "who else...?"), hashtag spam, or "great post!" filler.
- Touch politics, punch down, or dunk on individual developers."""


class ProfileExists(FileExistsError):
    """The profile directory already exists; onboarding never overwrites one."""


@dataclass
class Draft:
    handle: str
    profile: str
    site: str | None
    today: date
    read_log: dict
    timeline: history.Timeline
    baselines: history.Baselines
    exemplars: list[history.Exemplar]
    ranked: list[history.Candidate]
    watch: list[history.Candidate]
    pages: list[Page] = field(default_factory=list)
    site_log: dict = field(default_factory=dict)
    facts: list[SiteFact] = field(default_factory=list)
    dropped_facts: list[str] = field(default_factory=list)
    items: dict[str, list[dict]] = field(default_factory=dict)  # section key -> [{"text", "sources": [ids]}]
    left_out: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    models: list[dict] = field(default_factory=list)  # filled by the CLI from modelrec
    staff: set[str] = field(default_factory=set)  # lowercased handles whose bio names the account
    sources: dict[str, dict] = field(default_factory=dict)  # id -> {"url", "text", "label"}

    def page_texts(self) -> dict[str, str]:
        return {p.url: p.text for p in self.pages}

    def post_texts(self) -> dict[str, str]:
        return {p.url: p.text for p in self.timeline.own}


def check_free(profile: str, profiles_dir: Path) -> Path:
    config._check_name(profile)
    target = profiles_dir / profile
    if target.exists():
        raise ProfileExists(f"{target} already exists; onboarding never overwrites a profile. Pick another --profile "
                            "or move that one away first")
    return target


# ---------------------------------------------------------------- reading


def _read_tab(reader: XReader, handle: str, max_posts: int, with_replies: bool) -> tuple[list[XPost], dict]:
    """One tab as far back as its pages can be read.

    `profile_statuses` raises when any page fails and keeps none of the pages before it; fxtwitter 404s on deep
    cursors (@supabase's Posts tab, 2026-10-16). So a failed read is retried with half the limit until one succeeds.
    The live reader caches every page it got, so a retry fetches only the page that failed, and only while it is
    still inside the limit. The log keeps each failure, so the report says how far the read got and why it stopped."""
    limit, errors = max_posts, []
    while True:
        try:
            posts = reader.profile_statuses(handle, limit=limit, with_replies=with_replies)
            return posts, {"read": len(posts), "limit": limit, "errors": errors}
        except FetchError as e:
            errors.append(f"limit {limit}: {e}")
            # A failed URL without a cursor is the first page, which no smaller limit avoids.
            if limit <= TAB_FLOOR or "cursor=" not in str(e):
                raise
            limit //= 2


def read_timeline(reader: XReader, handle: str, max_posts: int = MAX_POSTS) -> tuple[list[XPost], dict]:
    """Both tabs of the account, as far back as fxtwitter's pages go, up to `max_posts` each.

    The Posts tab has its originals, threads, quotes and reposts; the Posts and replies tab adds its replies to
    other accounts. A Posts tab that can't be read at all raises, since there is nothing to draft from. A replies
    tab that can't be read is kept in the log and the report says the watch list has no replies in it."""
    handle = handle.lstrip("@")
    posts, posts_log = _read_tab(reader, handle, max_posts, with_replies=False)
    log: dict = {"posts_tab": len(posts), "posts_tab_log": posts_log}
    try:
        replies, replies_log = _read_tab(reader, handle, max_posts, with_replies=True)
        log |= {"replies_tab": len(replies), "replies_tab_log": replies_log}
    except FetchError as e:
        replies = []
        log["replies_tab_error"] = str(e)
    return posts + replies, log


# ---------------------------------------------------------------- the mission call


MISSION_SYSTEM = """You draft the mission for an X account that a tool recommends posts and replies for. The account is @{handle}. A person will edit your draft, and a check drops any line that the sources it cites don't back, so cite precisely and leave out what the sources don't support.

The sources:
- F ids are statements from the company's website, each with the page it came from.
- E ids are the account's posts that did best against its own median engagement.
- P ids are the account's other recent posts.

Write these lists:
- what_it_is: what the product is and does. Restate facts of kind product or feature, citing only F ids, and add nothing the facts don't say.
- who_for: who the product is for. Restate facts of kind audience, citing only F ids. Leave the list empty if no fact says who it is for.
- subjects: the four to eight subjects the account posts about, each a short phrase and what the posts say about it, citing the P, E or F ids that show it.
- tone: three to six lines on how the posts are written: length, register, humor, emoji, how they open and what they ask readers to do. Cite the E and P ids that show it. Describe the posts instead of quoting them.
- goals: two to four things the posts try to get readers to do, such as sign up, try a feature, read the docs or come to an event, citing the ids that show it.
- keywords: three to eight short lowercase phrases that appear in the posts or pages, used to match other accounts' posts to this account's subjects.

Every item cites at least one id from the lists. Any name or number in an item must appear in what it cites.

Return JSON {{"what_it_is": [{{"text": "...", "sources": ["F1"]}}], "who_for": [], "subjects": [], "tone": [], "goals": [], "keywords": []}}."""


def _one_line(text: str, limit: int = 400) -> str:
    return " ".join((text or "").split())[:limit]


def _label(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return (parts.netloc.removeprefix("www.") + parts.path.rstrip("/")) or url


def collect_sources(facts: list[SiteFact], pages: list[Page], ex: list[history.Exemplar],
                    recent: list[XPost]) -> dict[str, dict]:
    """Number every source the mission call may cite: F for site facts, E for exemplars, P for recent posts."""
    text_of = {p.url: p.text for p in pages}
    out: dict[str, dict] = {}
    for i, f in enumerate(facts, 1):
        f.id = f"F{i}"
        out[f.id] = {"url": f.url, "text": text_of[f.url], "label": _label(f.url), "says": f.statement,
                     "listing": f"[{f.kind}] {f.statement}"}
    for i, e in enumerate(ex, 1):
        out[f"E{i}"] = {"url": e.url, "text": e.text, "label": "post", "says": _one_line(e.text),
                        "listing": f"({e.multiple}x its median) {_one_line(e.text)}"}
    for i, p in enumerate(recent, 1):
        out[f"P{i}"] = {"url": p.url, "text": p.text, "label": "post", "says": _one_line(p.text),
                        "listing": _one_line(p.text)}
    return out


def _mock_mission(sources: dict[str, dict]) -> dict:
    """The mock backend's answer: each section restates one source it may cite, word for word."""
    def first(prefixes: str) -> list[dict]:
        sid = next((s for s in sources if s[0] in prefixes), None)
        return [{"text": " ".join(sources[sid]["says"].split()[:30]), "sources": [sid]}] if sid else []
    return {"what_it_is": first("F"), "who_for": [], "subjects": first("PEF"), "tone": first("EP"),
            "goals": first("PEF"), "keywords": []}


def validate_items(answer: object, sources: dict[str, dict]) -> tuple[dict[str, list[dict]], list[str]]:
    """Keep the items whose ids are known and allowed for their section and whose text traces to them."""
    if not isinstance(answer, dict):
        raise ValueError(f"the mission call returned {type(answer).__name__}, not an object")
    kept: dict[str, list[dict]] = {}
    left_out: list[str] = []
    for key, section in SECTION_OF.items():
        rule = claims.SECTIONS[section]
        items = answer.get(key) or []
        if not isinstance(items, list):
            left_out.append(f"{section}: the answer's '{key}' isn't a list")
            items = []
        out, seen = [], set()
        for item in items:
            text = _one_line(item.get("text") if isinstance(item, dict) else "", 600)
            ids = item.get("sources") if isinstance(item, dict) else None
            ids = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
            usable = [i for i in dict.fromkeys(ids) if i in sources and i[0] in ALLOWED_IDS[key]]
            if not text:
                continue
            if not usable:
                left_out.append(f"{section}: {text!r} cites no usable source (cited {ids})")
                continue
            problems = claims.trace(text, [sources[i]["text"] for i in usable], rule.overlap)
            if problems:
                left_out.append(f"{section}: {text!r}: {'; '.join(problems)}")
            elif claims.normalize(text) not in seen and len(out) < MAX_ITEMS[key]:
                seen.add(claims.normalize(text))
                out.append({"text": text, "sources": usable})
        kept[key] = out
    return kept, left_out


def pick_keywords(answer: object, own: list[XPost], pages: list[Page]) -> list[str]:
    """The model's keywords that occur in at least two of the account's posts or in a fetched page."""
    raw = answer.get("keywords") if isinstance(answer, dict) else None
    texts = [claims.normalize(p.text) for p in own]
    page_text = claims.normalize(" ".join(p.text for p in pages))
    out = []
    for k in raw if isinstance(raw, list) else []:
        k = claims.normalize(str(k))
        if 2 <= len(k) <= 40 and k not in out and (sum(k in t for t in texts) >= 2 or k in page_text):
            out.append(k)
    return out[:MAX_KEYWORDS]


# ---------------------------------------------------------------- drafting


def draft(handle: str, profile: str, *, reader: XReader, llm: LLM, site: str | None = None, get: Getter | None = None,
          today: date | None = None, max_posts: int = MAX_POSTS, profiles_dir: Path | None = None) -> Draft:
    """Read the account's timeline and site and draft the profile. Writes nothing."""
    handle = handle.lstrip("@").strip()
    check_free(profile, profiles_dir or config.PROFILES_DIR)
    statuses, read_log = read_timeline(reader, handle, max_posts)
    tl = history.split(handle, statuses)
    base = history.baselines(tl)
    ex = history.exemplars(tl)
    ranked = history.rank_accounts(history.engagements(tl), exclude=(handle,))
    d = Draft(handle, profile, site, today or datetime.now(timezone.utc).date(), read_log, tl, base, ex, ranked,
              history.watchlist(ranked))
    d.staff = history.bio_mentions(statuses, handle)
    if site:
        d.pages, d.site_log = read_site(site, get)
        for page in d.pages:
            kept, dropped = claims.extract_facts(llm, handle, page)
            d.facts += kept
            d.dropped_facts += dropped
    exemplar_ids = {e.id for e in ex}
    recent = [p for p in tl.originals if p.id not in exemplar_ids and history.words_of(p.text)][:RECENT_POSTS]
    d.sources = collect_sources(d.facts, d.pages, ex, recent)
    listing = "\n".join(f"{sid} {s['listing']}" for sid, s in d.sources.items())
    user = f"SOURCES\n{listing}" if listing else "SOURCES\n(none)"
    answer = llm.complete_json(MISSION_SYSTEM.format(handle=handle), user, mock=lambda: _mock_mission(d.sources))
    d.items, d.left_out = validate_items(answer, d.sources)
    d.keywords = pick_keywords(answer, tl.own, d.pages)
    return d


# ---------------------------------------------------------------- rendering


def _cite(ids: list[str], sources: dict[str, dict]) -> str:
    links = dict.fromkeys(f"[{sources[i]['label']}]({sources[i]['url']})" for i in ids)
    return f" ({', '.join(links)})"


def render_mission(d: Draft) -> str:
    pages = f" and {len(d.pages)} pages of {d.site}" if d.pages else ""
    lines = [
        f"> Provisional until a person edits it: drafted by `harness onboard` on {d.today.isoformat()} from "
        f"@{d.handle}'s {len(d.timeline.own) + len(d.timeline.reposts)} timeline posts{pages}. Read ONBOARDING.md, "
        "check every cited line, then delete this line.",
        "",
        f"# Mission: @{d.handle} on X",
        "",
        "Each line under the labels from What it is to Examples, good cites the page or post it came from. After editing, rerun "
        f"`uv run python -m harness onboard check --profile {d.profile}`, which fails any line whose names, numbers "
        "or words aren't in what it cites.",
    ]
    for key, section in SECTION_OF.items():
        items = d.items.get(key) or []
        if not items:
            continue
        lines += ["", f"**{section}:**"] + [f"- {it['text']}{_cite(it['sources'], d.sources)}" for it in items]
    if d.exemplars:
        lines += ["", "**Examples, good:**"]
        lines += [f'- "{_one_line(e.text, 10_000)}" ([post, {e.multiple}x its median engagement]({e.url}))'
                  for e in d.exemplars]
    lines += ["", FIXED_RULES, ""]
    return "\n".join(lines)


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def render_observe(d: Draft, template_dir: Path) -> str:
    """observe.toml: the handle, the watch list and keywords, with every other setting from the devtools file."""
    t = tomllib.loads((template_dir / "observe.toml").read_text(encoding="utf-8"))
    # Without keywords the sources search for the brand's name, so they still return something on topic.
    queries = d.keywords[:3] or [ground.split_handle(d.handle)]
    rt = t.get("reply_targets", {})
    why = "\n".join(f"#   {c.handle}: {c.why()}" for c in d.watch) or "#   (none qualified)"
    text = f"""# Drafted by `harness onboard` on {d.today.isoformat()} from @{d.handle}'s timeline. Provisional: check the watch
# list against ONBOARDING.md before the watcher polls it. Settings other than the handle, the watch list and the
# keywords are copied from the devtools profile.

[brand]
x_handle = {json.dumps(d.handle)}

[google_news]
queries = {_toml_list([json.dumps(q) for q in queries])}
window = {json.dumps(t.get("google_news", {}).get("window", "2d"))}
per_query = {int(t.get("google_news", {}).get("per_query", 15))}

[x]
queries = {_toml_list(queries)}
lookback_hours = {int(t.get("x", {}).get("lookback_hours", 24))}
per_query = {int(t.get("x", {}).get("per_query", 25))}

[bluesky]
queries = {_toml_list(queries)}
lookback_hours = {int(t.get("bluesky", {}).get("lookback_hours", 24))}
per_query = {int(t.get("bluesky", {}).get("per_query", 25))}

[attention]
terms = {_toml_list(queries)}
baseline_days = {int(t.get("attention", {}).get("baseline_days", 14))}

[reply_targets]
# The accounts @{d.handle} engaged with in the most posts it wrote or reposted: replies to them, quotes of them,
# mentions of them and reposts of them, counted once per post. An account needs {history.WATCH_MIN_EVENTS} posts or more, and the
# list stops at {history.WATCH_MAX}, the size of the devtools list the watcher's request budget was set for.
{why}
accounts = {_toml_list([c.handle for c in d.watch])}
broad_accounts = []
keywords = {_toml_list(d.keywords)}
include_search = {"true" if rt.get("include_search", True) else "false"}
max_age_minutes = {int(rt.get("max_age_minutes", 90))}
limit = {int(rt.get("limit", 10))}
"""
    parse_watch(tomllib.loads(text))  # a typo in this template must fail here, not in the watcher
    return text


def _pct(x: float) -> str:
    return f"{x:.0%}"


def render_report(d: Draft) -> str:
    b, tl = d.baselines, d.timeline
    read = (f"{d.read_log.get('posts_tab', 0)} statuses from the Posts tab and "
            + (f"{d.read_log['replies_tab']} from the Posts and replies tab"
               if "replies_tab" in d.read_log else f"none from the Posts and replies tab ({d.read_log.get('replies_tab_error')})"))
    for tab, label in (("posts_tab_log", "Posts"), ("replies_tab_log", "Posts and replies")):
        tab_log = d.read_log.get(tab) or {}
        if tab_log.get("errors"):
            read += (f". The {label} tab failed {len(tab_log['errors'])} time(s) at deeper pages and was read up to "
                     f"{tab_log['limit']} statuses; last failure: {tab_log['errors'][-1]}")
    out = [
        f"# Onboarding report: @{d.handle}, profile `{d.profile}`",
        "",
        f"`harness onboard` drafted this profile on {d.today.isoformat()}. Nobody has checked it. Before using it, check "
        "the mission's claims against their sources, decide which watch-list accounts to keep, and read the baselines "
        "the draft was built from.",
        "",
        "## What was read",
        "",
        f"- Timeline: {read}. After merging: {b.originals} originals, {b.continuations} thread continuations, "
        f"{b.replies} replies to other accounts and {b.reposts} reposts, back to {(b.oldest_original or '')[:10] or 'no original'}.",
    ]
    if d.site:
        out.append(f"- Site: {len(d.pages)} pages of {d.site}, as text.")
        out += [f"  - {p.url} ({len(p.text):,} characters)" for p in d.pages]
        out += [f"  - skipped {u}: {why}" for u, why in d.site_log.get("skipped", {}).items()]
    else:
        out.append("- Site: none given, so the mission says nothing about the product. Rerun with `--site` to add it.")
    if d.models:
        pairs = sorted({(m.get("requested"), m.get("reported")) for m in d.models})
        out.append(f"- LLM calls: {len(d.models)}; " + "; ".join(f"requested {r}, the gateway reported {p}" for r, p in pairs) + ".")
    out += ["", "## Mission claims to check", "",
            "Each line of mission.md under the cited labels, with what it cites. A line passed the mechanical trace "
            "(names, numbers and enough words found in its sources); only a person can tell whether it says what the "
            "source means.", "", "| Section | Claim | Sources |", "| --- | --- | --- |"]
    for key, section in SECTION_OF.items():
        for it in d.items.get(key) or []:
            srcs = ", ".join(f"[{i}]({d.sources[i]['url']})" for i in it["sources"])
            out.append(f"| {section} | {it['text'].replace('|', '/')} | {srcs} |")
    if d.left_out:
        out += ["", f"Left out of the mission ({len(d.left_out)}), because the line had no usable source or failed the trace:", ""]
        out += [f"- {x}" for x in d.left_out]
    if d.site:
        out += ["", f"Site facts kept: {len(d.facts)}. Dropped: {len(d.dropped_facts)}."]
        out += [f"- {x}" for x in d.dropped_facts]
    out += ["", "## Watch list", "",
            f"Ranked by the number of @{d.handle}'s posts that replied to, quoted, mentioned or reposted each account. "
            f"The cut keeps accounts with {history.WATCH_MIN_EVENTS} posts or more, at most {history.WATCH_MAX}. Every "
            "account is in `accounts`, so the watcher treats all of its posts as on topic. Move people and big "
            "platforms whose posts are mostly off-topic to `broad_accounts`, where only keyword matches count.", "",
            "| # | Account | Why | Example posts |", "| --- | --- | --- | --- |"]
    for n, c in enumerate(d.watch, 1):
        examples = " ".join(f"[{i}](https://x.com/{d.handle}/status/{i})" for i in c.evidence)
        staff = f"; bio mentions @{d.handle}" if c.handle.lower() in d.staff else ""
        out.append(f"| {n} | @{c.handle} | {c.why()}{staff} | {examples} |")
    flagged = sum(c.handle.lower() in d.staff for c in d.watch)
    if flagged:
        out += ["", f"{flagged} of these accounts have a bio that mentions @{d.handle}, which usually means its own "
                "staff. The account already amplifies them; decide whether the watcher should draft replies to them."]
    if not d.watch:
        out.append("| | none qualified | The watcher refuses a profile whose watch list is empty; add accounts by hand. | |")
    below = [c for c in d.ranked if c not in d.watch][:10]
    if below:
        out += ["", "Next below the cut:", ""] + [f"- @{c.handle}: {c.why()}" for c in below]
    if "replies_tab_error" in d.read_log:
        out += ["", "The replies tab couldn't be read, so no reply counted toward the watch list."]
    out += ["", "## Baselines", "", "| Measure | Value |", "| --- | --- |",
            f"| Originals a day | {b.originals_per_day} |", f"| Replies to others a day | {b.replies_per_day} |",
            f"| Reposts a day | {b.reposts_per_day} |",
            f"| Median engagement (likes + reposts + replies + quotes), {b.mature_originals} originals 48 h or older | {b.median_engagement} |",
            f"| Median likes | {b.median_likes} |",
            f"| Median views | {'unknown' if b.median_views is None else b.median_views} |",
            f"| Originals with media | {_pct(b.media_share)} |"]
    out += [f"| Originals: {f.replace('_', ' ')} | {_pct(v)} |" for f, v in b.formats.items()]
    out += ["", "## Voice exemplars", "",
            f"Settled originals with at least {history.EXEMPLAR_MIN_MULTIPLE:g} times the median engagement, strongest first.", ""]
    out += [f"- {e.multiple}x ({e.engagement} engagements): [{e.url}]({e.url})" for e in d.exemplars] or ["- none qualified"]
    out += ["", "## What a person must edit", "",
            "- mission.md: read every cited line against its source, cut what's wrong, add what the account stands for "
            "that no page says, then delete the provisional first line.",
            "- mission.md's Do and Don't rules come from the devtools mission. Where the account's own voice breaks one, "
            "for example posts that ask readers to comment or retweet, decide which wins and edit the other.",
            "- observe.toml: prune the watch list, move accounts to `broad_accounts`, and check the keywords and queries.",
            f"- panel.json, prompts.toml, reply_framing.json and qualify.toml are copies of the `{TEMPLATE_PROFILE}` profile. "
            "They fit a developer-tools brand; rewrite them for any other kind.", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- writing


def write(d: Draft, profiles_dir: Path | None = None, out_dir: Path | None = None,
          template_dir: Path | None = None) -> Path:
    """Write the profile directory in one rename, and the claim check's sources to `out_dir/<profile>/`.

    Refuses when the profile directory exists, checked again just before the rename."""
    profiles_dir = profiles_dir or config.PROFILES_DIR
    out_dir = out_dir or config.OUT_DIR / "onboard"
    template_dir = template_dir or config.PROFILES_DIR / TEMPLATE_PROFILE
    target = check_free(d.profile, profiles_dir)
    files = {"mission.md": render_mission(d), "observe.toml": render_observe(d, template_dir),
             "ONBOARDING.md": render_report(d)}
    tmp = profiles_dir / f".{d.profile}.onboarding-{os.getpid()}"
    tmp.mkdir(parents=True)
    try:
        for name, text in files.items():
            (tmp / name).write_text(text, encoding="utf-8")
        for name in COPIED_FILES:
            shutil.copyfile(template_dir / name, tmp / name)
        check_free(d.profile, profiles_dir)
        os.rename(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    record = out_dir / d.profile
    record.mkdir(parents=True, exist_ok=True)
    (record / "sources.json").write_text(json.dumps({"pages": d.page_texts(), "posts": d.post_texts()}, indent=1,
                                                    ensure_ascii=False), encoding="utf-8")
    summary = {"handle": d.handle, "site": d.site, "today": d.today.isoformat(), "read_log": d.read_log,
               "baselines": asdict(d.baselines), "exemplars": [asdict(e) for e in d.exemplars],
               "watch": [c.handle for c in d.watch], "keywords": d.keywords, "items": d.items,
               "left_out": d.left_out, "facts": [asdict(f) for f in d.facts], "dropped_facts": d.dropped_facts,
               "models": d.models}
    (record / "draft.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return target


def check(profile: str, profiles_dir: Path | None = None, out_dir: Path | None = None) -> list[claims.ClaimCheck]:
    """Trace every claim in a profile's mission.md against the sources its onboarding saved."""
    profiles_dir = profiles_dir or config.PROFILES_DIR
    out_dir = out_dir or config.OUT_DIR / "onboard"
    mission = profiles_dir / config._check_name(profile) / "mission.md"
    record = out_dir / profile / "sources.json"
    if not mission.is_file():
        raise FileNotFoundError(f"no mission.md at {mission}")
    if not record.is_file():
        raise FileNotFoundError(f"no onboarding sources at {record}; the check needs the pages and posts "
                                "`harness onboard` read for this profile")
    sources = json.loads(record.read_text(encoding="utf-8"))
    return claims.check_mission(mission.read_text(encoding="utf-8"), sources["pages"], sources["posts"])
