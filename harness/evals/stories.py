"""Build fault-attribution evidence from the dev-tool dataset, where several accounts posted about
the same model launch (eval/fixtures/devtools_stories.json).

For each post on a story this fills in what `fault.attribute` needs about the outcome and the input:
the account's own engagement band (from its other eligible posts) and how other accounts' posts on
the same story did in the 24 hours after it, each against its own account's median. It can't judge
the draft; pass `draft_scores` from a backtest report, or the verdict says the draft score is missing.

It also answers the question the observe half of attribution rests on: does a live story predict a
good outcome for a post about it? If the peer ratio doesn't track the post's own result, a dead story
can't be the explanation for a flop.
"""

import math
import statistics
from collections import defaultdict
from datetime import timedelta

from harness.evals import metrics as m
from harness.evals.dataset import Post
from harness.evals.fault import FaultEvidence
from harness.evals.observe_eval import mentions


def _percentile(xs: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (the default in numpy and spreadsheets)."""
    xs = sorted(xs)
    pos = (len(xs) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def story_evidence(posts: list[Post], stories: dict[str, list[str]], pool: list[Post],
                   draft_scores: dict[str, float] | None = None, window_h: float = 24) -> list[tuple[str, FaultEvidence]]:
    """(story id, evidence) for every post in `posts` that mentions a story. `pool` gives each account's
    baseline (its eligible posts) and each peer's median."""
    by_account: dict[str, list[Post]] = defaultdict(list)
    for q in pool:
        by_account[q.account].append(q)
    medians = {a: statistics.median(q.engagement for q in qs) for a, qs in by_account.items()}
    window = timedelta(hours=window_h)
    out = []
    for sid, keys in stories.items():
        on_story = [p for p in posts if mentions(p.clean_text, keys)]
        for p in on_story:
            base = [q.engagement for q in by_account[p.account] if q.id != p.id]
            peers = [q for q in on_story if q.account != p.account and p.created_at <= q.created_at < p.created_at + window
                     and q.account in medians]
            ratios = [(q.engagement + 1) / (medians[q.account] + 1) for q in peers]
            out.append((sid, FaultEvidence(
                action_id=p.id, engagement=p.engagement,
                account_p10=_percentile(base, 10) if base else None,
                account_p90=_percentile(base, 90) if base else None,
                account_median=statistics.median(base) if base else None,
                n_baseline_posts=len(base),
                story_peer_ratio=statistics.median(ratios) if ratios else None,
                n_peer_posts=len(ratios),
                draft_score=(draft_scores or {}).get(p.id),
                notes=f"story {sid}",
            )))
    return out


def liveness_check(evidence: list[FaultEvidence], min_peers: int = 3) -> dict:
    """Spearman between a post's own result (against its account's median) and its story's peer ratio."""
    rows = [e for e in evidence if e.story_peer_ratio is not None and (e.n_peer_posts or 0) >= min_peers
            and e.account_median is not None]
    own = [math.log((e.engagement + 1) / (e.account_median + 1)) for e in rows]
    peer = [math.log(e.story_peer_ratio) for e in rows]
    return {"n_posts": len(rows), "min_peers": min_peers, "spearman_own_vs_peer": m.spearman(own, peer)}
