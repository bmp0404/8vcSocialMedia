from datetime import datetime, timedelta, timezone

import pytest

from harness.learning.dataset import (
    CATEGORY_LABELS,
    LAUNCH_PATTERN,
    account_profiles,
    attach_context,
    exemplars,
    load_dataset,
    mark_threads_and_age,
    public_metrics,
    row_to_episode,
    snowflake_time,
    tweet_id,
)
from harness.learning.kinds import Entity, Episode, Outcome, save
from harness.learning.store import FakeStore

UTC = timezone.utc

# (1724551110972149759 >> 22) = 411165025466; + 1288834974657 = 1700000000123 ms
# = 2023-11-14T22:13:20.123Z. The low 22 bits (4095) are the worker and sequence and don't affect the time.
TID = "1724551110972149759"
TID_TIME = datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)


def raw_row(**overrides) -> dict:
    row = {
        "Category": "Deadweight",
        "Account": "@cline",
        "Post Content": "Introducing Cline v3.0 #ai https://t.co/abc",
        "Media": "photo",
        "Likes": "3",
        "Replies": "1",
        "Retweets": "2",
        "Engagement": "7",
        "Post URL": f"https://x.com/cline/status/{TID}",
        "Date": "2023-11-14",
        "Segment": "agents",
    }
    row.update(overrides)
    return row


def dataset_episode(pid: str, author: str, engagement: int, category: str, created_at: str = "2026-09-01T10:00:00+00:00",
                    media: str = "none", segment: str = "", thread: bool | None = None) -> Episode:
    features: dict = {"engagement": engagement, "media": media}
    if segment:
        features["segment"] = segment
    if thread is not None:
        features["thread_continuation"] = thread
    return Episode(id=f"post-{pid}", source="dataset", author_username=author, text=f"post {pid}", created_at=created_at,
                   features=features, outcomes=[Outcome(label=CATEGORY_LABELS[category], source="dataset", raw=category)])


# tweet_id


@pytest.mark.parametrize("url, expected", [
    ("https://x.com/cline/status/1234567890", "1234567890"),
    ("https://twitter.com/zeddotdev/status/42?s=20", "42"),
    ("https://x.com/cline/status/99/photo/1", "99"),
])
def test_tweet_id_extracts_the_status_id(url, expected):
    assert tweet_id(url) == expected


@pytest.mark.parametrize("url", ["https://x.com/cline", "https://x.com/cline/statuses/", ""])
def test_tweet_id_raises_without_a_status_id(url):
    with pytest.raises(ValueError, match="no status id in post URL"):
        tweet_id(url)


# public_metrics


def test_public_metrics_derives_quotes_as_engagement_minus_likes_replies_retweets():
    row = {"Likes": "10", "Replies": "2", "Retweets": "3", "Engagement": "20"}
    assert public_metrics(row) == {"like_count": 10, "reply_count": 2, "retweet_count": 3, "quote_count": 5}


def test_public_metrics_keeps_a_zero_quote_count():
    row = {"Likes": "10", "Replies": "2", "Retweets": "3", "Engagement": "15"}
    assert public_metrics(row) == {"like_count": 10, "reply_count": 2, "retweet_count": 3, "quote_count": 0}


def test_public_metrics_omits_quote_count_when_negative():
    row = {"Likes": "10", "Replies": "2", "Retweets": "3", "Engagement": "14"}
    assert public_metrics(row) == {"like_count": 10, "reply_count": 2, "retweet_count": 3}


# snowflake_time


def test_snowflake_time_decodes_a_known_id():
    assert snowflake_time(TID) == TID_TIME


def test_snowflake_time_of_the_epoch_itself():
    # id 0 encodes 0 ms after the Twitter epoch, 1288834974657 ms = 2010-11-04T01:42:54.657Z.
    assert snowflake_time("0") == datetime(2010, 11, 4, 1, 42, 54, 657000, tzinfo=UTC)


