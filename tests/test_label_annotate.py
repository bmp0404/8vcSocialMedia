"""harness.label: alpha, label validation, prompt rendering and the annotation runner with a fake client."""

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx2 as httpx  # anthropic 1.x is built on httpx2
import pytest

from harness.label import annotate
from harness.label.agreement import alpha, percent_agreement
from harness.label.prompt import prompt_sha, system_prompt, user_message
from harness.label.schema import DROPPED, SCHEMA, SCHEMA_V1, contract_schema, validate_labels


def test_alpha_matches_krippendorffs_worked_example():
    # Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability", four coders, twelve units.
    n = None
    coders = [[1, 2, 3, 3, 2, 1, 4, 1, 2, n, n, n], [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, n, 3],
              [n, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, n], [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, n]]
    units = [[v for v in col if v is not None] for col in zip(*coders)]
    assert alpha(units, "nominal") == pytest.approx(0.743, abs=5e-4)
    assert alpha(units, "ordinal", order=[1, 2, 3, 4, 5]) == pytest.approx(0.815, abs=5e-4)
    assert alpha(units, "interval") == pytest.approx(0.849, abs=5e-4)


def test_alpha_edge_cases():
    assert alpha([["a", "a"], ["b", "b"]]) == 1.0
    assert alpha([["lo", "hi"], ["hi", "lo"]], "ordinal", order=["lo", "hi"]) < 0
    with pytest.raises(ValueError, match="undefined"):
        alpha([["a", "a"], ["a", "a"]])
    with pytest.raises(ValueError, match="two or more"):
        alpha([["a"], []])
    with pytest.raises(ValueError, match="not in the ordinal order"):
        alpha([["x", "y"]], "ordinal", order=["x"])
    assert percent_agreement([["a", "a"], ["a", "b"]]) == 0.5


def test_validate_labels_by_kind():
    post = {"format": "announcement", "specificity": 3, "novelty": 2, "humor": "none", "reads_as_ai": 2,
            "engagement_guess": "typical", "engagement_bait": "no", "hostility": "none"}
    assert validate_labels(post, "post", SCHEMA_V1) == []
    assert validate_labels(post, "reply", SCHEMA_V1) == ["missing adds_to_parent"]
    assert validate_labels(post | {"adds_to_parent": 3}, "post", SCHEMA_V1) == ["unexpected field adds_to_parent"]
    assert validate_labels(post | {"specificity": "3"}, "post", SCHEMA_V1) == ["specificity='3' is not one of [1, 2, 3, 4, 5]"]
    # Version 2 drops two fields; an answer that still carries them is rejected rather than half-stored.
    v2 = {k: v for k, v in post.items() if k not in DROPPED}
    assert validate_labels(v2, "post") == [] and validate_labels(v2, "reply") == []
    assert validate_labels(post, "post") == ["unexpected field engagement_bait"]


def test_contract_schema_has_exactly_the_contract_keys():
    c = contract_schema()
    assert set(c) == {"version", "fields"}
    assert all(set(f) == {"name", "kind", "values", "question", "applies_to"} for f in c["fields"])


ITEM = {"item_id": "x:2", "kind": "reply", "author": "fan", "author_followers": 1200, "text": "@acme nice, does it run on arm64?",
        "created_at": "2026-10-01T12:30:00+00:00", "parent_id": "x:1",
        "metrics": {"likes": 999}, "context": {"minutes_after_parent": 30.0, "media_types": [], "n_links": 0}}
PARENT = {"item_id": "x:1", "kind": "post", "author": "acme", "text": "We shipped v2.", "created_at": "2026-10-01T12:00:00+00:00",
          "context": {"media_types": ["photo"]}}


def test_user_message_shows_context_but_never_counts():
    msg = user_message(ITEM, PARENT)
    assert "@fan (1,200 followers)" in msg and "30 minutes after" in msg
    assert "Replying to @acme, whose post (with photo) said:" in msg and "We shipped v2." in msg
    assert "999" not in msg and "likes" not in msg.lower().replace("likely", "")
    assert "adds_to_parent" in user_message(ITEM, PARENT, SCHEMA_V1)
    assert "adds_to_parent" not in user_message(PARENT | {"author_followers": 5}, None, SCHEMA_V1).split("Fields to answer:")[1]
    assert len(prompt_sha()) == 16 and prompt_sha() != prompt_sha(SCHEMA_V1)
    assert "Reply with one JSON object" in system_prompt() and "adds_to_parent" not in system_prompt()
    assert "adds_to_parent apply to replies only" in system_prompt(SCHEMA_V1)


GOOD = {"format": "question", "specificity": 3, "novelty": 2, "humor": "none", "reads_as_ai": 1,
        "engagement_guess": "typical", "hostility": "none"}


def response(text, model="m-1", stop="end_turn"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=7, cache_creation_input_tokens=0)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage, model=model, stop_reason=stop)


