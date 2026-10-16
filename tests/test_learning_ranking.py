import json
from collections import Counter
from dataclasses import replace
from datetime import date

import pytest

from harness.learning.kinds import EditPair, Entity, Episode, Fact, Lesson, Outcome, Procedure, Reflection, save
from harness.learning.ranking import rank, recency, render_lessons, revalidation_queue, round_memory, trust
from harness.learning.settings import MemorySettings, RecallSettings, ReflectSettings, load_settings
from harness.learning.store import FakeStore, Memory
from harness.models import Narrative

TODAY = date(2026, 10, 15)
S = RecallSettings()
HELDOUT = {"target_posts": 40, "target_with_trait": 31, "other_posts": 35, "other_with_trait": 9}


def lesson(lid: str, confidence: float = 0.5, *, status: str = "active", valid_until: str = "2026-12-01",
           domain: str = "brand", scope: str = "domain", updated: str = "2026-10-15",
           text: str = "Lead with a concrete number", polarity: str = "avoid", last_test: dict | None = None,
           labels_passed: tuple = ("review",)) -> Lesson:
    return Lesson(id=lid, text=text, predicate="Does the post open with a number?", confidence=confidence,
                  evidence=["post-1"], reflections=["reflection-1"], valid_until=valid_until, scope=scope,
                  domain=domain, polarity=polarity, status=status, last_test=last_test or {}, created="2026-09-01",
                  updated=updated, labels_passed=list(labels_passed))


def episode(eid: str, label: str, *, author: str = "brand", boost: bool | None = None, domain: str = "",
            created_at: str = "2026-10-15T12:00:00+00:00", text: str = "Fever win tonight") -> Episode:
    features: dict = {}
    if boost is not None:
        features["boost_suspect"] = boost
    if domain:
        features["domain"] = domain
    outcome = Outcome(label=label, source="review", raw="approved" if label == "success" else "killed") if label \
        else Outcome(label="", source="eval", raw="0.4")
    return Episode(id=eid, source="run", author_username=author, text=text, created_at=created_at, features=features,
                   outcomes=[outcome])


def reflection(rid: str, status: str, *, domain: str = "", scope: str = "general",
               claim: str = "Posts that open with a number do better") -> Reflection:
    return Reflection(id=rid, claim=claim, predicate="Does the post open with a number?", outcome="success",
                      evidence=["post-1"], contrast=["post-2"], group="contrast:brand", scope=scope, domain=domain,
                      status=status, created="2026-10-01")


def fact(fid: str, *, status: str = "current", valid_until: str = "", as_of: str = "2026-10-15",
         subject: str = "entity-fever", question: str = "What is the Fever record?",
         answer: str = "The Fever are 20-10.") -> Fact:
    return Fact(id=fid, subject=subject, question=question, answer=answer, quote=answer,
                source_url="https://example.com/standings", as_of=as_of, valid_until=valid_until, status=status)


def entity(eid: str, name: str, summary: str) -> Entity:
    return Entity(id=eid, name=name, summary=summary, entity_type="team", updated="2026-10-01")


def procedure() -> Procedure:
    return Procedure(id="procedure-review-edits-hype", mind="hype",
                     pairs=[EditPair(action_id="a1", original="Draft", edited="Kept", verdict="approved",
                                     at="2026-10-01T09:00:00+00:00")],
                     created="2026-10-01", updated="2026-10-01")


def mem(item, score: float = 0.0) -> Memory:
    """Save through the real frontmatter, then read the stored note back, with a search score."""
    store = FakeStore()
    return replace(store.items[save(store, item)], score=score)


def stored(*items) -> FakeStore:
    store = FakeStore()
    for item in items:
        save(store, item)
    return store


# trust: lessons


def test_trust_of_an_active_home_lesson_is_its_confidence():
    assert trust(mem(lesson("lesson-a", 0.5)), TODAY, S) == 0.5


def test_trust_treats_an_empty_domain_as_home():
    assert trust(mem(lesson("lesson-a", 0.5, domain="")), TODAY, S) == 0.5


