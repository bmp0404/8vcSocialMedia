"""Check onboarding against corpus brands: watch-list recall after a cutoff, mission fit, and claim tracing.

For each brand, `CorpusReader` replays its timeline in the corpus snapshot (`data/corpus/raw/`, rebuilt as in
`harness/corpus/DESIGN.md`) as it stood before the cutoff, and `profile.draft` builds the watch list and the mission
from that alone, plus the brand's site read today. The posts after the cutoff are the test.

- Watch-list recall: of the accounts the brand replied to, quoted, mentioned or reposted after the cutoff, the share
  the drafted list holds, against the devtools watch list and against the 24 accounts the other corpus brands engaged
  with most before the cutoff (a list built from the same data without the brand's own history).
- Mission fit: the reply planner (`lanes.plan_targets`) judges each post under the drafted mission and under the
  devtools mission, one post per call as on the watcher's hot path. Reported: how often it calls off_mission the
  posts the brand engaged with (its own later posts and the posts it reposted, quoted or replied to), and how often
  it does so for peer posts the brand didn't engage with, so a mission that accepts everything shows as such.
- Claims: `claims.check_mission` over each drafted mission.md.

    HARNESS_PROVIDER=anthropic uv run python -m harness.onboard validate --out <dir>
"""

import functools
import gzip
import json
import random
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harness import config
from harness.context.xreader import XPost
from harness.corpus.load import Corpus, Item
from harness.corpus.snapshot import RAW_DIR
from harness.llm import LLM
from harness.minds import lanes
from harness.onboard import history, profile
from harness.onboard.site import Getter

# Every corpus brand on the devtools watch list with a full timeline in the corpus, chosen as `DESIGN.md` explains.
BRANDS = {"supabase": "https://supabase.com/", "resend": "https://resend.com/", "vllm_project": "https://vllm.ai/"}
CUTOFF = datetime(2026, 10, 1, tzinfo=timezone.utc)
SEED = 20261016
PEER_POSTS = 40  # per brand; enough that a mission that rejects nothing shows a low off-mission rate on peers
OWN_POSTS = 60  # per brand, sampled when it posted more after the cutoff
LESSONS = "(no lessons yet)"  # both missions see the same lessons, none, so only the mission differs
LLM_BUDGET = 1200
DRAFT_CALLS_PER_BRAND = 12  # one per site page (at most 11) and the mission call


def _to_post(item: Item, corpus: Corpus, reposted_by: str | None, read_by: datetime | None) -> XPost:
    quoted = corpus.get(item.quoted_id) if item.quoted_id else None
    fetched = item.fetched_at if read_by is None else min(item.fetched_at, read_by)
    m = item.metrics
    return XPost(id=item.status_id, text=item.text, author_handle=item.author, created_at=item.created_at,
                 fetched_at=fetched.isoformat(), likes=m["likes"], reposts=m["reposts"], replies=m["replies"],
                 quotes=m["quotes"], bookmarks=m.get("bookmarks"), views=m.get("views"),
                 author_followers=item.author_followers, author_verified=item.author_verified,
                 replying_to=item.parent_author, replying_to_status=item.parent_id, quoted_id=item.quoted_id,
                 quoted_author=quoted.author if quoted else None, is_repost=reposted_by is not None,
                 reposted_by=reposted_by, media_types=list(item.media_types), n_links=item.n_links,
                 n_mentions=item.n_mentions, lang=item.lang, source="corpus")


class CorpusReader:
    """An XReader over the corpus for posts created in [start, end).

    The Posts tab is the account's own posts and its reposts. The corpus timelines came from that tab, so they hold
    no replies to other accounts; the Posts and replies tab adds the account's replies found in the corpus reply
    samples, which covers only threads under dataset posts. A repost is placed by the reposted post's own time,
    since fxtwitter gives no repost time. With `end` set, every read is dated no later than `end`, so a post younger
    than 48 hours at the cutoff isn't treated as settled; its counts are still the later read's."""

    def __init__(self, corpus: Corpus, start: datetime | None = None, end: datetime | None = None):
        self.corpus, self.start, self.end = corpus, start, end

    def _in_window(self, item: Item) -> bool:
        return (self.start is None or item.created_at >= self.start) and (self.end is None or item.created_at < self.end)

    def profile_statuses(self, handle: str, limit: int = 20, with_replies: bool = False) -> list[XPost]:
        h = handle.lstrip("@").lower()
        out = []
        for item in self.corpus.items:
            if not self._in_window(item):
                continue
            own = item.author.lower() == h
            if own and item.kind == "post":
                out.append(_to_post(item, self.corpus, None, self.end))
            elif own and item.parent_author and item.parent_author.lower() == h:
                out.append(_to_post(item, self.corpus, None, self.end))  # a thread continuation
            elif own and with_replies and "reply_samples" in item.sources:
                out.append(_to_post(item, self.corpus, None, self.end))
            elif not own and h in {r.lower() for r in item.reposted_by}:
                out.append(_to_post(item, self.corpus, handle.lstrip("@"), self.end))
        return sorted(out, key=lambda p: int(p.id), reverse=True)[:limit]


