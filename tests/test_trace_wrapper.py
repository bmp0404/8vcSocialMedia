from harness.llm import AnthropicLLM, MockLLM
from harness.trace import Trace, TracingLLM, traced


def test_traced_mock_is_still_a_mock_and_records_calls():
    trace = Trace()
    llm = traced(MockLLM(), trace)
    assert isinstance(llm, MockLLM)  # lanes.run detects mock runs this way
    assert isinstance(llm, TracingLLM)
    assert llm.complete_json("SYS", "prompt", mock=lambda: {"ok": 1}) == {"ok": 1}
    assert len(trace.calls) == 1


def test_traced_anthropic_keeps_class_and_attributes():
    inner = AnthropicLLM.__new__(AnthropicLLM)  # no client needed to check routing
    inner.model, inner.client = "claude-test", object()
    llm = traced(inner, Trace())
    assert isinstance(llm, AnthropicLLM)  # writer and veto route models on this check
    assert llm.model == "claude-test"
    assert llm.client is inner.client
    assert type(traced(inner, Trace())) is type(llm)  # one wrapper class per inner class
