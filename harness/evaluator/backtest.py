"""Retrodiction backtest: rank real posts with each eval method and measure agreement with actual engagement.

The evaluator only sees post text (no author, reach, media or metrics). Engagement is normalized so
the test asks "is this post better than its account's usual?" rather than "is this a big account?":
  account      percentile of engagement within the author's posts in the file (authors with >= 5 posts);
               default when the feed has no impressions / follower counts
  impressions  engagement / impressions
  followers    engagement / author followers
  raw          engagement (confounded by account size; for reference only)
engagement = likes + 2*replies + 2*reposts + 3*quotes (memory.engagement's weights), else the feed's
`engagement` column.
"""

from __future__ import annotations

import csv
import json
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.evaluator import metrics as M
from harness.evaluator.candidates import Candidate, EvalContext
from harness.evaluator.llms import judge_llm
from harness.evaluator.rubric import CRITERIA, rubric
from harness.evaluator.tournament import compare_pairs, run_grouped
from harness.evaluator.triage import triage

ALIASES = {
    "id": ("id", "tweet_id", "id_str", "post_id"),
    "text": ("text", "full_text", "post_content", "content", "tweet", "body", "note_tweet.text"),
    "author": ("account", "author", "username", "handle", "screen_name", "user.username", "user.screen_name",
               "author.username"),
    "url": ("url", "post_url", "tweet_url", "link", "permalink"),
    "likes": ("likes", "like_count", "favorite_count", "public_metrics.like_count"),
    "replies": ("replies", "reply_count", "public_metrics.reply_count"),
    "reposts": ("retweets", "reposts", "retweet_count", "repost_count", "public_metrics.retweet_count"),
    "quotes": ("quotes", "quote_count", "public_metrics.quote_count"),
    "views": ("impressions", "views", "impression_count", "view_count", "public_metrics.impression_count"),
    "followers": ("followers", "followers_count", "author_followers", "user.followers_count",
                  "author.followers_count"),
    "engagement": ("engagement", "engagements"),
    "category": ("category", "label"),
    "segment": ("segment", "tier"),
    "media": ("media", "media_type"),
    "responding_to": ("responding_to", "in_reply_to_text", "parent_text"),
    "date": ("date", "created_at"),
}
VIRAL = {"super viral", "medium viral"}
URL_RE = re.compile(r"https?://\S+")


@dataclass
class Post:
    id: str
    text: str
    author: str
    eng: float
    url: str = ""
    views: float | None = None
    followers: float | None = None
    category: str = ""
    segment: str = ""
    media: str = ""
    responding_to: str = ""
    synthetic: bool = False
    target: float = 0.0
    group: str = ""
    scores: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- load


def _dig(row: dict, dotted: str) -> Any:
    cur: Any = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            return [{re.sub(r"\W+", "_", k.strip().lower()).strip("_"): v for k, v in r.items() if k} for r in csv.DictReader(f)]
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict):
        data = next((data[k] for k in ("data", "posts", "tweets", "items", "results") if isinstance(data.get(k), list)), [data])
    return [r for r in data if isinstance(r, dict)]


def load(path: str | Path) -> list[Post]:
    out = []
    for n, r in enumerate(_rows(Path(path))):
        g = lambda f: next((v for k in ALIASES[f] if (v := _dig(r, k)) not in (None, "")), None)  # noqa: E731
        text = g("text")
        if not isinstance(text, str) or not text.strip():
            continue
        comps = [_num(g(f)) for f in ("likes", "replies", "reposts", "quotes")]
        if any(c is not None for c in comps):
            likes, replies, reposts, quotes = (c or 0 for c in comps)
            eng = likes + 2 * replies + 2 * reposts + 3 * quotes
        else:
            eng = _num(g("engagement")) or 0.0
        out.append(Post(
            id=str(g("id") or g("url") or f"p{n}"), text=text.strip(), author=str(g("author") or "?").lstrip("@").lower(),
            eng=eng, url=str(g("url") or ""), views=_num(g("views")), followers=_num(g("followers")),
            category=str(g("category") or ""), segment=str(g("segment") or ""), media=str(g("media") or ""),
            responding_to=str(g("responding_to") or ""), synthetic=bool(r.get("synthetic")),
        ))
    return out


def body_len(text: str) -> int:
    return len(URL_RE.sub("", text).strip())


