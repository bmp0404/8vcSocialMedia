import pytest

from harness.llm import LLM, AnthropicLLM, MockLLM, OpenAICompatLLM, TruncatedError, parse_json


def test_parse_json_takes_the_first_value_when_more_follows():
    assert parse_json('{"a": 1}\n{"b": 2}') == {"a": 1}
    assert parse_json('Here you go:\n```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert parse_json('note [1, 2] then {"x": 1}') == [1, 2]


def test_parse_json_raises_on_no_json():
    with pytest.raises(ValueError):
        parse_json("no json here")


class Scripted(LLM):
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def test_complete_json_retries_once_then_gives_up():
    llm = Scripted(["not json", '{"ok": true}'])
    assert llm.complete_json("SYS", "p") == {"ok": True}
    assert "wasn't valid JSON" in llm.prompts[1]
    with pytest.raises(ValueError):
        Scripted(["nope", "still nope"]).complete_json("SYS", "p")


def test_only_the_mock_backend_says_it_is_mock():
    assert MockLLM().is_mock is True
    assert getattr(AnthropicLLM.__new__(AnthropicLLM), "is_mock", False) is False


# ---- truncation: a reply cut off at the output cap raises and is never parsed

class FakeAnthropicClient:
    def __init__(self, text, stop_reason):
        self.calls = 0
        block = type("Block", (), {"type": "text", "text": text})()
        self.resp = type("Resp", (), {"content": [block], "stop_reason": stop_reason,
                                      "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})()})()
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return self.resp


def anthropic_with(text, stop_reason):
    llm = AnthropicLLM.__new__(AnthropicLLM)  # no SDK client: the fake stands in for it
    llm.model, llm.client = "claude-test", FakeAnthropicClient(text, stop_reason)
    return llm


def test_anthropic_reply_cut_at_max_tokens_raises_and_is_not_parsed_or_retried():
    llm = anthropic_with('{"narratives": [{"id": "a"}]', "max_tokens")
    with pytest.raises(TruncatedError, match="claude-test stopped at its output cap .*'max_tokens'"):
        llm.complete_json("SYS", "p")
    assert llm.client.calls == 1


def test_anthropic_complete_reply_parses():
    assert anthropic_with('{"ok": 1}', "end_turn").complete_json("SYS", "p") == {"ok": 1}
    assert anthropic_with("done", "stop_sequence").complete("SYS", "p") == "done"


def test_truncation_is_not_a_value_error():
    # complete_json retries on ValueError; a truncation must escape that retry.
    assert not issubclass(TruncatedError, ValueError)


class FakeOpenAIClient:
    def __init__(self, text, finish_reason):
        message = type("M", (), {"content": text})()
        self.resp = type("R", (), {"choices": [type("C", (), {"message": message, "finish_reason": finish_reason})()]})()
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        return self.resp


def test_openai_compatible_reply_cut_at_length_raises():
    llm = OpenAICompatLLM.__new__(OpenAICompatLLM)
    llm.model, llm.provider, llm.client = "gpt-test", "openai", FakeOpenAIClient('{"a": ', "length")
    with pytest.raises(TruncatedError, match="'length'"):
        llm.complete_json("SYS", "p")
    llm.client = FakeOpenAIClient('{"a": 1}', "stop")
    assert llm.complete_json("SYS", "p") == {"a": 1}


def test_iterate_metered_llm_checks_truncation_too():
    from harness.iterate.llm import MeteredLLM

    class Meter:
        def reserve(self, arm): pass
        def record(self, arm, *a, **k): pass

    llm = MeteredLLM.__new__(MeteredLLM)
    llm.model, llm.meter, llm.arm, llm.max_tokens, llm.retries = "claude-test", Meter(), "a", 2000, 0
    llm.client = FakeAnthropicClient('{"a": ', "max_tokens")
    with pytest.raises(TruncatedError, match="2000 tokens"):
        llm.complete_json("SYS", "p")
