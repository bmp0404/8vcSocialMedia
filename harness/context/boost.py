"""Flag engagement that paid reach, coordinated pushes or fake accounts may have inflated.

The team rule is to mark these rows, never drop them, so eval can compare
results with and without them. Three separate labels, because they mean
different things for a brand (following the research report's split):

- paid_or_external_reach: the post reached far more people than the author's
  posts usually do, and they engaged far less. X counts promoted impressions
  in public views, so promotion shows up as views without engagement. A repost
  by a much larger account looks the same: either way the brand's own
  audience did not earn the reach.
- coordinated_authentic: staff or community pushing the post. Real people,
  but not organic interest. Detected from reposter and replier bios naming
  the brand.
- likely_inauthentic: two or more independent fake-engagement signals.

`boost_suspect` is paid_or_external_reach or likely_inauthentic.

None of the cutoffs is calibrated against known bought engagement, because no
labelled examples exist. Count-ratio votes fire beyond the 1st or 99th
percentile of this dataset, so they describe outliers here, not proven fraud.
Two signals are deliberately not votes: `like_rate_high` (on this dataset it
flagged exactly six organic memes) and `author_outlier` (launches are meant to
beat the author's median).
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.context.settings import BoostParams


@dataclass(frozen=True)
class RatioRule:
    name: str
    numerator: str
    denominator: str
    side: str  # "high" fires above the upper quantile, "low" below the lower one
    min_param: str  # BoostParams field holding the minimum denominator
    vote: bool  # counts toward likely_inauthentic; otherwise reported as a weak signal
    group: str  # rules sharing a group cast one vote between them
    why: str


RULES = (
    RatioRule("like_rate_high", "likes", "views", "high", "like_rate_min_views", False, "like_rate",
              "likes per view far above peers; on this dataset it caught organic memes, so it does not vote"),
    RatioRule("repost_heavy", "reposts", "likes", "high", "repost_min_likes", True, "repost_ratio",
              "reposts outpace likes: repost rings and engagement pods inflate reposts"),
    # Both of these divide by likes, so one like purchase trips both: one vote between them.
    RatioRule("bookmarks_low", "bookmarks", "likes", "low", "bookmarks_min_likes", True, "passive_likes",
              "many likes, almost no bookmarks: purchased likes never save the post"),
    RatioRule("replies_low", "replies", "likes", "low", "replies_min_likes", True, "passive_likes",
              "many likes, almost no conversation"),
)
INAUTHENTIC_CURVE_FLAGS = {"drip_feed", "like_only_burst", "count_drop"}
REACH_CURVE_FLAGS = {"view_surge_low_conversion", "unexplained_step"}


def _quantile(values: list[float], q: float) -> float:
    v = sorted(values)
    if not v:
        raise ValueError("no values")
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def ratio_cutoffs(rows: list[dict], p: BoostParams = BoostParams()) -> dict[str, float]:
    """The lower or upper quantile of each rule's ratio over rows with enough volume."""
    cut = {}
    for r in RULES:
        floor = getattr(p, r.min_param)
        vals = [
            row[r.numerator] / row[r.denominator]
            for row in rows
            if row.get(r.numerator) is not None and (row.get(r.denominator) or 0) >= floor
        ]
        if len(vals) >= p.min_rows_for_cutoff:
            cut[r.name] = _quantile(vals, p.ratio_high_q if r.side == "high" else p.ratio_low_q)
    return cut


def reply_signals(replies: list[dict], p: BoostParams = BoostParams()) -> dict:
    """Signals from a sample of replies: [{"text", "author_followers", "created_unix"}].

    Burst share is only meaningful on a near-complete sample; callers drop it
    when coverage is low.
    """
    n = len(replies)
    out = {"reply_sample_n": n}
    if n < p.reply_min_sample:
        return out
    low = sum(1 for r in replies if (r.get("author_followers") or 0) < p.low_follower) / n
    norm = [" ".join((r.get("text") or "").lower().split()) for r in replies]
    dup = sum(1 for t in norm if t and norm.count(t) > 1) / n
    times = sorted(r["created_unix"] for r in replies if r.get("created_unix"))
    burst = 0
    for i, t in enumerate(times):
        j = i
        while j < len(times) and times[j] - t <= p.burst_window_s:
            j += 1
        burst = max(burst, j - i)
    out.update(
        reply_low_follower_share=round(low, 3),
        reply_dup_share=round(dup, 3),
        reply_burst_share=round(burst / n, 3),
    )
    return out


