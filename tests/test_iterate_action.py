"""Tests for the action lineup instrument's single-answer pieces."""

import pytest

from harness.iterate.fitness import action
from harness.iterate.llm import Meter, MeteredLLM, MockMeteredLLM


def test_pipeline_mode_dispatches_on_type_not_attributes():
    meter = Meter(10)
    assert action.pipeline_mode(MockMeteredLLM(meter, "arm", lambda s, p: {})) == "mock"
    real = object.__new__(MeteredLLM)  # skip __init__: no client, no network
    real.meter, real.arm = meter, "arm"
    assert action.pipeline_mode(real) == "metered"

    class Lookalike:  # has a meter and an arm, but is neither type
        pass

    fake = Lookalike()
    fake.meter, fake.arm = meter, "arm"
    with pytest.raises(TypeError):
        action.pipeline_mode(fake)


def test_lineup_is_seeded_and_holds_the_candidate_once():
    thread = {"id": "t1", "pool": [{"text": f"real {i}"} for i in range(6)]}
    shown, pos = action.lineup(thread, "candidate", seed=0)
    assert len(shown) == 5 and shown.count("candidate") == 1 and shown[pos - 1] == "candidate"
    assert action.lineup(thread, "candidate", seed=0) == (shown, pos)
    assert len({tuple(action.lineup(thread, "candidate", seed=s)[0]) for s in range(20)}) > 1


def test_tidy_draft_strips_quotes_mentions_links_and_extra_lines():
    assert action.tidy_draft('"@fan1 @fan2 what a shot https://t.co/abc"\nsecond line') == "what a shot"
    assert action.tidy_draft("") == ""


def test_mock_detector_finds_the_mock_reply_at_its_method_rate():
    hits = 0
    for i in range(200):
        prompt = "REPLIES:\n1. real a\n2. mock continuation reply\n3. real b\n4. real c\n5. real d\n" + f"#{i}"
        hits += action.mock_detector(action.LINEUP_SYSTEM, prompt)["ai_reply"] == 2
    assert 50 < hits < 95  # 0.35 rate over 200 draws
