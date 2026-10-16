import pytest

from harness.llm import LLM, parse_json


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
