from datetime import date, datetime, timedelta, timezone

import pytest

from harness.evals import observe_eval as oe
from harness.evals.dataset import Post
from harness.llm import MockLLM
from harness.models import Narrative

T0 = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
STORIES = {"opus": ["opus 5.5"], "argon": ["gemini 4 argon"]}


def post(i, account, text, hours):
    return Post(id=f"https://x.com/{account}/status/{i}", account=account, text=text, media="none", likes=0,
                replies=0, retweets=0, engagement=0, date=date(2026, 9, 22), category="Deadweight",
                segment="Mid-size", created_at=T0 + timedelta(hours=hours))


POSTS = [
    post(1, "@a", "Claude Opus 5.5 is now available in our editor", 0),
    post(2, "@b", "Opus 5.5 is live for every team", 2),
    post(3, "@c", "We added Opus 5.5 to the agent picker", 3),
    post(4, "@d", "Gemini 4 Argon lands in the CLI", 30),
    post(5, "@e", "Unrelated database news", 0.5),
]


def test_cutoffs_sit_an_hour_after_each_storys_first_post():
    assert oe.cutoffs(POSTS, STORIES) == [T0 + timedelta(hours=1), T0 + timedelta(hours=31)]


def test_a_story_is_live_with_a_post_before_and_two_new_accounts_after():
    assert oe.live_stories(POSTS, STORIES, T0 + timedelta(hours=1)) == ["opus"]
    # Argon has nothing after its first post, and Opus is out of the 24 h window by then.
    assert oe.live_stories(POSTS, STORIES, T0 + timedelta(hours=31)) == []


def test_signals_before_carry_no_engagement():
    signals = oe.signals_before(POSTS, T0 + timedelta(hours=1))
    assert [s.url for s in signals] == [POSTS[0].id, POSTS[4].id]
    assert all(s.metrics == {} for s in signals)


def test_naive_observer_takes_the_most_recent_posts():
    signals = oe.signals_before(POSTS, T0 + timedelta(hours=4))
    assert [n.summary for n in oe.naive_narratives(signals, 2)] == [POSTS[2].clean_text, POSTS[1].clean_text]


def test_surfaced_matches_keywords_in_title_summary_or_angle():
    assert oe.surfaced([Narrative(id="x", title="Model news", summary="", angle="compare OPUS 5.5 costs")],
                       ["opus 5.5"])
    assert not oe.surfaced([Narrative(id="x", title="Opus 5", summary="")], ["opus 5.5"])


def test_time_travel_runs_on_mock():
    r = oe.time_travel(MockLLM(), POSTS, STORIES, "mission", top=2)
    assert r["n_live"] == 1 and r["n_cutoffs"] == 1
    assert r["naive_recall"] == 1.0


def test_percentile_interpolates_between_ranks():
    from harness.evals.stories import _percentile

    assert _percentile([1, 2, 3, 4, 5], 50) == 3
    assert _percentile([0, 10], 10) == 1.0
    assert _percentile([4], 90) == 4


def test_story_evidence_uses_peers_after_the_post_against_their_own_medians():
    from harness.evals.stories import liveness_check, story_evidence

    posts = [post(1, "@a", "Opus 5.5 is here", 0), post(2, "@b", "Opus 5.5 now in our CLI", 2),
             post(3, "@c", "Opus 5.5 lands today", 5)]
    posts[0].engagement, posts[1].engagement, posts[2].engagement = 0, 99, 9
    # Baselines: @b's median is 9 and @c's is 9, so their posts did 10x and 1x their usual.
    pool = posts + [post(10 + i, acct, "old post", -100) for i, acct in enumerate(["@b", "@b", "@c", "@c"])]
    for q in pool[3:]:
        q.engagement = 9
    ev = dict((e.action_id, e) for _, e in story_evidence(posts, {"opus": ["opus 5.5"]}, pool))
    first = ev[posts[0].id]
    assert first.n_peer_posts == 2 and first.story_peer_ratio == pytest.approx((10.0 + 1.0) / 2)
    assert ev[posts[2].id].n_peer_posts == 0 and ev[posts[2].id].story_peer_ratio is None
    # @a has no other posts, so no median of its own; only @b's post has both a baseline and a peer.
    assert liveness_check(list(ev.values()), min_peers=1)["n_posts"] == 1