@pytest.mark.parametrize("confidence, expected", [(1.7, 1.0), (-0.2, 0.0)])
def test_trust_clips_lesson_confidence(confidence, expected):
    assert trust(mem(lesson("lesson-a", confidence)), TODAY, S) == expected


@pytest.mark.parametrize("status", ["retired", "stale"])
def test_trust_excludes_an_inactive_lesson(status):
    assert trust(mem(lesson("lesson-a", status=status)), TODAY, S) is None


@pytest.mark.parametrize("valid_until, expected", [("2026-10-14", None), ("2026-10-15", None), ("2026-10-16", 0.5)])
def test_trust_excludes_a_lesson_due_today_or_earlier(valid_until, expected):
    assert trust(mem(lesson("lesson-a", valid_until=valid_until)), TODAY, S) == expected


def test_trust_excludes_a_domain_scoped_lesson_from_another_domain():
    assert trust(mem(lesson("lesson-a", domain="devtools", scope="domain")), TODAY, S) is None


def test_trust_scales_a_general_lesson_from_another_domain():
    m = mem(lesson("lesson-a", 0.5, domain="devtools", scope="general"))
    assert trust(m, TODAY, S) == pytest.approx(0.3)  # 0.5 * other_domain_factor 0.6
    assert trust(m, TODAY, RecallSettings(other_domain_factor=0.25)) == pytest.approx(0.125)


def test_trust_gives_a_lesson_full_weight_in_an_added_home_domain():
    m = mem(lesson("lesson-a", 0.5, domain="devtools", scope="domain"))
    assert trust(m, TODAY, RecallSettings(home_domains=["brand", "devtools"])) == 0.5


# trust: episodes


def test_trust_of_a_labeled_home_episode_is_one():
    assert trust(mem(episode("ep-a", "success")), TODAY, S) == 1.0
    assert trust(mem(episode("ep-b", "failure", domain="brand")), TODAY, S) == 1.0


def test_trust_halves_a_boost_suspect_episode():
    assert trust(mem(episode("ep-a", "success", boost=True)), TODAY, S) == 0.5
    assert trust(mem(episode("ep-b", "success", boost=False)), TODAY, S) == 1.0


def test_trust_applies_other_author_factor_to_other_authors():
    s = RecallSettings(other_author_factor=0.3)
    assert trust(mem(episode("ep-a", "success", author="cline")), TODAY, s) == pytest.approx(0.3)
    assert trust(mem(episode("ep-b", "success", author="cline", boost=True)), TODAY, s) == pytest.approx(0.15)
    assert trust(mem(episode("ep-c", "success", author="cline")), TODAY, RecallSettings(home_authors=["cline"])) == 1.0


def test_trust_excludes_an_episode_from_another_domain():
    assert trust(mem(episode("ep-a", "success", domain="devtools")), TODAY, S) is None
    s = RecallSettings(home_domains=["brand", "devtools"], other_author_factor=0.3)
    assert trust(mem(episode("ep-b", "success", author="cline", domain="devtools")), TODAY, s) == pytest.approx(0.3)


def test_trust_excludes_an_unlabeled_episode():
    assert trust(mem(episode("ep-a", "")), TODAY, S) is None


# trust: reflections, facts and other kinds


def test_trust_of_a_supported_reflection_is_the_kind_trust():
    assert trust(mem(reflection("reflection-a", "supported")), TODAY, S) == 0.5


@pytest.mark.parametrize("status", ["rejected", "inconclusive", "untested"])
def test_trust_excludes_a_reflection_that_is_not_supported(status):
    assert trust(mem(reflection("reflection-a", status)), TODAY, S) is None


def test_trust_applies_the_domain_rule_to_reflections():
    assert trust(mem(reflection("reflection-a", "supported", domain="devtools", scope="domain")), TODAY, S) is None
    general = mem(reflection("reflection-b", "supported", domain="devtools", scope="general"))
    assert trust(general, TODAY, S) == pytest.approx(0.3)


def test_trust_of_facts():
    assert trust(mem(fact("fact-a")), TODAY, S) == 1.0
    assert trust(mem(fact("fact-b", valid_until="2026-11-01")), TODAY, S) == 1.0
    assert trust(mem(fact("fact-c", status="superseded")), TODAY, S) is None
    assert trust(mem(fact("fact-d", valid_until="2026-10-01")), TODAY, S) is None


