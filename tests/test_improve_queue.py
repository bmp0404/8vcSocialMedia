"""Queue spec parsing: every malformed spec is refused with the reason, and a good one round-trips."""

import subprocess
from datetime import datetime, timezone

import pytest

from harness.improve import queue
from harness.iterate.fitness import Metric


class Lineup:
    knobs = ("action.reply.generation", "action.samples")
    llm_opinion = False
    secondaries = (Metric("veto_pass_rate", True, guardrail=True, margin=0.10), Metric("reply_chars", False))


class Opinion(Lineup):
    llm_opinion = True


class Undeclared:
    knobs = Lineup.knobs
    secondaries = Lineup.secondaries


CATALOG = {"action_lineup": Lineup, "judge_score": Opinion, "mystery": Undeclared}

GOOD = '''
id = "styled"
proposed_at = 2026-10-16T14:00:00Z
proposed_by = "agent:test"
profile = "wnba"
fitness = "action_lineup"
hypothesis = "Short style rules read less like AI."
source = "research/action.md, finding 1"

[change]
kind = "knobs"
knobs = { "action.reply.generation" = "styled" }

[guardrails]
veto_pass_rate = 0.05
reply_chars = 20

[budget]
screen_calls = 300
'''


def write(tmp_path, text, name="styled.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_good_spec_parses_with_defaults_filled():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        spec = queue.parse(write(Path(d), GOOD), CATALOG)
    assert spec.id == "styled" and spec.kind == "knobs" and spec.knobs == {"action.reply.generation": "styled"}
    assert spec.proposed_at == datetime(2026, 10, 16, 14, tzinfo=timezone.utc)
    assert spec.guardrails == {"veto_pass_rate": 0.05, "reply_chars": 20.0}
    assert (spec.screen_calls, spec.confirm_calls) == (300, queue.DEFAULT_CALLS)
    prop = spec.proposer("screen", spec.proposed_at)
    assert prop["kind"] == "queue" and prop["spec"] == "styled" and prop["phase"] == "screen"


@pytest.mark.parametrize("edit, message", [
    (lambda t: t.replace('id = "styled"', 'id = "Styled!"'), "lowercase"),
    (lambda t: t.replace("2026-10-16T14:00:00Z", "2026-10-16T14:00:00"), "timezone"),
    (lambda t: t.replace('proposed_by = "agent:test"\n', ""), "missing"),
    (lambda t: t.replace('profile = "wnba"', 'profile = "nba"'), "no mission profile"),
    (lambda t: t.replace('fitness = "action_lineup"', 'fitness = "nope"'), "unknown fitness"),
    (lambda t: t.replace('fitness = "action_lineup"', 'fitness = "judge_score"'), "no LLM opinion score"),
    (lambda t: t.replace('fitness = "action_lineup"', 'fitness = "mystery"'), "llm_opinion"),
    (lambda t: t.replace('kind = "knobs"', 'kind = "vibes"'), "change.kind"),
    (lambda t: t.replace('"action.reply.generation" = "styled"', '"action.reply.generation" = "poetry"'), "not one of"),
    (lambda t: t.replace('"action.reply.generation" = "styled"', '"judge.model" = "gpt-6.1-sol"'), "can't measure"),
    (lambda t: t.replace('knobs = { "action.reply.generation" = "styled" }', "knobs = {}"), "one or two knobs"),
    (lambda t: t.replace("veto_pass_rate = 0.05", "veto_pass_rate = 0.5"), "looser"),
    (lambda t: t.replace("veto_pass_rate = 0.05", "likes = 0.05"), "no secondary metric"),
    (lambda t: t.replace("veto_pass_rate = 0.05", "veto_pass_rate = -1"), "non-negative"),
    (lambda t: t.replace("screen_calls = 300", "screen_calls = 99999"), "budget.screen_calls"),
    (lambda t: "temperature = 0.7\n" + t, "unknown key"),
    (lambda t: t + "\ntemperature = 0.7\n", r"\[budget\] takes only"),
    (lambda t: t.replace('kind = "knobs"', 'kind = "knobs"\ncommit = "abc1234"'), "belong to kind"),
    (lambda t: t.replace('kind = "knobs"', 'kind = "code"'), "commit"),
    (lambda t: "id = [", "not valid TOML"),
])
def test_bad_specs_are_refused_with_the_reason(tmp_path, edit, message):
    with pytest.raises(queue.SpecError, match=message):
        queue.parse(write(tmp_path, edit(GOOD)), CATALOG)


def test_the_file_name_must_match_the_id(tmp_path):
    with pytest.raises(queue.SpecError, match="must be named styled.toml"):
        queue.parse(write(tmp_path, GOOD, "other.toml"), CATALOG)


def test_a_code_change_needs_a_commit_in_this_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True)  # noqa: E731
    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    head = git("rev-parse", "HEAD").stdout.strip()
    code = GOOD.replace('kind = "knobs"', f'kind = "code"\ncommit = "{head[:10]}"\nsummary = "frame the page from the mission\'s side"')
    spec = queue.parse(write(tmp_path, code), CATALOG, repo=repo)
    assert spec.kind == "code" and spec.commit == head[:10]
    assert spec.proposer("confirm", spec.proposed_at)["commit"] == head[:10]
    stranger = code.replace(head[:10], "deadbeef00")
    with pytest.raises(queue.SpecError, match="isn't in this checkout's history"):
        queue.parse(write(tmp_path, stranger), CATALOG, repo=repo)
    no_summary = code.replace('summary = "frame the page from the mission\'s side"', "")
    with pytest.raises(queue.SpecError, match="summary"):
        queue.parse(write(tmp_path, no_summary), CATALOG, repo=repo)


def test_load_keeps_good_specs_and_reports_bad_ones(tmp_path):
    write(tmp_path, GOOD)
    write(tmp_path, GOOD.replace('"styled"', '"broken"', 1).replace("action_lineup", "nope"), "broken.toml")
    specs, errors = queue.load(tmp_path, CATALOG)
    assert [s.id for s in specs] == ["styled"]
    assert list(errors) == ["broken.toml"] and "unknown fitness" in errors["broken.toml"]
    assert queue.load(tmp_path / "absent", CATALOG) == ([], {})


def _rec(phase, status):
    return {"proposer": {"kind": "queue", "spec": "styled", "phase": phase}, "decision": {"status": status}}


@pytest.mark.parametrize("records, expected", [
    ([], "screen"),
    ([_rec("screen", "pending")], "screen"),
    ([_rec("screen", "inconclusive")], "done"),
    ([_rec("screen", "rejected")], "done"),
    ([_rec("screen", "candidate")], "confirm"),
    ([_rec("screen", "candidate"), _rec("confirm", "pending")], "confirm"),
    ([_rec("screen", "candidate"), _rec("confirm", "promoted")], "done"),
    ([_rec("screen", "candidate"), _rec("confirm", "inconclusive")], "done"),
])
def test_phase_follows_the_ledger(records, expected):
    other = {"proposer": {"kind": "queue", "spec": "else", "phase": "screen"}, "decision": {"status": "candidate"}}
    assert queue.phase("styled", records + [other]).next == expected
