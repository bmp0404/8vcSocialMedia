"""Where sam/memory (harness/learning) meets Krish's store (harness/memstore), against the real store."""

from dataclasses import replace
from datetime import timedelta

import pytest

from harness import config, memory, memstore
from harness.learn import learn
from harness.learning.kinds import Episode, Lesson, save
from harness.learning.memstore_adapter import MemstoreAdapter
from harness.learning.ranking import render_lessons, today_utc
from harness.learning.settings import load_settings
from harness.llm import get_llm
from harness.models import Action


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMSTORE_PATH", str(tmp_path / "store"))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(config, "LESSONS_PATH", tmp_path / "lessons.md")
    return tmp_path


def test_learn_writes_the_run_episodes_its_reflections_cite(isolated):
    drafts = [Action(type="post", text=f"draft {i}", mind="meme_lord", narrative_id=f"n{i % 2}") for i in range(8)]
    memory.save_run(drafts)
    for i, a in enumerate(drafts):
        memory.set_review(a.id, "approved" if i % 2 else "killed")

    learn(get_llm("mock"), store=MemstoreAdapter())  # raised MemoryValidationError before round_end ran first

    for a in drafts:
        note = memstore.load(f"ep-{a.id}")
        assert note is not None and note.meta["source"] == "run"
        assert note.meta["review_status"] == ("approved" if drafts.index(a) % 2 else "killed")
    assert memstore.list_notes("reflection"), "the mock reflection was not saved"


def lesson_with_evidence() -> Lesson:
    today = today_utc()
    return Lesson(id="lesson-seam-test", text="Name the player in the first five words.",
                  predicate="Is the player named early?", confidence=0.9, evidence=["post-1", "post-2", "post-3"],
                  reflections=["refl-seam-test"],
                  valid_until=(today + timedelta(days=30)).isoformat(), domain="brand", created=today.isoformat(),
                  updated=today.isoformat())


def test_lessons_show_a_tested_lesson_once_and_keep_the_store_notes(isolated):
    store = MemstoreAdapter()
    episode = Episode(id="post-1", source="dataset", author_username="someone", text="a post",
                      created_at="2026-10-01T12:00:00+00:00")
    save(store, episode)
    save(store, lesson_with_evidence())
    config.LESSONS_PATH.write_text(render_lessons(store, today_utc(), load_settings().recall), encoding="utf-8")
    # A note Krish's API wrote directly, with no `record`: only prompt_block() can show it.
    hand = memstore.Note(id="hand-written-procedure", type="procedure", title="Hand-written rule", aliases=["rule"],
                         created="2026-10-15", updated="2026-10-15", body="- **Rule:** keep it short.\n")
    memstore.write_note(hand)

    text = config.lessons()

    assert text.count("Name the player in the first five words") == 1
    assert "Hand-written rule (procedure, hand-written-procedure)" in text


def test_prompt_block_exclude_drops_only_matching_notes(isolated):
    store = MemstoreAdapter()
    save(store, Episode(id="post-1", source="dataset", author_username="someone", text="a post",
                        created_at="2026-10-01T12:00:00+00:00"))
    save(store, lesson_with_evidence())
    save(store, replace(lesson_with_evidence(), id="lesson-seam-other", text="Ask one question at the end."))

    block = memstore.prompt_block(exclude=lambda n: n.id == "lesson-seam-test")

    assert "Ask one question at the end" in block
    assert "Name the player in the first five words" not in block
