from datetime import datetime, timedelta, timezone

from harness.context import boost
from harness.context.enrich import History, category
from harness.context.settings import BoostParams

P = BoostParams()
CUT = {"like_rate_high": 0.035, "repost_heavy": 0.22, "bookmarks_low": 0.02, "replies_low": 0.01}


def base(**kw):
    row = {"likes": 1000, "reposts": 100, "replies": 50, "bookmarks": 100, "views": 100_000, "engagement": 1150,
           "author_median_views": 20_000, "author_median_eng_per_view": 0.02, "author_posts_with_views": 20}
    row.update(kw)
    return row


def test_clean_post_is_none():
    out = boost.classify(base(), CUT, P)
    assert out["boost_class"] == "none" and not out["boost_suspect"]


def test_views_without_engagement_is_paid_or_external_reach():
    # 5x the author's median views (100k vs 20k) and 0.0115 eng/view <= 0.4 * 0.02.
    out = boost.classify(base(views=100_000, engagement=800), CUT, P)
    assert out["boost_paid_or_external_reach"] and out["boost_suspect"]
    assert out["boost_class"] == "paid_or_external_reach"


def test_reach_rule_needs_enough_author_posts():
    out = boost.classify(base(views=100_000, engagement=800, author_posts_with_views=4), CUT, P)
    assert not out["boost_paid_or_external_reach"]


def test_reach_rule_boundary_is_inclusive():
    # Exactly 5x views and exactly 0.4x conversion fires.
    out = boost.classify(base(views=100_000, engagement=800, author_median_eng_per_view=0.02), CUT, P)
    assert boost.classify(base(views=99_999, engagement=800), CUT, P)["boost_paid_or_external_reach"] is False
    assert out["boost_paid_or_external_reach"]


def test_like_rate_high_and_author_outlier_never_vote():
    out = boost.classify(base(likes=10_000, bookmarks=1000, replies=500, views=100_000, eng_ratio_author=50), CUT, P)
    assert out["boost_inauthentic_votes"] == 0
    assert set(out["boost_weak_signals"].split(";")) == {"like_rate_high", "author_outlier"}


def test_two_votes_make_likely_inauthentic():
    out = boost.classify(base(reposts=400, bookmarks=5), CUT, P)  # repost_heavy + bookmarks_low
    assert out["boost_likely_inauthentic"] and out["boost_class"] == "likely_inauthentic"
    assert out["boost_inauthentic_votes"] == 2


def test_one_vote_is_not_enough():
    assert not boost.classify(base(reposts=400), CUT, P)["boost_likely_inauthentic"]


def test_reply_timing_votes_need_coverage():
    row = base(reply_low_follower_share=0.7, reply_burst_share=0.6, reply_coverage=0.5)
    assert boost.classify(row, CUT, P)["boost_inauthentic_votes"] == 0
    row["reply_coverage"] = 0.9
    assert boost.classify(row, CUT, P)["boost_likely_inauthentic"]


def test_staff_amplification_is_coordinated_not_suspect():
    out = boost.classify(base(staff_share=0.3, staff_sample_n=40), CUT, P)
    assert out["boost_class"] == "coordinated_authentic"
    assert out["boost_coordinated_authentic"] and not out["boost_suspect"]


def test_inauthentic_outranks_paid_in_class():
    out = boost.classify(base(views=100_000, engagement=800, reposts=400, bookmarks=5), CUT, P)
    assert out["boost_class"] == "likely_inauthentic" and out["boost_paid_or_external_reach"]


def test_curve_flags_route_to_the_right_label():
    out = boost.classify(base(), CUT, P, curve_flags=["view_surge_low_conversion", "drip_feed", "count_drop"])
    assert out["boost_paid_or_external_reach"] and out["boost_likely_inauthentic"]


def test_ratio_cutoffs_use_quantiles_over_rows_with_volume():
    rows = [{"likes": 100, "views": 10_000 + i, "reposts": i, "replies": 5, "bookmarks": 10} for i in range(100)]
    cut = boost.ratio_cutoffs(rows, P)
    assert "like_rate_high" in cut and "repost_heavy" in cut
    assert "bookmarks_low" not in cut  # every row is under the 200-like floor


def test_reply_signals_duplicates_and_burst():
    t0 = 1_790_000_000
    reps = [{"text": "Great!", "author_followers": 5, "created_unix": t0 + i} for i in range(6)]
    reps += [{"text": f"thought {i}", "author_followers": 500, "created_unix": t0 + 3600 * i} for i in range(1, 7)]
    sig = boost.reply_signals(reps, P)
    assert sig["reply_sample_n"] == 12
    assert sig["reply_dup_share"] == 0.5
    assert sig["reply_low_follower_share"] == 0.5
    assert sig["reply_burst_share"] == 0.5


def test_category_restores_the_missing_band():
    assert [category(e) for e in (0, 10, 11, 99, 100, 749, 750)] == [
        "Deadweight", "Deadweight", "Low", "Low", "Medium Viral", "Medium Viral", "Super Viral"]


def test_history_before_is_strictly_past():
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    h = History([{"created": t0 + timedelta(hours=i), "eng": i} for i in (3, 1, 2)])
    assert [p["eng"] for p in h.before(t0 + timedelta(hours=2))] == [1]
    assert [p["eng"] for p in h.in_window(t0 + timedelta(hours=2), t0 + timedelta(hours=4))] == [2, 3]