def classify(
    row: dict, cutoffs: dict[str, float], p: BoostParams = BoostParams(), curve_flags: list[str] | None = None
) -> dict:
    """The three boost labels for one post.

    `row` keys (any may be missing or None): likes, reposts, replies, bookmarks,
    views, engagement, author_median_views, author_median_eng_per_view,
    author_posts_with_views, eng_ratio_author, reply_coverage,
    reply_low_follower_share, reply_dup_share, reply_burst_share,
    reposter_staff_share, reposter_sample_n, reply_insider_share, reply_sample_n,
    reposter_new_account_share.
    """
    votes, weak, reach, coordinated = [], [], [], []
    groups: set[str] = set()
    for r in RULES:
        if r.name not in cutoffs:
            continue
        num, den = row.get(r.numerator), row.get(r.denominator) or 0
        if num is None or den < getattr(p, r.min_param):
            continue
        ratio = num / den
        if (r.side == "high" and ratio > cutoffs[r.name]) or (r.side == "low" and ratio < cutoffs[r.name]):
            if not r.vote:
                weak.append(r.name)
            elif r.group not in groups:
                groups.add(r.group)
                votes.append(r.name)
            else:
                weak.append(r.name)

    if (row.get("eng_ratio_author") or 0) >= p.author_outlier:
        weak.append("author_outlier")

    views, eng = row.get("views"), row.get("engagement")
    med_views, med_epv = row.get("author_median_views"), row.get("author_median_eng_per_view")
    if (
        views and eng is not None and med_views and med_epv
        and (row.get("author_posts_with_views") or 0) >= p.reach_min_author_posts
        and views >= p.reach_views_ratio * med_views
        and eng / views <= p.reach_conversion_ratio * med_epv
    ):
        reach.append("views_without_engagement")

    # Each sample is judged on its own size: one brand-bio reposter is not a share of 30 repliers.
    for share, n, name in (
        ("reposter_staff_share", "reposter_sample_n", "staff_reposters"),
        ("reply_insider_share", "reply_sample_n", "staff_repliers"),
    ):
        if (row.get(n) or 0) >= p.staff_min_sample and (row.get(share) or 0) >= p.staff_share:
            coordinated.append(name)

    covered = (row.get("reply_coverage") or 0) >= p.min_reply_coverage
    if covered and (row.get("reply_low_follower_share") or 0) >= p.low_follower_share:
        votes.append("throwaway_repliers")
    if (row.get("reply_dup_share") or 0) >= p.dup_reply_share:
        votes.append("duplicate_replies")
    if covered and (row.get("reply_burst_share") or 0) >= p.burst_share:
        votes.append("reply_burst")
    if (row.get("reposter_new_account_share") or 0) >= p.new_account_share:
        votes.append("new_account_reposters")
    for f in curve_flags or []:
        if f in INAUTHENTIC_CURVE_FLAGS:
            votes.append(f)
        elif f in REACH_CURVE_FLAGS:
            reach.append(f)

    inauthentic = len(votes) >= p.min_signals
    paid = bool(reach)
    cls = "likely_inauthentic" if inauthentic else "paid_or_external_reach" if paid else (
        "coordinated_authentic" if coordinated else "none")
    return {
        "boost_paid_or_external_reach": paid,
        "boost_coordinated_authentic": bool(coordinated),
        "boost_likely_inauthentic": inauthentic,
        "boost_suspect": paid or inauthentic,
        "boost_class": cls,
        "boost_inauthentic_votes": len(votes),
        "boost_reasons": ";".join(votes + reach + coordinated),
        "boost_weak_signals": ";".join(weak),
    }
