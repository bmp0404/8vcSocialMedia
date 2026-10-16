"""Every kind must survive a write and a read unchanged, through the fake store and through the adapter codec."""

import importlib.util
import re
from dataclasses import replace

import pytest

from harness.learning import kinds
from harness.learning.kinds import (
    EditPair, Entity, Episode, Fact, Lesson, Outcome, Procedure, Reflection, check_note, load, save, slug,
)
from harness.learning.memstore_adapter import StubNote, decode_record, encode_record, from_note, to_note
from harness.learning.store import FakeStore


def samples():
    return [
        Episode(id="post-2100570682593546310", source="dataset", author_username="supabase", text="Love that guy",
                created_at="2026-09-17T13:00:52+00:00", tweet_id="2100570682593546310",
                url="https://x.com/supabase/status/2100570682593546310",
                features={"media": "photo", "is_launch": False, "engagement": 18836, "boost_suspect": None},
                observation={"narrative_id": "n1", "signals": [{"id": "s1", "metrics": {"likes": 3}}]},
                decision={"mind": "meme_lord", "siblings": [{"id": "abc", "predicted_score": 0.4}],
                          "chosen_by_bot": None},
                peers=[{"tweet_id": "1", "author_username": "x", "engagement": 5, "author_median": 3.5}],
                outcomes=[Outcome(label="success", source="dataset", raw="Super Viral",
                                  metrics={"like_count": 17542, "quote_count": 25}),
                          Outcome(label="", source="metrics", raw="+24h", metrics={"like_count": 1},
                                  at="2026-09-18T13:00:00+00:00", reasons=[])]),
        Reflection(id="refl-0123456789", claim="Bare links fail.", predicate="Is it a bare link?", outcome="failure",
                   evidence=["post-1", "post-2"], contrast=["post-3"], group="contrast:a|other",
                   domain="devtools", status="supported", rule={"feature": "n_chars", "op": "lt", "value": 60},
                   heldout={"p_value": 0.01, "effect": 0.2, "target_posts": 10, "target_with_trait": 6,
                            "other_posts": 8, "other_with_trait": 1},
                   filters={"boost_filter": "off", "thread_continuation": 3}, created="2026-10-15"),
        Lesson(id="lesson-0123456789", text="Say why the link matters.", predicate="Is it a bare link?",
               confidence=0.21, evidence=["post-1", "post-3"], reflections=["refl-0123456789"],
               valid_until="2026-11-14", rule={"feature": "text", "op": "contains_any", "value": ["link", "here"]},
               domain="devtools", domains=["devtools"], support=1, inconclusive=2,
               last_test={"p_value": 0.01, "target_posts": 10, "target_with_trait": 6, "other_posts": 8,
                          "other_with_trait": 1}, labeled_at_test=600, created="2026-10-15", updated="2026-10-15"),
        Entity(id="entity-vllm-project", name="@vllm_project", summary="46 posts", aliases=["vllm_project"],
               stats={"posts": 46, "categories": {"Deadweight": 30}}, updated="2026-10-16"),
        Fact(id="fact-0123456789", subject="entity-resend", question="Who is it for?", answer="Engineers.",
             quote="We are a team of engineers", source_url="https://resend.com", as_of="2026-10-15",
             valid_until="2026-11-14"),
        Procedure(id="procedure-review-edits-meme-lord", mind="meme_lord",
                  pairs=[EditPair(action_id="abc12345", original="a", edited="b", verdict="approved", note="n",
                                  diff={"words_added": ["b"], "verify_added": False}, at="2026-10-15T01:00:00+00:00")],
                  created="2026-10-15", updated="2026-10-15"),
    ]


@pytest.mark.parametrize("item", samples(), ids=lambda i: type(i).__name__)
def test_fake_store_round_trip(item):
    store = FakeStore()
    nid = save(store, item)
    assert nid == item.id
    assert load(store.items[nid]) == item


SAFE = re.compile(r"^[A-Za-z0-9_-]*$")


@pytest.mark.parametrize("item", samples(), ids=lambda i: type(i).__name__)
def test_adapter_codec_round_trip(item):
    kind, body, meta = item.to_memory()
    note = to_note(kind, body, meta, StubNote)
    # Krish's parser splits lists on commas and keeps only strings, floats for confidence, and string lists.
    assert SAFE.match(note.meta["record"])
    for key, value in note.meta.items():
        assert isinstance(value, (str, float, list)), key
        if isinstance(value, list):
            assert all(isinstance(v, str) and "," not in v for v in value), key
    assert "\n" not in note.title and '"' not in note.title
    back = from_note(note)
    assert back.kind == kind and back.body == body
    assert load(back) == item
    assert {k: v for k, v in back.meta.items() if k != "kind"} == {k: v for k, v in meta.items() if k != "kind"}