def test_trust_of_entities_and_procedures_is_the_kind_trust():
    assert trust(mem(entity("entity-fever", "Indiana Fever", "WNBA team.")), TODAY, S) == 0.8
    assert trust(mem(procedure()), TODAY, S) == 1.0


def test_trust_excludes_a_kind_without_a_trust_value():
    assert trust(Memory(id="note-a", kind="note", body="x", meta={}), TODAY, S) is None


# recency


@pytest.mark.parametrize("updated, expected", [
    ("2026-10-15", 1.0),
    ("2026-09-15", 0.5),  # 30 days, one half-life
    ("2026-08-16", 0.25),  # 60 days
    ("2026-10-20", 1.0),  # a future date counts as today
])
def test_recency_halves_every_half_life(updated, expected):
    assert recency(mem(lesson("lesson-a", updated=updated)), TODAY, S) == expected


def test_recency_uses_half_life_days():
    m = mem(lesson("lesson-a", updated="2026-09-15"))
    assert recency(m, TODAY, RecallSettings(half_life_days=10)) == 0.125


def test_recency_of_an_undated_memory():
    m = Memory(id="note-a", kind="entity", body="x", meta={})
    assert recency(m, TODAY, S) == 0.5
    assert recency(m, TODAY, RecallSettings(undated_recency=0.2)) == 0.2


def test_recency_reads_a_facts_as_of_date():
    assert recency(mem(fact("fact-a", as_of="2026-09-15")), TODAY, S) == 0.5


# rank


def test_rank_min_max_scales_relevance_and_orders_by_score_then_id():
    s = RecallSettings(kind_caps={"fact": 10})
    candidates = [mem(fact("fact-a"), 2.0), mem(fact("fact-b"), 4.0), mem(fact("fact-c"), 6.0), mem(fact("fact-d"), 0.0)]
    picks = rank(candidates, TODAY, s)
    # relevance + trust 1.0 + recency 1.0; fact-a (lowest positive score) and fact-d (no score) tie at 2.0.
    assert [(p.id, p.score, p.why["relevance"]) for p in picks] == [
        ("fact-c", 3.0, 1.0), ("fact-b", 2.5, 0.5), ("fact-a", 2.0, 0.0), ("fact-d", 2.0, 0.0)]
    assert picks[0].why == {"relevance": 1.0, "trust": 1.0, "recency": 1.0, "label": ""}


def test_rank_gives_a_lone_scored_candidate_full_relevance():
    picks = rank([mem(fact("fact-a"), 3.0), mem(fact("fact-b"), 0.0)], TODAY, S)
    assert [(p.id, p.why["relevance"]) for p in picks] == [("fact-a", 1.0), ("fact-b", 0.0)]


def test_rank_applies_weights():
    s = RecallSettings(weight_relevance=2.0, weight_trust=0.5, weight_recency=0.0)
    picks = rank([mem(fact("fact-a"), 5.0), mem(lesson("lesson-a", 0.4))], TODAY, s)
    assert [(p.id, p.score) for p in picks] == [("fact-a", 2.5), ("lesson-a", 0.2)]


def test_rank_caps_each_kind():
    lessons = [mem(lesson(f"lesson-{i}", conf)) for i, conf in enumerate((0.1, 0.5, 0.3, 0.4, 0.2))]
    assert [p.id for p in rank(lessons, TODAY, S)] == ["lesson-1", "lesson-3", "lesson-2", "lesson-4"]
    assert [p.id for p in rank(lessons, TODAY, RecallSettings(kind_caps={"lesson": 2}))] == ["lesson-1", "lesson-3"]


def test_rank_drops_kinds_without_a_cap():
    assert rank([mem(fact("fact-a"))], TODAY, RecallSettings(kind_caps={"lesson": 4})) == []


