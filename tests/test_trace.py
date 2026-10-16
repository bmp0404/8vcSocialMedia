"""harness/trace.py: the traced LLM keeps its class, calls and stages are recorded, and tracing never fails a run."""

import argparse
import json

import pytest

from harness import cli, config, memory
from harness import trace as tr
from harness.llm import LLM, AnthropicLLM, MockLLM
from harness.models import Action
from harness.trace import Trace, TracingLLM, traced


# The two tests from sam/ui's tests/test_trace_wrapper.py, unchanged.

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


def test_wrap_keeps_the_class_and_never_wraps_twice():
    trace = Trace()
    llm = trace.wrap(MockLLM())
    assert isinstance(llm, MockLLM) and isinstance(llm, TracingLLM)
    assert trace.wrap(llm) is llm


class Boom(LLM):
    def complete(self, system, prompt):
        raise RuntimeError("gateway 502")


def test_a_failed_call_is_recorded_and_still_raises():
    trace = Trace()
    with pytest.raises(RuntimeError, match="gateway 502"):
        traced(Boom(), trace).complete("SYSTEM LINE\nmore", "the prompt")
    (call,) = trace.calls
    assert (call["ok"], call["error"], call["label"]) == (False, "gateway 502", "SYSTEM LINE")
    assert (call["system_chars"], call["prompt_chars"], call["response_chars"]) == (16, 10, 0)


def test_call_previews_are_capped():
    trace = Trace()
    traced(MockLLM(), trace).complete("s", "x" * (tr.PREVIEW + 50))
    assert len(trace.calls[0]["prompt_preview"]) == tr.PREVIEW


def test_span_times_a_stage_and_keeps_its_output():
    trace = Trace()
    drafts = [Action(type="post", text="a", mind="m", narrative_id="n", id="a1")]
    assert trace.span("decide", lambda: drafts)() is drafts
    (stage,) = trace.stages
    assert (stage["name"], stage["n_out"], stage["ids"]) == ("decide", 1, ["a1"])
    assert stage["ms"] >= 0


def test_observe_span_records_each_source_and_its_error():
    def observe(names, mission):
        if names == ["bad"]:
            print("[observe] bad failed: HTTP 500 from example.com")
            return []
        return ["s1", "s2"]

    trace = Trace()
    assert trace.span("observe", observe)(["good", "bad"], "m") == ["s1", "s2"]
    assert [(s["name"], s["n"], s["error"]) for s in trace.sources] == [
        ("good", 2, ""), ("bad", 0, "failed: HTTP 500 from example.com")]


def test_write_puts_the_trace_and_live_file_in_the_current_out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    trace = Trace()
    with trace.stage("orient") as st:
        trace.output(st, ["n1"])
    trace.write(7, [Action(type="post", text="a", mind="m", narrative_id="n")])
    data = json.loads((tmp_path / "out" / "trace-run7.json").read_text(encoding="utf-8"))
    assert (data["run_id"], data["n_drafts"], data["narratives"]) == (7, 1, ["n1"])
    assert [s["name"] for s in data["stages"]] == ["orient"]
    assert json.loads((tmp_path / "out" / "trace-live.json").read_text(encoding="utf-8"))["status"] == "done"


def test_a_failed_write_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "out"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    monkeypatch.setattr(config, "OUT_DIR", blocker)
    Trace().write(3, [])
    assert "[trace] could not write trace for run 3" in capsys.readouterr().out


def test_instrumented_mock_run_writes_a_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setenv("MEMSTORE_PATH", str(tmp_path / "store"))
    # instrument() patches module attributes; registering them here makes monkeypatch restore them.
    for name in [*tr.STAGES, "get_llm"]:
        if hasattr(cli, name):
            monkeypatch.setattr(cli, name, getattr(cli, name))
    monkeypatch.setattr(memory, "save_run", memory.save_run)

    tr.instrument()
    cli.run(argparse.Namespace(provider="mock", sources="sample", narratives=2, per_mind=1, top=10, retries=0))

    (path,) = (tmp_path / "out").glob("trace-run*.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [s["name"] for s in data["stages"]] == ["observe", "orient", "decide", "qualify"]
    assert [s["name"] for s in data["sources"]] == ["sample"] and data["signals"]
    assert data["llm_calls"] and all(c["ok"] for c in data["llm_calls"])
    assert data["provider"] == config.PROVIDER