# mark_threads_and_age

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def timed(pid: str, author: str, seconds: int) -> Episode:
    created = (T0 + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    return Episode(id=f"post-{pid}", source="dataset", author_username=author, text="x", created_at=created)


def test_mark_threads_flags_posts_under_180_seconds_after_the_same_authors_previous_post():
    eps = [
        timed("4", "cline", 580),  # 180 s after post-3: not under the gap
        timed("2", "cline", 100),  # 100 s after post-1: continuation
        timed("5", "zed", 50),  # 50 s after cline's first post, but zed's own first post
        timed("1", "cline", 0),  # cline's first post
        timed("3", "cline", 400),  # 300 s after post-2
    ]
    mark_threads_and_age(eps, collected_at=T0 + timedelta(hours=10))
    flags = {ep.id: ep.features["thread_continuation"] for ep in eps}
    assert flags == {"post-1": False, "post-2": True, "post-3": False, "post-4": False, "post-5": False}


def test_mark_threads_computes_label_age_hours_against_collected_at():
    eps = [timed("1", "cline", 0), timed("2", "cline", 100), timed("3", "cline", 400), timed("4", "cline", 580),
           timed("5", "zed", 50)]
    mark_threads_and_age(eps, collected_at=T0 + timedelta(hours=10))
    # (36000 - offset) / 3600, rounded to one decimal.
    ages = {ep.id: ep.features["label_age_hours"] for ep in eps}
    assert ages == {"post-1": 10.0, "post-2": 10.0, "post-3": 9.9, "post-4": 9.8, "post-5": 10.0}


def test_mark_threads_defaults_collection_time_to_the_newest_post():
    eps = [timed("1", "cline", 0), timed("2", "zed", 720)]
    mark_threads_and_age(eps)
    assert {ep.id: ep.features["label_age_hours"] for ep in eps} == {"post-1": 0.2, "post-2": 0.0}


def test_mark_threads_respects_a_custom_gap():
    eps = [timed("1", "cline", 0), timed("2", "cline", 100)]
    mark_threads_and_age(eps, thread_gap_seconds=60)
    assert eps[1].features["thread_continuation"] is False


# LAUNCH_PATTERN


@pytest.mark.parametrize("text", [
    "Introducing Cline for JetBrains",
    "Zed is now available on Linux",
    "Shipping v2.1 with faster indexing",
    "We just shipped tabs",
    "Public beta opens today",
    "Release notes for 1.4 are up",
    "Now live: agent mode",
    "v10.0.3 fixes the crash",
])
def test_launch_pattern_matches_launch_wording(text):
    assert LAUNCH_PATTERN.search(text)


@pytest.mark.parametrize("text", [
    "Reintroducing the old keybindings",  # no word boundary before "introduc"
    "Read the announcement thread",  # "announcement" is not announce/announces/announced/announcing
    "Our launchpad for new hires",  # "launchpad" runs past "launch"
    "v2 is coming",  # a version needs at least one dot
    "dev2.1 branch",  # no word boundary before the v
    "It is now including tests",  # "now in" must end at a word boundary
    "This is outrageous",  # "is out" must end at a word boundary
    "Thanks for the great feedback",
])
def test_launch_pattern_rejects_lookalikes(text):
    assert LAUNCH_PATTERN.search(text) is None


# row_to_episode


def test_row_to_episode_builds_the_full_episode():
    ep = row_to_episode(raw_row(), "dataset")
    assert ep == Episode(
        id=f"post-{TID}",
        source="dataset",
        author_username="cline",
        text="Introducing Cline v3.0 #ai https://t.co/abc",
        created_at="2023-11-14T22:13:20+00:00",
        tweet_id=TID,
        url=f"https://x.com/cline/status/{TID}",
        features={"media": "photo", "segment": "agents", "engagement": 7, "has_link": True,
                  "starts_with_mention": False, "n_chars": 43, "n_hashtags": 1, "is_launch": True},
        outcomes=[Outcome(label="failure", source="dataset", raw="Deadweight",
                          metrics={"like_count": 3, "reply_count": 1, "retweet_count": 2, "quote_count": 1})],
    )


@pytest.mark.parametrize("category, label", [("Deadweight", "failure"), ("Medium Viral", "success"),
                                             ("Super Viral", "success")])
def test_row_to_episode_labels_categories(category, label):
    ep = row_to_episode(raw_row(Category=category), "dataset")
    assert ep.label() == label
    assert ep.outcomes[0].raw == category


def test_row_to_episode_flags_a_leading_mention():
    ep = row_to_episode(raw_row(**{"Post Content": "  @zeddotdev thanks!"}), "dataset")
    assert ep.features["starts_with_mention"] is True
    assert ep.features["has_link"] is False
    assert ep.features["is_launch"] is False


def test_row_to_episode_raises_on_an_unknown_category():
    with pytest.raises(ValueError, match="unknown Category 'Viral'"):
        row_to_episode(raw_row(Category="Viral"), "dataset")


def test_row_to_episode_saves_with_valid_frontmatter():
    store = FakeStore()
    mid = save(store, row_to_episode(raw_row(), "dataset"))
    meta = store.items[mid].meta
    assert mid == f"post-{TID}"
    assert meta["aliases"] == ["cline", TID, "photo", "launch"]
    assert meta["tags"] == ["dataset", "failure"]
    assert (meta["created"], meta["updated"]) == ("2023-11-14", "2023-11-14")


# load_dataset


def test_load_dataset_raises_on_missing_columns(tmp_path):
    path = tmp_path / "posts.csv"
    path.write_text("Category,Account,Post URL\nDeadweight,@cline,https://x.com/cline/status/1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="is missing columns"):
        load_dataset(path)


# account_profiles


def test_account_profiles_counts_outcomes_and_takes_the_median():
    eps = [
        dataset_episode("1", "cline", 5, "Deadweight", "2026-09-01T10:00:00+00:00", segment="agents"),
        dataset_episode("2", "cline", 200, "Medium Viral", "2026-09-03T10:00:00+00:00", media="photo", segment="agents"),
        dataset_episode("3", "cline", 900, "Super Viral", "2026-09-02T10:00:00+00:00", media="video", segment="ide"),
        dataset_episode("4", "zed", 4, "Deadweight", "2026-09-05T10:00:00+00:00"),
        dataset_episode("5", "zed", 8, "Deadweight", "2026-09-04T10:00:00+00:00"),
    ]
    profiles = account_profiles(eps, "devtools")
    assert profiles == [
        Entity(
            id="entity-cline", name="@cline", entity_type="account", aliases=["cline"], updated="2026-09-03",
            summary=("@cline (devtools): 3 posts from 2026-09-01T10:00:00+00:00 to 2026-09-03T10:00:00+00:00, "
                     "2 succeeded and 1 failed; median engagement 200, max 900."),
            stats={"posts": 3, "successes": 2, "failures": 1,
                   "categories": {"Deadweight": 1, "Medium Viral": 1, "Super Viral": 1},
                   "median_engagement": 200, "max_engagement": 900, "media_share": 0.667,
                   "first_post": "2026-09-01T10:00:00+00:00", "last_post": "2026-09-03T10:00:00+00:00",
                   "segments": ["agents", "ide"]},
        ),
        Entity(
            id="entity-zed", name="@zed", entity_type="account", aliases=["zed"], updated="2026-09-05",
            summary=("@zed (devtools): 2 posts from 2026-09-04T10:00:00+00:00 to 2026-09-05T10:00:00+00:00, "
                     "0 succeeded and 2 failed; median engagement 6.0, max 8."),
            stats={"posts": 2, "successes": 0, "failures": 2, "categories": {"Deadweight": 2},
                   "median_engagement": 6.0, "max_engagement": 8, "media_share": 0.0,
                   "first_post": "2026-09-04T10:00:00+00:00", "last_post": "2026-09-05T10:00:00+00:00",
                   "segments": []},
        ),
    ]
    store = FakeStore()
    assert [save(store, p) for p in profiles] == ["entity-cline", "entity-zed"]


def test_account_profiles_slugs_the_entity_id():
    profiles = account_profiles([dataset_episode("1", "Some_User", 5, "Deadweight")], "devtools")
    assert (profiles[0].id, profiles[0].name, profiles[0].aliases) == ("entity-some-user", "@Some_User", ["Some_User"])


# exemplars


def test_exemplars_takes_top_and_bottom_n_per_category_and_skips_thread_continuations():
    eps = [
        dataset_episode("d0", "a", 0, "Deadweight", thread=True),
        dataset_episode("d1", "a", 1, "Deadweight", thread=False),
        dataset_episode("d2", "a", 2, "Deadweight"),
        dataset_episode("d3", "a", 3, "Deadweight"),
        dataset_episode("d4", "a", 4, "Deadweight"),
        dataset_episode("d5", "a", 5, "Deadweight"),
        dataset_episode("m1", "a", 150, "Medium Viral"),
        dataset_episode("m2", "a", 300, "Medium Viral"),
        dataset_episode("m3", "a", 120, "Medium Viral"),
        dataset_episode("s1", "a", 1000, "Super Viral"),
        dataset_episode("s2", "a", 5000, "Super Viral", thread=True),
    ]
    picked = exemplars(eps, per_category=2)
    assert [ep.id for ep in picked] == ["post-d1", "post-d2", "post-d4", "post-d5", "post-m1", "post-m2", "post-m3",
                                        "post-s1"]


def test_exemplars_breaks_engagement_ties_by_id():
    eps = [dataset_episode(pid, "a", 7, "Deadweight") for pid in ("c", "a", "d", "b")]
    assert [ep.id for ep in exemplars(eps, per_category=1)] == ["post-a", "post-d"]


# attach_context

CONTEXT_CSV = (
    "Post URL,Likes,eng_ratio_author_median,boost_suspect,followers,sci,label,empty_col\n"
    "https://x.com/a/status/1,999,1.5,true,1200,1e3,hello,\n"
    "https://x.com/b/status/2,5,0.25,FALSE,7,2.5E1,,\n"
    "https://x.com/c/status/9,1,2,true,3,1,x,\n"
)


def context_episodes() -> list[Episode]:
    return [Episode(id=f"post-{n}", source="dataset", author_username="a", text="x", url=f"https://x.com/{u}/status/{n}",
                    features={"media": "none"})
            for u, n in (("a", 1), ("b", 2), ("c", 3))]


def test_attach_context_joins_on_post_url_and_parses_values(tmp_path):
    path = tmp_path / "context.csv"
    path.write_text(CONTEXT_CSV, encoding="utf-8")
    eps = context_episodes()
    assert attach_context(eps, path) == 2
    one, two, three = (ep.features for ep in eps)
    assert one == {"media": "none", "eng_ratio_author_median": 1.5, "boost_suspect": True, "followers": 1200,
                   "sci": 1000.0, "label": "hello"}
    assert two == {"media": "none", "eng_ratio_author_median": 0.25, "boost_suspect": False, "followers": 7,
                   "sci": 25.0}
    assert three == {"media": "none"}
    # Dict equality treats True == 1 and 1200 == 1200.0, so check the parsed types too.
    assert one["boost_suspect"] is True and two["boost_suspect"] is False
    assert type(one["followers"]) is int and type(one["sci"]) is float
    assert type(one["eng_ratio_author_median"]) is float


def test_attach_context_raises_without_a_post_url_column(tmp_path):
    path = tmp_path / "context.csv"
    path.write_text("url,boost_suspect\nhttps://x.com/a/status/1,true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="has no 'Post URL' column"):
        attach_context(context_episodes(), path)
