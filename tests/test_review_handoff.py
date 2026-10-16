"""A [VERIFY: ...] draft reaches review flagged, and never reaches the hand-off unresolved."""

import json

import pytest

from harness import config, memory
from harness.llm import MockLLM
from harness.minds import lanes
from harness.models import Action
from harness.review import check_approval, export_approved

MARKED = "clark had [VERIFY: 12 assists] in the opener"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    return tmp_path


def save(text: str) -> int:
    return memory.save_run([Action(type="post", text=text, mind="m", narrative_id="n", id="a1")])


def test_review_refuses_to_approve_an_unresolved_marker(isolated):
    save(MARKED)
    with pytest.raises(ValueError, match=r"resolve \[VERIFY: 12 assists\] before approving"):
        check_approval("a1", "approved", None)
    check_approval("a1", "approved", "clark had 9 assists in the opener")  # a person checked and wrote it in
    check_approval("a1", "killed", None)
    with pytest.raises(KeyError):
        check_approval("nope", "approved", None)


def test_hand_off_refuses_an_unresolved_approval_that_got_past_review(isolated):
    run_id = memory.save_run([Action(type="post", text=MARKED, mind="m", narrative_id="n", id="a1"),
                              Action(type="post", text="a clean draft", mind="m", narrative_id="n", id="a2")])
    memory.set_review("a1", "approved")  # set_review itself doesn't check, so the export must
    memory.set_review("a2", "approved")
    data = json.loads(export_approved(run_id).read_text(encoding="utf-8"))
    assert [a["id"] for a in data["actions"]] == ["a2"]
    assert data["refused"] == [{"id": "a1", "unresolved": ["[VERIFY: 12 assists]"]}]


def test_lanes_flag_a_shipped_draft_with_a_marker(monkeypatch):
    flagged = Action(type="post", text=MARKED, mind="caption", narrative_id="n")
    monkeypatch.setattr(lanes, "reply_lane", lambda *a, **k: ([], []))
    monkeypatch.setattr(lanes, "post_lane", lambda *a, **k: ([flagged], []))
    actions, _ = lanes.run(MockLLM(), "mission", "", [], [], takes=[])
    assert actions[0].draft_meta["needs_verification"] == ["[VERIFY: 12 assists]"]
