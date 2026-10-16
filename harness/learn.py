"""Learn: turn review verdicts, edits and recorded outcomes into tested lessons, then rewrite memory/lessons.md.

Edits become procedure notes, one per mind. Approved and killed drafts become labeled episodes, and the
reflection job contrasts them within each narrative. memory/lessons.md lists the lessons that passed a held-out
test, so the minds keep reading the same file they read today.
"""

from harness import config, memory
from harness.learning import reviews
from harness.learning.kinds import save
from harness.learning.memstore_adapter import MemstoreAdapter
from harness.learning.ranking import HOME, render_lessons, today_utc
from harness.learning.reflect import reflect
from harness.learning.settings import load_settings
from harness.learning.store import flush, get_store
from harness.llm import LLM

REVIEW_DEFINITION = (
    "success = a human reviewer approved the draft, possibly after editing it, or recorded metrics say it "
    "succeeded; failure = the reviewer killed it, or recorded metrics say it failed"
)


def learn(llm: LLM, store=None) -> str | None:
    store = store or get_store()
    rows = memory.run_log()
    summaries = reviews.summarize(memory.review_history())
    procedures = reviews.procedures(summaries, rows)
    for procedure in procedures:
        save(store, procedure)
    n_edits = sum(len(p.pairs) for p in procedures)
    # A critic veto is the model grading itself, so it never labels an episode for reflection.
    episodes = [e for e in reviews.run_episodes(rows, summaries) if e.outcome() and e.outcome().source != "eval"]
    if not episodes and not n_edits:
        print("[learn] no reviews or labeled outcomes yet: review drafts with `python -m harness review` first")
        return None
    print(f"[learn] {len(episodes)} labeled drafts, {n_edits} edits in {len(procedures)} procedure notes")
    # Our own drafts carry no boost flags: nothing pays to push a draft that was never posted.
    settings = load_settings()
    settings.reflect.require_boost_flags = False
    settings.reflect.label_source = "review"
    if isinstance(store, MemstoreAdapter):
        # Reflections cite drafts as ep-<action id> episodes, and Krish's store refuses a reflection or lesson
        # whose cited episodes it can't find. His round_end writes those episodes, and nothing else calls it.
        store.write_run_episodes(sorted({r["run_id"] for r in rows if r["review"] or r["metrics"]}))
    reflect(episodes, llm, store, definition=REVIEW_DEFINITION, domain=HOME, settings=settings.reflect,
            key=reviews.by_narrative)
    text = render_lessons(store, today_utc(), settings.recall)
    config.LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.LESSONS_PATH.write_text(text, encoding="utf-8")
    flush(store)
    print(f"[learn] wrote {config.LESSONS_PATH}")
    return text