# ---------------------------------------------------------------- watch-list recall


def engaged_accounts(corpus: Corpus, handle: str, start: datetime | None, end: datetime | None) -> dict[str, int]:
    """Lowercased account -> posts in [start, end) in which `handle` engaged it, by `history.engagements`."""
    reader = CorpusReader(corpus, start, end)
    tl = history.split(handle, reader.profile_statuses(handle, limit=10**6, with_replies=True))
    return {c.handle.lower(): c.events for c in history.rank_accounts(history.engagements(tl), exclude=(handle,))}


@functools.cache
def timeline_accounts(raw_dir: Path = RAW_DIR) -> tuple[str, ...]:
    """Accounts whose timelines the corpus snapshot holds, as its `timeline_of` field names them."""
    with gzip.open(raw_dir / "author_timelines.jsonl.gz", "rt", encoding="utf-8") as f:
        owners = {json.loads(line).get("timeline_of") for line in f if line.strip()}
    return tuple(sorted((o for o in owners if o), key=str.lower))


def popular_list(corpus: Corpus, brand: str, end: datetime, accounts: tuple[str, ...],
                 n: int = history.WATCH_MAX) -> list[str]:
    """The `n` accounts the most other corpus brands (`accounts`) engaged with before `end`, then by total posts."""
    brands, posts = {}, {}
    for other in accounts:
        if other.lower() == brand.lower():
            continue
        for account, events in engaged_accounts(corpus, other, None, end).items():
            if account == brand.lower():
                continue
            brands[account] = brands.get(account, 0) + 1
            posts[account] = posts.get(account, 0) + events
    return sorted(brands, key=lambda a: (-brands[a], -posts[a], a))[:n]


def recall(watch: list[str], engaged: dict[str, int]) -> dict:
    keep = {w.lower() for w in watch}
    hit = [a for a in engaged if a in keep]
    total = sum(engaged.values())
    return {"size": len(watch), "accounts": len(engaged), "hit": len(hit),
            "recall": round(len(hit) / len(engaged), 3) if engaged else None,
            "events": total, "events_hit": sum(engaged[a] for a in hit),
            "event_recall": round(sum(engaged[a] for a in hit) / total, 3) if total else None}


def devtools_list() -> list[str]:
    raw = tomllib.loads((config.PROFILES_DIR / "devtools" / "observe.toml").read_text(encoding="utf-8"))
    return list(raw["reply_targets"]["accounts"])


# ---------------------------------------------------------------- mission fit


@dataclass
class FitPost:
    url: str
    author: str
    text: str
    group: str  # "own": the brand's later post; "engaged": a post it reposted, quoted or replied to; "peer": neither


def fit_posts(corpus: Corpus, brand: str, start: datetime, rng: random.Random, accounts: tuple[str, ...],
              own_cap: int = OWN_POSTS, n_peers: int = PEER_POSTS) -> list[FitPost]:
    """The brand's own originals after `start` (at most `own_cap`), the posts it engaged with after `start` that the
    corpus holds the text of, and `n_peers` originals after `start` by the other `accounts` it didn't engage with."""
    b = brand.lower()
    tl = history.split(brand, CorpusReader(corpus, start).profile_statuses(brand, limit=10**6, with_replies=True))
    own = [corpus.get(p.id) for p in tl.originals]
    own = rng.sample(own, own_cap) if len(own) > own_cap else own
    engaged_ids = [p.id for p in tl.reposts]
    engaged_ids += [p.quoted_id for p in tl.own if p.quoted_id and p.quoted_author]
    engaged_ids += [p.replying_to_status for p in tl.replies if p.replying_to_status]
    engaged = [i for i in (corpus.get(x) for x in dict.fromkeys(engaged_ids))
               if i is not None and i.text.strip() and i.author.lower() != b and i.created_at >= start]
    taken = {i.status_id for i in own + engaged}
    peers = {a.lower() for a in accounts} - {b}
    peers_pool = [i for i in corpus.items
                  if i.created_at >= start and i.kind == "post" and i.author.lower() in peers
                  and i.status_id not in taken and history.words_of(i.text)]
    peers = rng.sample(peers_pool, min(n_peers, len(peers_pool)))
    return ([FitPost(i.url, i.author, i.text, "own") for i in own] + [FitPost(i.url, i.author, i.text, "engaged") for i in engaged]
            + [FitPost(i.url, i.author, i.text, "peer") for i in peers])