def test_rank_keeps_at_most_one_success_and_one_failure_episode():
    candidates = [
        mem(episode("ep-s3", "success", created_at="2026-08-16T12:00:00+00:00")),
        mem(episode("ep-s1", "success", created_at="2026-10-15T12:00:00+00:00")),
        mem(episode("ep-f2", "failure", created_at="2026-09-15T12:00:00+00:00")),
        mem(episode("ep-s2", "success", created_at="2026-09-15T12:00:00+00:00")),
        mem(episode("ep-f1", "failure", created_at="2026-10-15T12:00:00+00:00")),
    ]
    picks = rank(candidates, TODAY, RecallSettings(kind_caps={"episode": 5}))
    assert [(p.id, p.score, p.why["label"]) for p in picks] == [("ep-f1", 2.0, "failure"), ("ep-s1", 2.0, "success")]


def test_rank_keeps_one_episode_when_all_share_a_label():
    candidates = [mem(episode("ep-s1", "success")), mem(episode("ep-s2", "success"))]
    assert [p.id for p in rank(candidates, TODAY, S)] == ["ep-s1"]


def test_rank_skips_untrusted_and_duplicate_candidates():
    a = mem(fact("fact-a"), 2.0)
    picks = rank([a, a, mem(fact("fact-b", status="superseded"), 9.0)], TODAY, S)
    assert [p.id for p in picks] == ["fact-a"]
    # The superseded fact is excluded before scaling, so the only survivor tops the relevance scale.
    assert picks[0].why["relevance"] == 1.0


# round_memory


def round_store() -> FakeStore:
    return stored(
        lesson("lesson-number", 0.6, text="Lead with a concrete number"),
        lesson("lesson-question", 0.4, text="Ask one question at the end"),
        lesson("lesson-old", 0.9, status="retired", text="Post at noon"),
        lesson("lesson-due", 0.9, valid_until="2026-10-01", text="Tag the league"),
        fact("fact-fever-record", as_of="2026-10-10"),
        fact("fact-lottery", subject="entity-wnba", question="When is the draft lottery?",
             answer="The lottery is in November."),
        episode("ep-win", "success", text="Fever win tonight, Clark drops 30", created_at="2026-10-01T12:00:00+00:00"),
        episode("ep-loss", "failure", text="Fever lose again", created_at="2026-10-02T12:00:00+00:00"),
        reflection("reflection-fever", "rejected", claim="Fever posts win"),
        entity("entity-fever", "Indiana Fever", "Indiana Fever, the WNBA team."),
        entity("entity-wnba", "WNBA", "The league."),
    )


NARRATIVES = [Narrative(id="n1", title="Fever clinch playoff spot", summary="Fever beat the Sky")]
DUE = [{"id": "lesson-due", "kind": "lesson", "text": "Tag the league", "valid_until": "2026-10-01"}]


def test_round_memory_off_returns_no_picks():
    out = round_memory(round_store(), NARRATIVES, RecallSettings(mode="off"), today=TODAY)
    assert out == {"mode": "off", "action": {"n1": {"picks": [], "text": "(no relevant memories yet)"}},
                   "observe": {"recheck": DUE}}


def test_round_memory_on_returns_ranked_picks_and_rendered_text():
    out = round_memory(round_store(), NARRATIVES, S, today=TODAY)
    assert out["mode"] == "on"
    assert out["observe"] == {"recheck": DUE}
    picks = out["action"]["n1"]["picks"]
    assert sorted(p["id"] for p in picks) == ["entity-fever", "ep-loss", "ep-win", "fact-fever-record",
                                              "lesson-number", "lesson-question"]
    assert [(p["score"], p["id"]) for p in picks] == sorted(((p["score"], p["id"]) for p in picks),
                                                            key=lambda x: (-x[0], x[1]))
    lines = out["action"]["n1"]["text"].splitlines()
    assert lines[:5] == [
        "LESSONS (each tested on held-out posts):",
        "- Lead with a concrete number (not tested on held-out posts) [lesson-number]",
        "- Ask one question at the end (not tested on held-out posts) [lesson-question]",
        "FACTS (verbatim from the source):",
        '- The Fever are 20-10. ("The Fever are 20-10.", https://example.com/standings) [fact-fever-record]',
    ]
    assert lines[5] == "SIMILAR PAST POSTS:"
    assert sorted(lines[6:8]) == ["- failure: @brand post: Fever lose again [ep-loss]",
                                  "- success: @brand post: Fever win tonight, Clark drops 30 [ep-win]"]
    assert lines[8:] == ["WHO'S INVOLVED:", "- Indiana Fever: Indiana Fever, the WNBA team. [entity-fever]"]
    assert len(out["action"]["n1"]["text"]) <= S.budget_chars