def status_error(code):
    req = httpx.Request("POST", "http://127.0.0.1/v1/messages")
    resp = httpx.Response(code, request=req, headers={"retry-after": "0"})
    cls = anthropic.RateLimitError if code == 429 else anthropic.InternalServerError if code >= 500 else anthropic.BadRequestError
    return cls("boom", response=resp, body=None)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def run_one(script, tmp_path, budget=50):
    client = FakeClient(script)
    guard = annotate.Guard(budget_left=budget)
    stats = annotate.RunStats()
    with (tmp_path / "calls.jsonl").open("w", encoding="utf-8") as log:
        row = asyncio.run(annotate.annotate_one(client, annotate.Annotator("m"), "r", ITEM, PARENT, guard, stats, log))
    calls = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    return row, calls, stats, client


def test_annotate_success_records_usage_and_response_model(tmp_path):
    row, calls, stats, _ = run_one([response(json.dumps({"labels": GOOD, "rationale": "asks about arm64"}))], tmp_path)
    assert row["error"] is None and row["labels"] == GOOD and row["annotator"]["response_model"] == "m-1"
    assert set(row) == {"item_id", "run_id", "annotator", "labels", "rationale", "usage", "at", "error"}
    assert {"model", "response_model", "prompt_sha"} <= set(row["annotator"])
    assert row["usage"]["cache_read_input_tokens"] == 7 and calls[0]["ok"] and stats.calls == 1


def test_annotate_retries_transient_errors_and_bad_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(annotate.asyncio, "sleep", lambda s, _real=asyncio.sleep: _real(0))
    bad = response(json.dumps({"labels": GOOD | {"specificity": 9}}))
    good = response(json.dumps({"labels": GOOD, "rationale": "ok"}))
    row, calls, stats, client = run_one([status_error(503), status_error(429), bad, good], tmp_path)
    assert row["error"] is None and client.calls == 4
    assert [c["ok"] for c in calls] == [False, False, False, True]
    assert stats.errors == {"http_503": 1, "http_429": 1, "invalid_answer": 1}


def test_annotate_gives_up_on_a_client_error_and_records_it(tmp_path):
    row, calls, _, client = run_one([status_error(400)], tmp_path)
    assert client.calls == 1 and row["labels"] == {} and row["error"].startswith("http_400")


def test_annotate_stops_after_max_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(annotate.asyncio, "sleep", lambda s, _real=asyncio.sleep: _real(0))
    row, _, _, client = run_one([response("not json")] * annotate.MAX_ATTEMPTS, tmp_path)
    assert client.calls == annotate.MAX_ATTEMPTS and row["error"].startswith("invalid answer")


def test_guard_budget_and_failure_rate_stop():
    g = annotate.Guard(budget_left=1)
    g.take_call()
    with pytest.raises(annotate.BudgetExceeded):
        g.take_call()
    g = annotate.Guard(budget_left=10**6)
    for i in range(annotate.FAIL_WINDOW):
        g.record(i % 5 != 4, "gpt")  # exactly 20% failed: not more than 20%, keep going
        g.record(True, "claude")
    assert g.stopped is None
    g.record(False, "gpt")  # drops a success from gpt's window, which now holds 41 failures in 200
    assert g.stopped == "gpt: 41 of its last 200 calls failed"
    with pytest.raises(annotate.FailureRateStop):
        g.take_call("gpt")
    g.take_call("claude")  # a healthy model keeps its calls


def test_reply_label_votes_needs_every_reference_model(tmp_path):
    from harness.label.features import reply_label_votes

    def row(item, model, labels, at, error=None):
        return json.dumps({"item_id": item, "run_id": "r", "annotator": {"model": model}, "labels": labels,
                           "at": at, "error": error}) + "\n"
    (tmp_path / "a.jsonl").write_text(
        row("x:1", "c", {"specificity": 2}, "1") + row("x:1", "c", {"specificity": 4}, "3")
        + row("x:2", "c", {"specificity": 1}, "1"), encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(
        row("x:1", "g", {"specificity": 3}, "2") + row("x:2", "g", {}, "2", error="http_503")
        + row("x:1", "other", {"specificity": 5}, "2"), encoding="utf-8")
    votes, _ = reply_label_votes(("c", "g"), SCHEMA, tmp_path)
    assert votes == {"x:1": {"specificity": [4, 3]}}  # x:2 lacks a good answer from g


def test_guard_seeds_each_window_from_the_call_logs(tmp_path):
    rows = [{"at": f"2026-10-16T09:{i // 60:02d}:{i % 60:02d}", "model": "gpt", "ok": i % 2 == 0} for i in range(200)]
    rows += [{"at": "2026-10-16T08:00:00", "model": "claude", "ok": True}]
    (tmp_path / "run.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    g = annotate.Guard(budget_left=10)
    g.seed(tmp_path)
    assert "gpt" in g.stopped_models and "claude" not in g.stopped_models