def judge(llm: LLM, mission: str, post: FitPost) -> dict:
    """The planner's call on one post, as the watcher's hot path makes it: one target, no live facts."""
    with config.use_profile("devtools"):  # plan_care comes from the profile; a worker thread doesn't inherit it
        target = {"url": post.url, "author": post.author, "text": post.text}
        try:
            plans, error = lanes.plan_targets(llm, mission, LESSONS, [target])
        except Exception as e:  # a gateway failure costs this post its verdict, not the run
            return {"tool": "error", "reason": "", "detail": f"{type(e).__name__}: {e}"}
    plan = plans.get(post.url)
    if plan is None:
        return {"tool": "error", "reason": "", "detail": error or "the plan skipped the post"}
    return {"tool": plan["tool"], "reason": plan.get("reason", ""), "detail": plan.get("detail", "")}


def off_mission_rates(rows: list[dict], arm: str) -> dict:
    out = {}
    for group in ("own", "engaged", "peer"):
        judged = [r for r in rows if r["group"] == group and r[arm]["tool"] != "error"]
        off = sum(r[arm]["tool"] == "abstain" and r[arm]["reason"] == "off_mission" for r in judged)
        out[group] = {"judged": len(judged), "off_mission": off,
                      "rate": round(off / len(judged), 3) if judged else None,
                      "errors": sum(r["group"] == group and r[arm]["tool"] == "error" for r in rows)}
    brand = [r for r in rows if r["group"] in ("own", "engaged") and r[arm]["tool"] != "error"]
    off = sum(r[arm]["tool"] == "abstain" and r[arm]["reason"] == "off_mission" for r in brand)
    out["brand_engaged"] = {"judged": len(brand), "off_mission": off, "rate": round(off / len(brand), 3) if brand else None}
    return out


def paired(rows: list[dict], groups: tuple[str, ...]) -> dict:
    """Posts where exactly one mission said off_mission, for a sign test between the two."""
    def off(r, arm):
        return r[arm]["tool"] == "abstain" and r[arm]["reason"] == "off_mission"
    both = [r for r in rows if r["group"] in groups and "error" not in (r["onboarded"]["tool"], r["devtools"]["tool"])]
    return {"pairs": len(both), "only_devtools_off": sum(off(r, "devtools") and not off(r, "onboarded") for r in both),
            "only_onboarded_off": sum(off(r, "onboarded") and not off(r, "devtools") for r in both)}


# ---------------------------------------------------------------- one brand


@dataclass
class BrandResult:
    brand: str
    site: str
    profile_dir: str
    draft: dict = field(default_factory=dict)
    recall: dict = field(default_factory=dict)
    claims: dict = field(default_factory=dict)
    fit: dict = field(default_factory=dict)


def _names(brand: str, cutoff: datetime, out: Path) -> tuple[str, Path, Path, Path]:
    name = f"{brand.lower().replace('_', '-')}-cut-{cutoff:%Y%m%d}"
    return name, out / "profiles", out / "onboard", out / f"verdicts-{brand}.jsonl"


