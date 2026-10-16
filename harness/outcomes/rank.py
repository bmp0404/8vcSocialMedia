"""rank_in_thread: where the account's reply sits among the replies to the same post.

A reply is judged against the other replies in its thread, never against the account's own
median. A new account's median is 0, so every reply would beat it; and a reply's likes depend on
the thread it joined far more than on the account's other replies. Within a thread the reply
benchmark's signal lives in timing and reach (harness/corpus/DESIGN.md on sam/corpus), so the rank
is logged next to the minutes after the parent and the account's follower count.

The rank is the reply's 1-based position among the direct replies on the first page of the
parent's conversation in X's likes order (fxtwitter's `ranking_mode=likes`, about 35 replies), the
order a reader scrolls. The percentile is `1 - (rank - 1) / n`, where n is the parent's reply
counter (or the page, if longer): 1.0 is the top reply and 1/n the last. A reply missing from the
page ranks below every reply on it, so only an upper bound on its percentile is known.
"""

from dataclasses import asdict, dataclass

RANK_CHECKPOINTS = (1, 24)  # hours after our reply, inside the label.py windows [T, 2T)
# Both uncalibrated: no real _sgu_ reply had been ranked when they were set.
MIN_THREAD = 5  # replies under the parent, ours included, below which a rank says nothing
SUCCESS_PERCENTILE = 0.5  # the upper half of the thread

SUCCESS, FAILURE = "success", "failure"
INSUFFICIENT_THREAD = "insufficient_thread"  # too few replies to rank against
UNRANKED = "unranked"  # below the first page, and the page doesn't say whether that's the lower half


@dataclass(frozen=True)
class Rank:
    rank: int | None  # position on the likes-ordered page; None when the reply isn't on it
    likes_rank: int | None  # 1 + replies on the page with more likes; None when not on it
    page_replies: int  # direct replies on the page, ours included when present
    thread_replies: int  # the parent's reply counter, or page_replies if that is larger
    percentile: float | None
    percentile_max: float | None  # when off the page: the best percentile the reply could have
    likes: int | None
    followers: int | None
    outcome: str

    def to_dict(self) -> dict:
        return asdict(self)


def rank_in_thread(page: list, parent_id: str, reply_id: str, parent_replies: int | None) -> Rank:
    """Rank `reply_id` among the direct replies to `parent_id` on `page` (xreader XPosts, X's likes order).

    A reply whose `replying_to_status` fxtwitter dropped counts as direct, as `x_fx.thread_replies` does.
    """
    direct = [r for r in page if r.id != parent_id and r.replying_to_status in (None, parent_id)]
    ours = next((r for r in direct if r.id == reply_id), None)
    on_page = ours is not None
    n = max(parent_replies or 0, len(direct) + (0 if on_page else 1))
    if not on_page:
        bound = round(1 - len(direct) / n, 4)
        if n < MIN_THREAD:
            outcome = INSUFFICIENT_THREAD
        else:
            outcome = FAILURE if bound < SUCCESS_PERCENTILE else UNRANKED
        return Rank(None, None, len(direct), n, None, bound, None, None, outcome)
    rank = direct.index(ours) + 1
    likes_rank = 1 + sum(1 for r in direct if r.likes > ours.likes)
    pct = round(1 - (rank - 1) / n, 4)
    if n < MIN_THREAD:
        outcome = INSUFFICIENT_THREAD
    else:
        outcome = SUCCESS if pct >= SUCCESS_PERCENTILE else FAILURE
    return Rank(rank, likes_rank, len(direct), n, pct, None, ours.likes, ours.author_followers, outcome)
