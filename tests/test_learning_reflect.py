import random

import pytest

from harness.learning.kinds import Episode, Lesson, Outcome, load, save
from harness.learning.reflect import (
    Batch, MissingBoostFlags, apply_rule, by_account_and_launch, caveats_for, due_for_retest, extra_features,
    link_evidence, parse_labels, reflect, rule_error, split_heldout, tables_for, usable, vote,
)
from harness.learning.settings import ReflectSettings
from harness.learning.store import FakeStore
from harness.llm import LLM


def ep(eid, label, author="acme", text="hello world", media="none", **features):
    return Episode(id=eid, source="dataset", author_username=author, text=text, created_at="2026-10-01T00:00:00+00:00",
                   features={"media": media, "is_launch": False, **features},
                   outcomes=[Outcome(label=label, source="dataset", raw=label)])


def batch():
    b = Batch(group="contrast:acme|other")
    b.failures["acme|other"] = [ep("post-1", "failure"), ep("post-2", "failure")]
    b.successes["acme|other"] = [ep("post-3", "success")]
    return b


def test_link_evidence_keeps_only_matching_citations():
    h = {"outcome": "failure", "evidence": ["post-1", "post-2", "post-3", "post-9", "post-1"], "contrast": ["post-3", "post-2"]}
    assert link_evidence(h, batch()) == (["post-1", "post-2"], ["post-3"], ["post-3", "post-9", "post-2"])


@pytest.mark.parametrize("h", [
    {"outcome": "failure", "evidence": ["post-1"], "contrast": ["post-3"]},  # one evidence id
    {"outcome": "failure", "evidence": ["post-1", "post-2"], "contrast": ["post-1"]},  # no valid contrast
    {"outcome": "maybe", "evidence": ["post-1", "post-2"], "contrast": ["post-3"]},
])
def test_link_evidence_rejects_thin_hypotheses(h):
    assert link_evidence(h, batch()) is None


@pytest.mark.parametrize("rule,error", [
    ({"feature": "media", "op": "eq", "value": "none"}, None),
    ({"feature": "media", "op": "in", "value": ["photo", "video"]}, None),
    ({"feature": "n_chars", "op": "lt", "value": 60}, None),
    ({"feature": "n_chars", "op": "lt", "value": True}, "lt/gt need a number"),
    ({"feature": "has_link", "op": "eq", "value": "yes"}, "has_link eq needs true or false"),
    ({"feature": "text", "op": "contains_any", "value": []}, "contains_any needs a non-empty list of strings"),
    ({"feature": "mood", "op": "eq", "value": 1}, "unknown feature 'mood'"),
    ({"feature": "media", "op": "lt", "value": 1}, "op 'lt' not allowed for media"),
    ("media none", "rule is not an object"),
])
def test_rule_error(rule, error):
    assert rule_error(rule) == error


def test_rule_error_accepts_typed_context_features():
    extra = {"pre_has_number": "bool", "pre_n_words": "number"}
    assert rule_error({"feature": "pre_has_number", "op": "eq", "value": True}, extra) is None
    assert rule_error({"feature": "pre_has_number", "op": "gt", "value": 1}, extra) is not None
    assert rule_error({"feature": "pre_n_words", "op": "gt", "value": 20}, extra) is None


def test_apply_rule_text_matches_whole_words_case_insensitively():
    rule = {"feature": "text", "op": "contains_any", "value": ["Ticket", "early bird"]}
    assert apply_rule(rule, ep("a", "failure", text="Get your tickets")) is False  # "tickets" is not "ticket"
    assert apply_rule(rule, ep("a", "failure", text="One TICKET left")) is True
    assert apply_rule(rule, ep("a", "failure", text="Early bird ends Friday")) is True
    none_rule = {**rule, "op": "contains_none"}
    assert apply_rule(none_rule, ep("a", "failure", text="One TICKET left")) is False
    assert apply_rule({"feature": "n_chars", "op": "gt", "value": 3}, ep("a", "failure", n_chars=5)) is True
    assert apply_rule({"feature": "n_chars", "op": "gt", "value": 3}, ep("a", "failure")) is False


def test_usable_filters_and_counts():
    eps = [ep("a", "failure", boost_suspect=False), ep("b", "", boost_suspect=False),
           ep("c", "failure", thread_continuation=True), ep("d", "failure", label_age_hours=10),
           ep("e", "success", boost_suspect=True), ep("f", "failure", boost_suspect=True),
           ep("g", "success", label_eligible=False)]
    kept, dropped = usable(eps)
    assert [e.id for e in kept] == ["a", "f"]  # a boosted failure is still a failure
    assert dropped == {"unlabeled": 1, "thread_continuation": 1, "label_too_young": 1, "not_label_eligible": 1,
                       "boosted_success": 1, "boost_filter": "on"}