def normalize(posts: list[Post], mode: str = "auto", min_posts: int = 5) -> tuple[list[Post], str]:
    """Set Post.target (and group) per `mode`; drops posts that can't be normalized."""
    if mode == "auto":
        if all(p.views for p in posts):
            mode = "impressions"
        elif all(p.followers for p in posts):
            mode = "followers"
        else:
            mode = "account"
    if mode == "account":
        by = defaultdict(list)
        for p in posts:
            by[p.author].append(p)
        keep = [p for ps in by.values() if len(ps) >= min_posts for p in ps]
        pct = M.percentile_within([p.eng for p in keep], [p.author for p in keep])
        for p, v in zip(keep, pct):
            p.target, p.group = v, p.author
        return keep, mode
    for p in posts:
        denom = p.views if mode == "impressions" else p.followers if mode == "followers" else 1.0
        p.target = p.eng / denom if denom else 0.0
        p.group = "all"
    return [p for p in posts if (mode == "raw") or (p.views if mode == "impressions" else p.followers)], mode


def sample(posts: list[Post], segments: list[str] | None, accounts: int, per_account: int, seed: int = 0,
           min_body: int = 20, skip: int = 0) -> list[Post]:
    """Stratified sample for grouped (per-account) data: `accounts` authors, round-robin across segments,
    preferring authors with both viral and deadweight posts, and `per_account` posts spread evenly
    across each author's engagement distribution. Posts with < min_body chars of non-URL text are
    dropped (a text-only judge has nothing to read)."""
    rng = random.Random(seed)
    pool = [p for p in posts if body_len(p.text) >= min_body and (not segments or p.segment in segments)]
    if all(p.group == "all" for p in pool):
        return pool
    by = defaultdict(list)
    for p in pool:
        by[p.author].append(p)
    by = {a: ps for a, ps in by.items() if len(ps) >= per_account}

    def spread(ps: list[Post]) -> bool:
        cats = {p.category.lower() for p in ps}
        return bool(cats & VIRAL) and bool(cats - VIRAL) if any(p.category for p in ps) else True

    seg_lists = defaultdict(list)
    for a, ps in by.items():
        seg_lists[ps[0].segment].append(a)
    for seg in seg_lists:
        rng.shuffle(seg_lists[seg])
        seg_lists[seg].sort(key=lambda a: not spread(by[a]))
    order = [s for s in (segments or sorted(seg_lists)) if s in seg_lists]
    chosen = []
    while len(chosen) < accounts + skip and any(seg_lists[s] for s in order):
        for s in order:
            if seg_lists[s] and len(chosen) < accounts + skip:
                chosen.append(seg_lists[s].pop(0))
    chosen = chosen[skip:]
    out = []
    for a in chosen:
        ps = sorted(by[a], key=lambda p: (p.eng, p.id))
        k = min(per_account, len(ps))
        idx = sorted({round(i * (len(ps) - 1) / max(1, k - 1)) for i in range(k)})
        out += [ps[i] for i in idx]
    return out


# --------------------------------------------------------------------------- methods


def _cands(posts: list[Post]) -> list[Candidate]:
    return [Candidate(id=p.id, text=p.text, kind="reply" if p.responding_to else "post",
                      responding_to=p.responding_to, target_key=p.url or p.id) for p in posts]


def run_panel(posts: list[Post], ectx: EvalContext, chunk: int = 20) -> dict[str, float]:
    """The current qualify.py audience panel (absolute 0-1 predicted_score); critic vetoes ignored."""
    from harness import qualify  # imported late: other agents are editing it
    from harness.models import Action

    llm = judge_llm(ectx.llm)
    out = {}
    for s in range(0, len(posts), chunk):
        acts = [Action(type="post", text=p.text, mind="backtest", narrative_id="backtest", lane="post",
                       context={"text": p.responding_to} if p.responding_to else {}, id=p.id)
                for p in posts[s:s + chunk]]
        qualify.qualify(llm, ectx.mission, acts, [])
        out.update({a.id: a.predicted_score for a in acts})
    return out


def run_rubric(posts, ectx, backend) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    res = rubric(_cands(posts), ectx, backend)
    totals = {cid: r.total for cid, r in res.items()}
    per = {crit: {cid: r.scores[crit].score for cid, r in res.items() if crit in r.scores} for crit in CRITERIA}
    per["guard (voice+grounding+cringe)"] = {cid: r.guard for cid, r in res.items()}
    return totals, per


def run_pairwise(posts, ectx, backend):
    t = run_grouped(_cands(posts), {p.id: p.group for p in posts}, ectx, backend)
    return dict(t.win_prob), t


def run_triage(posts, ectx, backend) -> dict[str, float]:
    tr = triage([{"id": p.id, "url": p.url or p.id, "author": "@" + p.author, "text": p.text} for p in posts], ectx, backend)
    out = {}
    for p in posts:
        t = tr.get(p.url or p.id)
        if t:
            pw = t.probs.get("wait", 0.0) if t.probs else (1.0 if t.decision == "wait" else 0.0)
            out[p.id] = t.p_engage + 0.5 * pw
    return out


