"""Flag engagement that bots, paid boosts or coordinated pushes may have inflated.

The team rule is to mark these rows, never drop them, so eval can compare
results with and without them. From final counts alone no single signal
proves anything: a meme earns few bookmarks, a launch earns many reposts. So
a post is `boost_suspect` only when two or more independent signals agree.

Count-ratio signals fire when a post sits beyond the 1st or 99th percentile of
the same ratio across the dataset (among posts with enough volume for the ratio
to mean something). The cutoffs are therefore descriptive of this dataset, not
validated against known bought engagement; no labelled boosted posts exist yet.
Curve signals (drip_feed, like_only_burst, count_drop, unexplained_step) come
from `curves.detect` once the tracker has snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RatioRule:
    name: str
    numerator: str
    denominator: str
    side: str  # "high" fires above the 99th percentile, "low" below the 1st
    min_denominator: float
    why: str


RULES = (
    RatioRule("like_rate_high", "likes", "views", "high", 2000,
              "likes per view far above peers: bought likes do not come with matching impressions"),
    RatioRule("repost_heavy", "reposts", "likes", "high", 50,
              "reposts outpace likes: repost rings and engagement pods inflate reposts"),
    RatioRule("bookmarks_low", "bookmarks", "likes", "low", 200,
              "many likes, almost no bookmarks: purchased likes never save the post"),
    RatioRule("replies_low", "replies", "likes", "low", 200,
              "many likes, almost no conversation"),
)
AUTHOR_OUTLIER = 20.0  # engagement >= 20x the author's own median
REPLY_MIN_SAMPLE = 10
LOW_FOLLOWER = 20  # repliers with fewer followers than this look like throwaway accounts
LOW_FOLLOWER_SHARE = 0.5
DUP_REPLY_SHARE = 0.2
BURST_WINDOW_S = 120
BURST_SHARE = 0.4
MIN_SIGNALS = 2


def _quantile(values: list[float], q: float) -> float:
    v = sorted(values)
    if not v:
        raise ValueError("no values")
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def ratio_cutoffs(rows: list[dict]) -> dict[str, float]:
    """The 1st or 99th percentile of each rule's ratio over rows with enough volume."""
    cut = {}
    for r in RULES:
        vals = [
            row[r.numerator] / row[r.denominator]
            for row in rows
            if row.get(r.numerator) is not None and (row.get(r.denominator) or 0) >= r.min_denominator
        ]
        if len(vals) >= 50:
            cut[r.name] = _quantile(vals, 0.99 if r.side == "high" else 0.01)
    return cut


def reply_signals(replies: list[dict]) -> dict:
    """Signals from a sample of replies: [{"text", "author_followers", "created_unix"}]."""
    n = len(replies)
    out = {"reply_sample_n": n}
    if n < REPLY_MIN_SAMPLE:
        return out
    low = sum(1 for r in replies if (r.get("author_followers") or 0) < LOW_FOLLOWER) / n
    norm = [" ".join((r.get("text") or "").lower().split()) for r in replies]
    dup = sum(1 for t in norm if t and norm.count(t) > 1) / n
    times = sorted(r["created_unix"] for r in replies if r.get("created_unix"))
    burst = 0
    for i, t in enumerate(times):
        j = i
        while j < len(times) and times[j] - t <= BURST_WINDOW_S:
            j += 1
        burst = max(burst, j - i)
    out.update(
        reply_low_follower_share=round(low, 3),
        reply_dup_share=round(dup, 3),
        reply_burst_share=round(burst / n, 3),
    )
    return out


def score(row: dict, cutoffs: dict[str, float], curve_flags: list[str] | None = None) -> dict:
    """boost_score (number of signals), boost_reasons and boost_suspect for one row.

    `row` needs likes, reposts, replies, bookmarks, views; optionally
    eng_ratio_author_timeline and the reply_* fields from `reply_signals`.
    """
    reasons = []
    for r in RULES:
        if r.name not in cutoffs:
            continue
        num, den = row.get(r.numerator), row.get(r.denominator) or 0
        if num is None or den < r.min_denominator:
            continue
        ratio = num / den
        if (r.side == "high" and ratio > cutoffs[r.name]) or (r.side == "low" and ratio < cutoffs[r.name]):
            reasons.append(r.name)
    ratio_author = row.get("eng_ratio_author_timeline")
    if ratio_author is not None and ratio_author >= AUTHOR_OUTLIER:
        reasons.append("author_outlier")
    if (row.get("reply_low_follower_share") or 0) >= LOW_FOLLOWER_SHARE:
        reasons.append("throwaway_repliers")
    if (row.get("reply_dup_share") or 0) >= DUP_REPLY_SHARE:
        reasons.append("duplicate_replies")
    if (row.get("reply_burst_share") or 0) >= BURST_SHARE:
        reasons.append("reply_burst")
    reasons += [f for f in (curve_flags or []) if f in {"drip_feed", "like_only_burst", "count_drop", "unexplained_step"}]
    return {
        "boost_score": len(reasons),
        "boost_reasons": ";".join(reasons),
        "boost_suspect": len(reasons) >= MIN_SIGNALS,
    }