def test_round_memory_cuts_text_at_budget_chars():
    full = round_memory(round_store(), NARRATIVES, RecallSettings(budget_chars=10_000), today=TODAY)
    cut = round_memory(round_store(), NARRATIVES, RecallSettings(budget_chars=60), today=TODAY)
    text = cut["action"]["n1"]["text"]
    assert len(text) == 60
    assert text == full["action"]["n1"]["text"][:59] + "…"
    assert cut["action"]["n1"]["picks"] == full["action"]["n1"]["picks"]


def test_round_memory_shuffled_matches_the_kind_counts_of_on():
    on = round_memory(round_store(), NARRATIVES, S, today=TODAY)
    shuffled_settings = RecallSettings(mode="shuffled")
    shuffled = round_memory(round_store(), NARRATIVES, shuffled_settings, today=TODAY, seed=7)
    on_picks, sh_picks = on["action"]["n1"]["picks"], shuffled["action"]["n1"]["picks"]
    assert Counter(p["kind"] for p in sh_picks) == Counter(p["kind"] for p in on_picks) == Counter(
        {"lesson": 2, "episode": 2, "fact": 1, "entity": 1})
    trusted = {"lesson-number", "lesson-question", "fact-fever-record", "fact-lottery", "ep-win", "ep-loss",
               "entity-fever", "entity-wnba"}
    assert {p["id"] for p in sh_picks} <= trusted
    assert all(p["score"] == 0.0 and p["why"]["shuffled"] is True for p in sh_picks)
    assert shuffled["mode"] == "shuffled"
    again = round_memory(round_store(), NARRATIVES, shuffled_settings, today=TODAY, seed=7)
    assert again == shuffled


# revalidation_queue


def test_revalidation_queue_lists_only_past_due_lessons_and_facts():
    store = stored(
        lesson("lesson-due-today", valid_until="2026-10-15", text="Lead with a number"),
        lesson("lesson-past", valid_until="2026-09-30", text="Ask a question"),
        lesson("lesson-fresh", valid_until="2026-11-01", text="Skip hashtags"),
        lesson("lesson-marked-stale", status="stale", valid_until="2026-12-01", text="Post at noon"),
        lesson("lesson-retired", status="retired", valid_until="2026-09-01", text="Tag the league"),
        fact("fact-due", valid_until="2026-10-01", as_of="2026-09-01"),
        fact("fact-superseded", status="superseded", valid_until="2026-10-01"),
        fact("fact-open", valid_until=""),
        episode("ep-a", "success"),
    )
    assert revalidation_queue(store, TODAY) == [
        {"id": "lesson-due-today", "kind": "lesson", "text": "Lead with a number", "valid_until": "2026-10-15"},
        {"id": "lesson-past", "kind": "lesson", "text": "Ask a question", "valid_until": "2026-09-30"},
        {"id": "lesson-marked-stale", "kind": "lesson", "text": "Post at noon", "valid_until": "2026-12-01"},
        {"id": "fact-due", "kind": "fact", "text": "The Fever are 20-10.", "valid_until": "2026-10-01"},
    ]


# render_lessons

LESSONS_HEADER = ("# Lessons\n\n"
                  "Generated by `python -m harness learn` from lessons that passed a held-out test. "
                  "Don't edit by hand.\n\n")