def test_adapter_writes_the_keys_the_store_reads():
    lesson = samples()[2]
    note = to_note(*lesson.to_memory(), StubNote)
    assert note.meta["evidence"] == ["post-1", "post-3"]
    assert note.meta["confidence"] == 0.21
    assert note.meta["valid_until"] == "2026-11-14"
    assert note.meta["source"] == "reflect"
    fact = to_note(*samples()[4].to_memory(), StubNote)
    assert fact.meta["quote"] == "We are a team of engineers" and fact.meta["source"] == "https://resend.com"
    episode = to_note(*samples()[0].to_memory(), StubNote)
    assert episode.meta["source"] == "dataset"


def test_record_survives_awkward_text():
    meta = {"text": 'a, b: [c] "d"\nnew line', "n": 3, "flag": True, "none": None, "f": 0.5}
    assert decode_record(encode_record(meta)) == meta


def test_from_note_refuses_foreign_notes():
    with pytest.raises(ValueError, match="no `record`"):
        from_note(StubNote(id="x", type="lesson", title="t", aliases=["a"], created="2026-10-15",
                           updated="2026-10-15", body=""))


@pytest.mark.skipif(importlib.util.find_spec("harness.memstore") is None, reason="harness.memstore not on this branch")
@pytest.mark.parametrize("item", samples(), ids=lambda i: type(i).__name__)
def test_real_memstore_round_trip(item, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMSTORE_PATH", str(tmp_path / "store"))
    from harness.learning.memstore_adapter import MemstoreAdapter

    store = MemstoreAdapter()
    if isinstance(item, (Reflection, Lesson)):  # the store refuses either one when it can't find a cited episode
        save(store, replace(samples()[0], id="post-1"))
    save(store, item)
    [back] = [m for m in store.list_memories(item.KIND) if m.id == item.id]
    assert load(back) == item


@pytest.mark.skipif(importlib.util.find_spec("harness.memstore") is None, reason="harness.memstore not on this branch")
def test_a_fact_quoting_a_post_with_line_breaks_reads_back_whole(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMSTORE_PATH", str(tmp_path / "store"))
    from harness.learning.memstore_adapter import MemstoreAdapter

    text = "You can do that now. 🐳\n\nPlus, every agent gets its own project.\n\nhttps://supabase.com/blog"
    fact = replace(samples()[4], id="fact-multiline", answer=text, quote=text)
    store = MemstoreAdapter()
    save(store, fact)
    [back] = [m for m in store.list_memories(Fact.KIND) if m.id == fact.id]
    assert load(back) == fact
    assert to_note(*fact.to_memory(), StubNote).meta["quote"] == " ".join(text.split())


@pytest.mark.parametrize("meta,match", [
    ({"id": "Post_1", "title": "t", "aliases": ["a"], "created": "2026-10-15", "updated": "2026-10-15"}, "kebab-case"),
    ({"id": "a" * 81, "title": "t", "aliases": ["a"], "created": "2026-10-15", "updated": "2026-10-15"}, "kebab-case"),
    ({"id": "ok", "title": " ", "aliases": ["a"], "created": "2026-10-15", "updated": "2026-10-15"}, "title"),
    ({"id": "ok", "title": "t", "aliases": [], "created": "2026-10-15", "updated": "2026-10-15"}, "aliases"),
    ({"id": "ok", "title": "t", "aliases": ["a"], "created": "2026-10-15T01:00", "updated": "2026-10-15"}, "created"),
])
def test_check_note_rejects_bad_frontmatter(meta, match):
    with pytest.raises(ValueError, match=match):
        check_note("lesson", meta)


def test_episode_without_a_date_cannot_be_saved():
    with pytest.raises(ValueError, match="created"):
        save(FakeStore(), Episode(id="ep-abc", source="run", author_username="brand", text="x"))


def test_slug_and_keywords():
    assert slug("vllm_project") == "vllm-project"
    assert slug("  Caitlin Clark!! ") == "caitlin-clark"
    assert kinds.keywords("Does the post have no media attached?") == ["media", "attached"]


def test_outcome_priority_metrics_over_review_over_dataset_over_eval():
    def ep(*outcomes):
        return Episode(id="ep-a", source="run", author_username="b", text="t", outcomes=list(outcomes))

    eval_fail = Outcome(label="failure", source="eval")
    review_ok = Outcome(label="success", source="review")
    metrics_fail = Outcome(label="failure", source="metrics")
    unlabeled_metrics = Outcome(label="", source="metrics", metrics={"like_count": 3})
    assert ep(eval_fail).label() == "failure"
    assert ep(eval_fail, review_ok).label() == "success"
    assert ep(review_ok, metrics_fail).label() == "failure"
    assert ep(review_ok, unlabeled_metrics).label() == "success"
    assert ep().label() == ""


def test_load_rejects_unknown_type():
    from harness.learning.store import Memory

    with pytest.raises(ValueError, match="no dataclass"):
        load(Memory(id="x", kind="mystery", body=""))
