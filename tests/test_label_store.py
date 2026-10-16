"""harness.label.store: the label-store contract the labeling UI is built against, on tiny fixtures."""

import json

import pytest

from harness.label.store import Store

SCHEMA = {
    "version": 7,
    "fields": [
        {"name": "format", "kind": "enum", "values": ["joke", "tip", "other"], "question": "Format?",
         "applies_to": ["post", "reply"]},
        {"name": "specificity", "kind": "ordinal", "values": [1, 2, 3], "question": "Concrete?",
         "applies_to": ["post", "reply"]},
        {"name": "adds_to_parent", "kind": "ordinal", "values": [1, 2, 3], "question": "Adds?",
         "applies_to": ["reply"]},
        {"name": "note_topic", "kind": "text", "values": [], "question": "Topic?", "applies_to": ["post"]},
    ],
}


def item(n, kind="post", parent=None):
    return {"item_id": f"x:{n}", "kind": kind, "status_id": str(n), "url": f"https://x.com/a/status/{n}",
            "author": "a", "author_followers": 10, "text": f"text {n}", "created_at": "2026-10-01T00:00:00+00:00",
            "parent_id": parent, "metrics": {"likes": n}, "source": "author_timelines"}


def answer(item_id, model, labels, at, error=None, run="run"):
    return {"item_id": item_id, "run_id": f"{run}-{model}",
            "annotator": {"model": model, "response_model": model, "prompt_sha": "abc"},
            "labels": labels, "rationale": "r", "usage": {}, "at": at, "error": error}


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    (tmp_path / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    write_jsonl(tmp_path / "items.jsonl", [item(1), item(2, "reply", "x:1"), item(3), item(4)])
    (tmp_path / "llm").mkdir()
    write_jsonl(tmp_path / "llm" / "run-claude.jsonl", [
        answer("x:1", "claude", {"format": "joke", "specificity": 2}, "2026-10-16T01:00:00+00:00"),
        answer("x:2", "claude", {"format": "tip", "specificity": 1, "adds_to_parent": 3}, "2026-10-16T01:00:00+00:00"),
        answer("x:3", "claude", {"format": "tip", "specificity": 3}, "2026-10-16T01:00:00+00:00"),
        # A later answer from the same model replaces its earlier vote; an error row never votes.
        answer("x:3", "claude", {"format": "other", "specificity": 3}, "2026-10-16T02:00:00+00:00"),
        answer("x:3", "claude", {}, "2026-10-16T03:00:00+00:00", error="http_503"),
    ])
    write_jsonl(tmp_path / "llm" / "run-gpt.jsonl", [
        answer("x:1", "gpt", {"format": "joke", "specificity": 2}, "2026-10-16T01:30:00+00:00"),
        answer("x:2", "gpt", {"format": "joke", "specificity": 2, "adds_to_parent": 1}, "2026-10-16T01:30:00+00:00"),
        answer("x:3", "gpt", {"format": "other", "specificity": 1}, "2026-10-16T01:30:00+00:00"),
    ])
    return Store(tmp_path)


def test_schema_is_read_from_the_file_and_kinds_are_checked(store, tmp_path):
    assert store.load_schema()["version"] == 7
    store.load_schema()["version"] = 99  # callers get a copy
    assert store.load_schema()["version"] == 7
    bad = dict(SCHEMA, fields=[dict(SCHEMA["fields"][0], kind="slider")])
    (tmp_path / "schema.json").write_text(json.dumps(bad) + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kind"):
        store.load_schema()


def test_items_in_file_order_or_requested_order(store):
    assert [i["item_id"] for i in store.items()] == ["x:1", "x:2", "x:3", "x:4"]
    assert [i["item_id"] for i in store.items(["x:3", "x:1"])] == ["x:3", "x:1"]
    with pytest.raises(KeyError, match="x:9"):
        store.items(["x:9"])


def test_llm_labels_returns_every_answer_oldest_first(store):
    rows = store.llm_labels("x:3")
    assert [r["at"][11:13] for r in rows] == ["01", "01", "02", "03"]
    assert rows[-1]["error"] == "http_503"
    assert store.llm_labels("x:4") == []
    with pytest.raises(KeyError):
        store.llm_labels("x:9")


def test_consensus_agreement_ties_and_latest_vote(store):
    c1 = store.consensus("x:1")
    assert set(c1) == {"format", "specificity", "note_topic"}  # adds_to_parent applies to replies only
    assert c1["format"] == {"value": "joke", "agreement": 1.0, "votes": {"claude": "joke", "gpt": "joke"}, "human": None}
    assert c1["note_topic"] == {"value": None, "agreement": None, "votes": {}, "human": None}
    c3 = store.consensus("x:3")
    assert c3["format"]["votes"] == {"claude": "other", "gpt": "other"}  # claude's later answer, not its error
    assert c3["specificity"] == {"value": None, "agreement": 0.5, "votes": {"claude": 3, "gpt": 1}, "human": None}
    assert store.consensus("x:2")["adds_to_parent"]["votes"] == {"claude": 3, "gpt": 1}


def test_three_votes_take_the_plurality(store, tmp_path):
    write_jsonl(tmp_path / "llm" / "run-haiku.jsonl",
                [answer("x:3", "haiku", {"format": "tip", "specificity": 3}, "2026-10-16T04:00:00+00:00")])
    c = Store(tmp_path).consensus("x:3")
    assert c["specificity"]["value"] == 3 and c["specificity"]["agreement"] == pytest.approx(2 / 3)
    assert c["format"]["value"] == "other"


def test_human_label_is_reported_beside_the_llm_value_never_over_it(store, tmp_path):
    store.append_human("x:3", "sam", {"specificity": 2}, "close call")
    c = store.consensus("x:3")["specificity"]
    assert c["value"] is None and c["votes"] == {"claude": 3, "gpt": 1}
    assert c["human"]["value"] == 2 and c["human"]["labeler"] == "sam"
    store.append_human("x:3", "dev", {"specificity": 3, "format": "tip"})
    c = store.consensus("x:3")
    assert c["specificity"]["human"]["value"] == 3 and c["specificity"]["human"]["labeler"] == "dev"
    assert c["format"]["value"] == "other" and c["format"]["human"]["value"] == "tip"
    # LLM files are untouched by human labels.
    assert len(store.llm_labels("x:3")) == 4


def test_append_human_is_append_only(store, tmp_path):
    first = store.append_human("x:1", "sam", {"format": "tip"})
    store.append_human("x:1", "sam", {"format": "joke"})
    lines = (tmp_path / "human.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0]) == first
    assert set(first) == {"item_id", "labeler", "labels", "note", "at"}


@pytest.mark.parametrize("labels, message", [
    ({"format": "rant"}, "not one of"),
    ({"specificity": 4}, "not one of"),
    ({"specificity": True}, "not one of"),  # True == 1 in Python; a checkbox must not pass as 1
    ({"adds_to_parent": 2}, "does not apply"),  # x:1 is a post
    ({"mood": "x"}, "does not apply"),
    ({"note_topic": 3}, "takes text"),
])
def test_append_human_rejects_bad_labels(store, labels, message):
    with pytest.raises(ValueError, match=message):
        store.append_human("x:1", "sam", labels)


def test_append_human_rejects_unknown_item_and_blank_labeler(store, tmp_path):
    with pytest.raises(KeyError):
        store.append_human("x:9", "sam", {"format": "tip"})
    with pytest.raises(ValueError, match="labeler"):
        store.append_human("x:1", "  ", {"format": "tip"})
    assert not (tmp_path / "human.jsonl").exists()
    row = store.append_human("x:1", "sam", {"note_topic": "launch"})
    assert row["labels"] == {"note_topic": "launch"}


def test_queue_disagree_orders_by_disagreeing_fields_and_drops_settled_ones(store):
    q = store.queue("disagree")
    assert [(e["item_id"], e["n_disagree"]) for e in q] == [("x:2", 3), ("x:3", 1)]
    assert q[0]["disagree_fields"] == ["format", "specificity", "adds_to_parent"]
    assert q[0]["item"]["item_id"] == "x:2"
    store.append_human("x:3", "sam", {"specificity": 2})  # settles x:3's only disagreement
    assert [e["item_id"] for e in store.queue("disagree")] == ["x:2"]
    store.append_human("x:2", "sam", {"format": "tip"})  # x:2 still has two open fields
    assert [e["item_id"] for e in store.queue("disagree")] == ["x:2"]


def test_queue_unlabeled_all_and_paging(store):
    assert [e["item_id"] for e in store.queue("all")] == ["x:2", "x:3", "x:1", "x:4"]
    assert [e["item_id"] for e in store.queue("all", limit=2, offset=1)] == ["x:3", "x:1"]
    store.append_human("x:1", "sam", {"format": "joke"})
    assert [e["item_id"] for e in store.queue("unlabeled")] == ["x:2", "x:3", "x:4"]
    entry = store.queue("all", limit=4)[-1]
    assert entry["item_id"] == "x:4" and not entry["llm_labeled"] and not entry["human_labeled"]
    with pytest.raises(ValueError, match="unknown queue mode"):
        store.queue("random")
    with pytest.raises(ValueError):
        store.queue("all", limit=-1)


def test_audit_set_is_frozen_stratified_and_shrinks_as_people_label(store, tmp_path):
    with pytest.raises(FileNotFoundError, match="audit"):
        store.queue("audit")
    write_jsonl(tmp_path / "llm" / "run-solo.jsonl", [answer("x:4", "solo", {"format": "tip"}, "2026-10-16T05:00:00+00:00")])
    store = Store(tmp_path)
    audit = store.build_audit(size=3)
    assert audit["size"] == 3 and sorted(audit["items"]) == ["x:1", "x:2", "x:3"]  # x:4 has one model's vote
    assert audit["strata"] == {"post/0": 1, "post/1-2": 1, "reply/3+": 1}
    assert store.build_audit(size=3)["items"] == audit["items"]  # same seed, same set
    assert [e["item_id"] for e in store.queue("audit")] == audit["items"]
    store.append_human(audit["items"][0], "sam", {"format": "tip"})
    assert [e["item_id"] for e in store.queue("audit")] == audit["items"][1:]
