"""Orient's BAML path on malformed replies, parsed by the real OrientSignals parser.

Each test scripts the model's raw replies. `baml_adapter.call` is replaced by one that parses the
reply with `OrientSignals@spec(...).parse`, the same parser a live call uses, so these cover what
reaches build_narratives after BAML's parser and orient's checks.
"""

import json
from datetime import datetime, timezone

import pytest

from harness import baml_adapter
from harness import orient as o
from harness.models import Signal

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
SIGNALS = [
    Signal(source="news", text=f"Story {i}", url=f"https://example.com/{i}", created_at="2026-10-15T17:00:00Z")
    for i in range(3)
]


class Gateway:
    """A non-mock harness LLM. Nothing reaches the network: the adapter call is scripted."""

    provider, model = "anthropic", "fixture-model"


def narrative(id_, relevance=0.8, ids=(0,), spaced=True, drop=()):
    fields = {"id": id_, "title": f"T {id_}", "summary": "S.", "relevance": relevance, "velocity": 0.5,
              "angle": "A", "signal_ids": list(ids)}
    for key in drop:
        del fields[key]
    return json.dumps(fields) if spaced else json.dumps(fields, separators=(",", ":"))


def reply(*items, spaced=True):
    sep = ", " if spaced else ","
    return ('{"narratives": [' if spaced else '{"narratives":[') + sep.join(items) + "]}"


@pytest.fixture
def scripted(monkeypatch, tmp_path):
    """Queue raw replies; each orient call to the model parses the next one."""
    queue: list[str] = []
    calls: list[str] = []

    def call(llm, function, *args, mock, on_event=None):
        raw = queue.pop(0)
        calls.append(raw)
        value = baml_adapter.spec(llm, function, *args).parse(raw)
        return baml_adapter.BamlResult(value, raw=raw)

    monkeypatch.setattr(o.baml_adapter, "call", call)
    monkeypatch.setenv("HARNESS_ORIENT", "baml")
    monkeypatch.setattr(o, "REPLY_LOG_DIR", tmp_path / "orient-replies")
    return queue, calls


def run():
    return o.orient(Gateway(), "mission", SIGNALS, top=5, now=NOW)


def test_two_json_objects_keep_the_first(scripted):
    queue, _ = scripted
    queue.append(reply(narrative("first")) + "\n" + reply(narrative("second")))
    assert [n.id for n in run()] == ["first"]


def test_prose_around_the_json(scripted):
    queue, _ = scripted
    queue.append("Here are the narratives:\n```json\n" + reply(narrative("a", ids=(0, 1))) + "\n```\nDone.")
    [n] = run()
    assert n.id == "a"
    assert n.signal_urls == ["https://example.com/0", "https://example.com/1"]


def test_missing_field_drops_that_narrative_and_logs_it(scripted, capsys):
    queue, _ = scripted
    raw = reply(narrative("a"), narrative("b", drop=("relevance",)))
    queue.append(raw)
    assert [n.id for n in run()] == ["a"]
    out = capsys.readouterr().out
    assert "reply had 2 narratives; the parser kept 1" in out
    [saved] = o.REPLY_LOG_DIR.iterdir()
    assert saved.read_text(encoding="utf-8") == raw
    assert f"raw reply saved to {saved}" in out


def test_clean_reply_is_not_saved(scripted):
    queue, _ = scripted
    queue.append(reply(narrative("a"), narrative("b")))
    run()
    assert not o.REPLY_LOG_DIR.exists()


def test_out_of_range_relevance_is_dropped(scripted, capsys):
    queue, _ = scripted
    queue.append(reply(narrative("a", relevance=1.7), narrative("b", relevance=0.6)))
    assert [n.id for n in run()] == ["b"]
    assert "dropped a: {'relevance': 1.7} outside 0..1" in capsys.readouterr().out


def test_truncated_spaced_array_keeps_complete_narratives(scripted, capsys):
    queue, _ = scripted
    queue.append(reply(narrative("a"), narrative("b"))[:-60])
    assert [n.id for n in run()] == ["a"]
    out = capsys.readouterr().out
    assert "reply had 2 narratives; the parser kept 1" in out
    assert "reply was cut off before its JSON closed" in out


def test_truncated_compact_array_is_asked_again(scripted, capsys):
    queue, calls = scripted
    queue.append(reply(narrative("a", spaced=False), narrative("b", spaced=False), spaced=False)[:-40])
    queue.append(reply(narrative("c")))
    assert [n.id for n in run()] == ["c"]
    assert len(calls) == 2
    out = capsys.readouterr().out
    assert "reply had 2 narratives; the parser kept 0" in out
    assert "no narrative in the reply parsed; asking once more" in out


def test_twice_unparseable_raises(scripted):
    queue, _ = scripted
    truncated = reply(narrative("a", spaced=False), spaced=False)[:-2]
    queue.extend([truncated, truncated])
    with pytest.raises(o.OrientReplyError):
        run()


def test_an_empty_list_is_an_answer(scripted):
    queue, calls = scripted
    queue.append('{"narratives": []}')
    assert run() == []
    assert len(calls) == 1


def test_unrecorded_reply_fails(monkeypatch):
    monkeypatch.setenv("HARNESS_ORIENT", "baml")
    monkeypatch.setattr(o.baml_adapter, "call", lambda *a, **k: baml_adapter.BamlResult(object(), raw=None))
    with pytest.raises(o.OrientReplyError, match="no reply"):
        run()


def test_json_backend_uses_complete_json(monkeypatch):
    class JsonLLM(Gateway):
        def complete_json(self, system, prompt, mock=None):
            assert system == o.SYSTEM and "MISSION:\nmission" in prompt
            return {"narratives": [json.loads(narrative("j"))]}

    monkeypatch.setenv("HARNESS_ORIENT", "json")
    monkeypatch.setattr(o.baml_adapter, "call", lambda *a, **k: pytest.fail("BAML called on the json backend"))
    assert [n.id for n in o.orient(JsonLLM(), "mission", SIGNALS, top=5, now=NOW)] == ["j"]


def test_unknown_backend_fails(monkeypatch):
    monkeypatch.setenv("HARNESS_ORIENT", "yaml")
    with pytest.raises(ValueError, match="HARNESS_ORIENT must be one of"):
        run()