def test_usable_refuses_without_boost_flags_unless_allowed():
    with pytest.raises(MissingBoostFlags):
        usable([ep("a", "failure")])
    kept, dropped = usable([ep("a", "failure")], require_boost_flags=False)
    assert [e.id for e in kept] == ["a"] and dropped["boost_filter"] == "off"


def test_split_heldout_halves_eligible_strata_only():
    eps = ([ep(f"f{i}", "failure") for i in range(5)] + [ep(f"s{i}", "success") for i in range(3)]
           + [ep("x1", "failure", author="tiny"), ep("x2", "success", author="tiny")])
    proposal, held = split_heldout(eps, by_account_and_launch, random.Random(0))
    assert sorted(e.id for e in held if e.label() == "failure").__len__() == 2  # 5 // 2
    assert len([e for e in held if e.label() == "success"]) == 1  # 3 // 2
    assert {e.author_username for e in held} == {"acme"}  # "tiny" has 1 of each, under the minimum of 2
    assert {e.id for e in proposal} | {e.id for e in held} == {e.id for e in eps}
    assert not {e.id for e in proposal} & {e.id for e in held}


def test_parse_labels_drops_wrong_lengths():
    data = {"labels": {"post-1": "TF", "post-2": "T", "post-3": "tf", "post-4": "TX", "post-5": ["T", "F"]}}
    assert parse_labels(data, ["q0", "q1"]) == {"post-1": {"q0": True, "q1": False},
                                                "post-3": {"q0": True, "q1": False}}


def test_tables_for_counts_per_stratum():
    held = [ep("a", "failure"), ep("b", "failure"), ep("c", "success"), ep("d", "success", author="other")]
    tables = tables_for(held, by_account_and_launch, {"a": True, "b": False, "c": True, "d": False}, "failure")
    assert [(t.stratum, t.true_target, t.true_other, t.false_target, t.false_other) for t in tables] == [
        ("acme|other", 1, 1, 1, 0), ("other|other", 0, 0, 0, 1)]


def lesson(**kw):
    base = dict(id="lesson-x", text="Attach a video.", predicate="Has a video?", confidence=0.3, evidence=["post-1"],
                reflections=[], valid_until="2026-11-01", domain="devtools", domains=["devtools"], polarity="do",
                support=1, labeled_at_test=100, created="2026-10-01", updated="2026-10-01")
    return Lesson(**{**base, **kw})


RESULT = {"effect": 0.4, "p_value": 0.001, "target_posts": 10, "target_with_trait": 5, "other_posts": 10,
          "other_with_trait": 1}


def test_vote_supported_renews_and_promotes_on_a_second_domain():
    l = vote(lesson(), "supported", RESULT, "refl-a", "2026-10-15", "brand", 120, valid_days=30)
    assert (l.support, l.confidence, l.valid_until, l.domains, l.scope) == (2, 0.4, "2026-11-14", ["devtools", "brand"], "general")
    assert l.reflections == ["refl-a"] and l.labeled_at_test == 120


def test_vote_inconclusive_changes_nothing_but_the_count():
    l = vote(lesson(), "inconclusive", RESULT, "refl-a", "2026-10-15", "devtools", 120)
    assert (l.support, l.contradict, l.inconclusive, l.status, l.confidence) == (1, 0, 1, "active", 0.3)


def test_vote_contradicted_retires_once_contradictions_outnumber_support():
    l = vote(lesson(), "contradicted", RESULT, "r1", "2026-10-15", "devtools", 120)
    assert (l.contradict, l.status) == (1, "active")
    l = vote(l, "contradicted", RESULT, "r2", "2026-10-16", "devtools", 120)
    assert (l.contradict, l.status) == (2, "retired")


def test_due_for_retest_on_new_episodes_or_date():
    assert due_for_retest(lesson(), 149, "2026-10-15") is False
    assert due_for_retest(lesson(), 150, "2026-10-15") is True
    assert due_for_retest(lesson(), 100, "2026-11-01") is True
    assert due_for_retest(lesson(status="retired"), 1000, "2027-01-01") is False


def test_caveats():
    assert caveats_for({"outcome": "success", "claim": "Announcing a new product wins"}, {"boost_filter": "on"}) == [
        "likely launch-confounded: it describes announcements, and launches get paid and bot boosts that the launch "
        "keyword rule doesn't fully catch"]
    assert caveats_for({"outcome": "failure", "claim": "Announcing flops"}, {"boost_filter": "off"}) == [
        "boost flags were unavailable, so boosted successes were not excluded"]