# --------------------------------------------------------------------------- report


def evaluate_scores(posts: list[Post], scores: dict[str, float], grouped: bool) -> dict[str, float]:
    ps = [p for p in posts if p.id in scores]
    if len(ps) < 4:
        return {}
    pred, act = [scores[p.id] for p in ps], [p.target for p in ps]
    groups = [p.group for p in ps] if grouped else None
    lo, hi = M.spearman_ci(pred, act)
    row = {"n": len(ps), "spearman": M.spearman(pred, act), "ci_lo": lo, "ci_hi": hi,
           "p_at_k": M.precision_at_k(pred, act), "pair_acc": M.pairwise_accuracy(pred, act, groups)}
    if any(p.category for p in ps):
        row["viral_auc"] = M.auc(pred, [p.category.lower() in VIRAL for p in ps], groups)
    return row


def combine(*maps: dict[str, float]) -> dict[str, float]:
    ids = sorted(set.intersection(*(set(m) for m in maps)))
    if not ids:
        return {}
    zs = [M.zscores([m[i] for i in ids]) for m in maps]
    return {i: sum(z[k] for z in zs) / len(zs) for k, i in enumerate(ids)}


def fmt_table(rows: list[tuple[str, dict]]) -> str:
    cols = [("n", "{:>4.0f}"), ("spearman", "{:>+7.2f}"), ("ci", "{}"), ("p_at_k", "{:>6.2f}"),
            ("pair_acc", "{:>8.2f}"), ("viral_auc", "{:>9.2f}")]
    has_auc = any("viral_auc" in r for _, r in rows)
    head = f"{'method':<40}{'n':>4}  {'spearman':>8} {'90% CI':>13}  {'P@25%':>6}  {'pair_acc':>8}" + ("  viral_AUC" if has_auc else "")
    lines = [head, "-" * len(head)]
    for name, r in rows:
        if not r:
            lines.append(f"{name:<40}  (not enough scored posts)")
            continue
        ci = f"[{r['ci_lo']:+.2f},{r['ci_hi']:+.2f}]"
        line = f"{name:<40}{r['n']:>4}  {r['spearman']:>+8.2f} {ci:>13}  {r['p_at_k']:>6.2f}  {r['pair_acc']:>8.2f}"
        if has_auc:
            line += f"  {r.get('viral_auc', float('nan')):>9.2f}"
        lines.append(line)
    return "\n".join(lines)


def isnan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)


def viral_pairs(posts: list[Post], n: int, seed: int = 0, segments: list[str] | None = None,
                exclude: set[str] | None = None, min_body: int = 20) -> list[tuple[Post, Post]]:
    """Same-account (viral, deadweight) pairs, each post used at most once (the teammate's
    `tune._pairs` construction, so judge accuracies are comparable)."""
    rng = random.Random(seed)
    by = defaultdict(lambda: ([], []))
    for p in posts:
        if body_len(p.text) < min_body or (segments and p.segment not in segments) or (exclude and p.id in exclude):
            continue
        cat = p.category.lower()
        if cat in VIRAL:
            by[p.author][0].append(p)
        elif cat == "deadweight":
            by[p.author][1].append(p)
    pool = [(h, f) for hits, flops in by.values() for h in hits for f in flops]
    rng.shuffle(pool)
    out, used = [], set()
    for h, f in pool:
        if h.id in used or f.id in used:
            continue
        used |= {h.id, f.id}
        out.append((h, f))
        if len(out) == n:
            break
    return out


def judge_pairs(pairs: list[tuple[Post, Post]], ectx: EvalContext, backend: str | None) -> dict:
    """Direct judge accuracy on (winner, loser) pairs, both orders; a disagreement counts as half."""
    cands = {p.id: c for p, c in zip([p for pr in pairs for p in pr], _cands([p for pr in pairs for p in pr]))}
    res = compare_pairs([(cands[h.id], cands[f.id]) for h, f in pairs], ectx, backend)
    right = sum(1.0 if r.s > 0.5 else 0.5 if r.s == 0.5 else 0.0 for r in res)
    strict = sum(r.s > 0.5 for r in res)
    lo, hi = M.wilson(right, len(res))
    return {"n": len(res), "acc": right / max(1, len(res)), "lo": lo, "hi": hi, "strict": strict,
            "ties": sum(r.s == 0.5 for r in res), "consistent": sum(r.consistent for r in res) / max(1, len(res))}