def test_render_lessons_puts_home_lessons_first_and_drops_stale_ones():
    store = stored(
        lesson("lesson-foreign", 0.9, domain="devtools", scope="general", text="Skip the hashtags"),
        lesson("lesson-home-a", 0.4, domain="brand", text="Lead with a concrete number", last_test=HELDOUT,
               valid_until="2026-11-14"),
        lesson("lesson-stale", 0.95, valid_until="2026-10-01", text="Post at noon"),
        lesson("lesson-foreign-domain", 0.95, domain="devtools", scope="domain", text="Mention the changelog"),
        lesson("lesson-home-b", 0.7, domain="", text="Ask one question at the end", polarity="do",
               valid_until="2026-11-30"),
    )
    assert render_lessons(store, TODAY, S) == LESSONS_HEADER + (
        "- Ask one question at the end (not tested on held-out posts; learned from brand; retest by 2026-11-30; "
        "lesson-home-b)\n"
        "- Lead with a concrete number (trait on 31 of 40 held-out failures vs 9 of 35 successes from the same "
        "accounts; learned from brand; retest by 2026-11-14; lesson-home-a)\n"
        "- Skip the hashtags (not tested on held-out posts; learned from devtools; retest by 2026-12-01; "
        "lesson-foreign)\n"
    )
    assert render_lessons(store, TODAY, S, limit=1).count("\n- ") == 1


def test_render_lessons_with_none_active():
    store = stored(lesson("lesson-stale", valid_until="2026-10-01"))
    assert render_lessons(store, TODAY, S) == LESSONS_HEADER + "No lesson has passed a within-account held-out test yet.\n"


def test_render_lessons_marks_category_only_lessons_as_confounded():
    store = stored(lesson("lesson-cat", 0.4, labels_passed=("category",), text="Attach a video"),
                   lesson("lesson-cat-stale", 0.9, labels_passed=("category",), valid_until="2026-10-01"))
    assert render_lessons(store, TODAY, S) == LESSONS_HEADER + (
        "No lesson has passed a within-account held-out test yet.\n"
        "\n"
        "1 lesson passed only on the dataset's Category label, which mostly tracks account size. Treat each as unproven:\n"
        "\n"
        "- Attach a video (not tested on held-out posts; learned from brand; retest by 2026-12-01; lesson-cat)\n"
    )


def test_trust_discounts_category_only_lessons():
    within = mem(lesson("lesson-a", 0.6, labels_passed=("author_prior",)))
    category = mem(lesson("lesson-b", 0.6, labels_passed=("category",)))
    unknown = mem(lesson("lesson-c", 0.6, labels_passed=()))
    assert trust(within, TODAY, S) == 0.6
    assert trust(category, TODAY, S) == 0.6 * S.confounded_label_factor
    assert trust(unknown, TODAY, S) == 0.6 * S.confounded_label_factor


# settings


def test_from_dict_overrides_only_the_given_keys():
    s = MemorySettings.from_dict({"recall": {"mode": "shuffled", "budget_chars": 900}, "reflect": {"n_batches": 3}})
    assert (s.recall.mode, s.recall.budget_chars, s.recall.search_k) == ("shuffled", 900, 40)
    assert s.reflect == replace(ReflectSettings(), n_batches=3)
    assert s.ingest == MemorySettings().ingest


def test_from_dict_of_nothing_is_the_defaults():
    assert MemorySettings.from_dict({}) == MemorySettings()


def test_from_dict_raises_on_an_unknown_section():
    with pytest.raises(KeyError, match="unknown memory settings section 'recal'"):
        MemorySettings.from_dict({"recal": {"mode": "on"}})


def test_from_dict_raises_on_an_unknown_key():
    with pytest.raises(KeyError, match="unknown memory setting recall.budget"):
        MemorySettings.from_dict({"recall": {"budget": 100}})


def test_from_dict_raises_on_a_bad_recall_mode():
    with pytest.raises(ValueError, match="recall.mode must be on, off or shuffled, got 'random'"):
        MemorySettings.from_dict({"recall": {"mode": "random"}})


def test_load_settings_reads_a_json_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"recall": {"mode": "off", "kind_caps": {"lesson": 1}}, "ingest": {"brand_max_pages": 2}}),
                    encoding="utf-8")
    s = load_settings(path)
    assert (s.recall.mode, s.recall.kind_caps, s.ingest.brand_max_pages) == ("off", {"lesson": 1}, 2)
    assert s.reflect == ReflectSettings()


def test_load_settings_without_a_file_is_the_defaults(tmp_path):
    assert load_settings(tmp_path / "missing.json") == MemorySettings()


def test_load_settings_raises_on_a_bad_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"recall": {"mode": "sometimes"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="recall.mode"):
        load_settings(path)