def test_extra_features_only_types_present_everywhere():
    eps = [ep("a", "failure", pre_has_number=True, pre_n_words=3, pre_mixed=1),
           ep("b", "success", pre_has_number=False, pre_n_words=7.5, pre_mixed=True)]
    assert extra_features(eps) == {"pre_has_number": "bool", "pre_n_words": "number"}


class ScriptedLLM(LLM):
    """Returns queued JSON replies in order and records the prompts it saw."""

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete_json(self, system, prompt, mock=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def dataset(n_accounts=6):
    eps = []
    for a in range(n_accounts):
        for i in range(4):
            eps.append(ep(f"post-{a}f{i}", "failure", author=f"acct{a}", media="none", boost_suspect=False))
            eps.append(ep(f"post-{a}s{i}", "success", author=f"acct{a}", media="video", boost_suspect=False))
    return eps


def test_reflect_tests_rules_by_code_and_votes_with_the_lessons_own_predicate():
    store = FakeStore()
    old = lesson(id="lesson-video", predicate="Has a video?", rule={"feature": "media", "op": "eq", "value": "video"},
                 polarity="do", support=1)
    wrong_polarity = lesson(id="lesson-avoid", polarity="avoid", rule={"feature": "media", "op": "eq", "value": "none"})
    save(store, old)
    save(store, wrong_polarity)
    settings = ReflectSettings(n_batches=1, groups_per_batch=6, per_side=2)
    eps = dataset()

    def proposals():
        return {"no_shared_trait": "", "hypotheses": [
            {"outcome": "success", "claim": "Video wins", "predicate": "Does it show motion?", "rule": None,
             "lesson": "Use video", "evidence": [], "contrast": [], "restates": "lesson-video"},
            {"outcome": "success", "claim": "Restating the wrong way", "predicate": "Is it watchable?",
             "rule": {"feature": "media", "op": "eq", "value": "video"}, "lesson": "x", "evidence": [],
             "contrast": [], "restates": "lesson-avoid"},
        ]}

    class Proposer(ScriptedLLM):
        """Cites the batch's own post ids, read back from the prompt, as evidence and contrast."""

        def complete_json(self, system, prompt, mock=None):
            self.prompts.append(prompt)
            fails = [l.split("]")[0].split("[")[1] for l in prompt.splitlines() if l.startswith("- [post-") and "f" in l.split("]")[0][-3:]]
            wins = [l.split("]")[0].split("[")[1] for l in prompt.splitlines() if l.startswith("- [post-") and "s" in l.split("]")[0][-3:]]
            data = proposals()
            for h in data["hypotheses"]:
                h["evidence"], h["contrast"] = wins[:2], fails[:1]
            return data

    class Labeler(ScriptedLLM):
        def complete_json(self, system, prompt, mock=None):
            ids = [l.split("]")[0].split("[")[1] for l in prompt.splitlines() if l.startswith("- [post-")]
            n = sum(1 for l in prompt.splitlines() if l[:1].isdigit() and ". " in l)
            return {"labels": {i: ("T" if "s" in i[-2:] else "F") * n for i in ids}}

    report = reflect(eps, Proposer([]), store, definition="d", domain="devtools", settings=settings,
                     labeler=Labeler([]), seed=0)
    assert report.linked == 2
    # The first hypothesis votes on lesson-video; the second can't vote on an "avoid" lesson, so it stands alone.
    assert report.lessons_voted == ["lesson-video"]
    voted = load(store.items["lesson-video"])
    assert voted.support == 2 and voted.last_test["checked_by"] == "rule"
    assert load(store.items["lesson-avoid"]).support == 1
    reflections = [load(m) for m in store.list_memories("reflection")]
    assert sorted(r.restates for r in reflections) == ["", "lesson-video"]
    # Cited dataset posts were written as episode notes before any lesson cited them.
    assert all(eid in store.items for r in reflections for eid in r.evidence + r.contrast)


def test_a_supported_retest_with_boost_flags_drops_the_boost_caveat():
    l = lesson(caveats=["boost flags were unavailable, so boosted successes were not excluded", "likely launch-confounded"])
    l = vote(l, "supported", {**RESULT, "boost_filter": "off"}, "r1", "2026-10-15", "devtools", 120)
    assert len(l.caveats) == 2
    l = vote(l, "supported", {**RESULT, "boost_filter": "on"}, "r2", "2026-10-15", "devtools", 120)
    assert l.caveats == ["likely launch-confounded"]