def run_brand(corpus: Corpus, brand: str, site: str, llm: LLM, out: Path, cutoff: datetime = CUTOFF,
              get: Getter | None = None, workers: int = 4) -> tuple[BrandResult, list[dict]]:
    """Draft, judge and score one brand.

    A rerun picks up where a crash left off: a drafted profile already in `out` is reused, since onboarding never
    overwrites one, and so is the brand's verdicts file, so LLM calls go only to what's missing. Recall is recomputed
    from the corpus every time; the ranking is deterministic, and a drafted watch list that doesn't match it fails."""
    name, profiles_dir, record_dir, verdict_file = _names(brand, cutoff, out)
    target = profiles_dir / name
    reader = CorpusReader(corpus, end=cutoff)
    if not target.exists():
        d = profile.draft(brand, name, reader=reader, llm=llm, site=site, get=get, today=cutoff.date(),
                          profiles_dir=profiles_dir)
        profile.write(d, profiles_dir=profiles_dir, out_dir=record_dir)
    summary = json.loads((record_dir / name / "draft.json").read_text(encoding="utf-8"))
    sources = json.loads((record_dir / name / "sources.json").read_text(encoding="utf-8"))
    res = BrandResult(brand, site, str(target))
    res.draft = {"pages": len(sources["pages"]), "facts": len(summary["facts"]), "dropped_facts": len(summary["dropped_facts"]),
                 "mission_lines": {k: len(v) for k, v in summary["items"].items()}, "left_out": len(summary["left_out"]),
                 "exemplars": len(summary["exemplars"]), "watch": len(summary["watch"]),
                 "originals_read": summary["baselines"]["originals"]}

    tl = history.split(brand, reader.profile_statuses(brand, limit=10**6, with_replies=True))
    ranked = history.rank_accounts(history.engagements(tl), exclude=(brand,))
    watch = [c.handle for c in history.watchlist(ranked)]
    if watch != summary["watch"]:
        raise RuntimeError(f"{name}: the drafted watch list {summary['watch']} isn't the one the corpus gives now {watch}")
    engaged_after = engaged_accounts(corpus, brand, cutoff, None)
    engaged_before = engaged_accounts(corpus, brand, None, cutoff)
    accounts = timeline_accounts()
    lists = {"onboarded": watch, "devtools": devtools_list(), "popular": popular_list(corpus, brand, cutoff, accounts),
             # Not the drafted rule: the same ranking filled to the devtools list's 24 without the 2-post floor.
             "onboarded_top24": [c.handle for c in ranked[:history.WATCH_MAX]]}
    res.recall = {arm: recall(w, engaged_after) for arm, w in lists.items()}
    res.recall["ceiling"] = recall(list(engaged_before), engaged_after)  # every account seen before the cutoff

    checked = profile.check(name, profiles_dir=profiles_dir, out_dir=record_dir)
    res.claims = {"lines": len(checked), "traced": sum(not c.problems for c in checked),
                  "by_section": {s: sum(c.section == s for c in checked) for s in dict.fromkeys(c.section for c in checked)},
                  "failures": [{"section": c.section, "text": c.text, "problems": c.problems} for c in checked if c.problems]}

    posts = fit_posts(corpus, brand, cutoff, random.Random(f"{SEED}:{brand}"), accounts)
    missions = {"onboarded": (target / "mission.md").read_text(encoding="utf-8"),
                "devtools": (config.PROFILES_DIR / "devtools" / "mission.md").read_text(encoding="utf-8")}
    if verdict_file.exists():
        rows = [json.loads(line) for line in verdict_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if [r["url"] for r in rows] != [p.url for p in posts]:
            raise RuntimeError(f"{verdict_file} holds verdicts on other posts than this run samples; move it away")
    else:
        rows = judge_all(llm, missions, posts, workers)
        verdict_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    res.fit = {"posts": {g: sum(p.group == g for p in posts) for g in ("own", "engaged", "peer")},
               "onboarded": off_mission_rates(rows, "onboarded"), "devtools": off_mission_rates(rows, "devtools"),
               "paired_brand_engaged": paired(rows, ("own", "engaged")), "paired_peer": paired(rows, ("peer",))}
    return res, rows


def judge_all(llm: LLM, missions: dict[str, str], posts: list[FitPost], workers: int) -> list[dict]:
    jobs = [(i, arm) for i in range(len(posts)) for arm in missions]
    verdicts: dict[tuple[int, str], dict] = {}
    lock = threading.Lock()

    def work(job):
        i, arm = job
        v = judge(llm, missions[arm], posts[i])
        with lock:
            verdicts[job] = v

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, jobs))
    return [asdict(p) | {arm: verdicts[(i, arm)] for arm in missions} for i, p in enumerate(posts)]


def planned_calls(corpus: Corpus, brands: dict[str, str], cutoff: datetime, out: Path) -> int:
    """The most LLM calls a run can still make: the drafts and verdict files `out` doesn't hold yet."""
    total = 0
    for b in brands:
        name, profiles_dir, _, verdict_file = _names(b, cutoff, out)
        if not (profiles_dir / name).exists():
            total += DRAFT_CALLS_PER_BRAND
        if not verdict_file.exists():
            total += 2 * len(fit_posts(corpus, b, cutoff, random.Random(f"{SEED}:{b}"), timeline_accounts()))
    return total
