"""`harness improve validate`: the loop's own parser and admission on real instruments, no model call, and nothing
written where the live loop keeps its state."""

import argparse
from datetime import datetime, timedelta, timezone

import pytest
from watch_threads import Threads

from harness.improve import scheduler as sch
from harness.improve import validate
from harness.iterate import knobs

NOW = datetime(2026, 10, 16, 18, tzinfo=timezone.utc)

FRAMING = '''
id = "framing"
proposed_at = 2026-10-16T12:00:00Z
proposed_by = "agent:test"
profile = "wnba"
fitness = "action_lineup"
hypothesis = "Writing under the profile's framing handle reads less like AI."
threads = ["action/fixtures/x_threads.json"]

[baseline]
knobs = { "action.reply.generation" = "continuation" }

[change]
kind = "knobs"
knobs = { "action.continuation.framing" = "profile" }
'''

FILTER = '''
id = "filter"
proposed_at = 2026-10-16T12:00:00Z
proposed_by = "agent:test"
profile = "devtools"
fitness = "action_lineup"
hypothesis = "Dropping bot-like replies from the page reads less like AI."

[baseline]
knobs = { "action.reply.generation" = "continuation" }

[change]
kind = "knobs"
knobs = { "action.continuation.page_filter" = true }
'''


@pytest.fixture
def live(tmp_path):
    """Where a live loop would keep its state, which validate must never create, plus a watcher directory with
    six devtools threads the lineup can use (13 replies each) rooted before NOW."""
    threads = Threads(tmp_path / "threads")
    for n in range(6):
        threads.thread(NOW - timedelta(days=1, hours=n), timedelta(hours=30), list(range(13, 0, -1)),
                       texts=[f"reply number {i} with words" for i in range(13)],
                       root_text=f"a long enough root post number {n}")
    threads.write()
    cfg = sch.ImproveConfig(provider="anthropic", out_dir=tmp_path / "live-improve", ledger_dir=tmp_path / "ledger",
                            instruments_path=tmp_path / "instruments.json", threads_dir=tmp_path / "threads")
    return tmp_path, cfg


def _write(tmp_path, name, text):
    path = tmp_path / f"{name}.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_spec_with_a_baseline_is_admitted_with_its_items_budget_and_probes(live):
    tmp_path, cfg = live
    (v,) = validate.check([_write(tmp_path, "framing", FRAMING)], cfg, now=NOW)
    assert v.state == "ready" and v.admitted and v.plan.phase == "screen" and v.plan.mode == "screen"
    assert v.plan.base.get("action.reply.generation") == "continuation"
    assert knobs.diff(v.plan.base, v.plan.challenger) == {"action.continuation.framing": {"from": "neutral",
                                                                                         "to": "profile"}}
    n = len(v.plan.fit.items())
    assert v.items_by_source == {"fixture files": n, "watcher": 0, "corpus": 0} and n >= 15  # the watcher's are devtools
    assert v.plan.estimate == 12 * n  # raw continuation: the writer, two detectors, the veto; three arms
    assert v.plan.needed == {knobs.OPUS55, knobs.OPUS45, "claude-fable-5-dd-los-6.5-tpg"}
    assert v.plan.budget == 400 - 3  # the spec's default screen budget, less one probe per model
    assert not cfg.out_dir.exists()  # nothing written where a live loop keeps its state
    assert not (tmp_path / "ledger").exists()


def test_watcher_threads_are_read_from_a_copy_and_counted(live):
    tmp_path, cfg = live
    before = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "threads").iterdir()}
    (v,) = validate.check([_write(tmp_path, "filter", FILTER)], cfg, now=NOW)
    assert v.state == "ready" and v.items_by_source == {"fixture files": 0, "watcher": 6, "corpus": 0}
    assert v.plan.estimate == (4 + 4 + 5) * 6  # the challenger's page filter adds a call per thread
    assert {p.name: p.stat().st_mtime_ns for p in (tmp_path / "threads").iterdir()} == before


def test_the_parser_and_admission_refusals_come_back_with_their_reasons(live):
    tmp_path, cfg = live
    no_baseline = FRAMING.replace('[baseline]\nknobs = { "action.reply.generation" = "continuation" }\n', "")
    devtools = FRAMING.replace('profile = "wnba"', 'profile = "devtools"').replace(
        'threads = ["action/fixtures/x_threads.json"]\n', "")
    typo = FRAMING.replace("[change]", "[chnage]")
    verdicts = validate.check([_write(tmp_path, "framing", no_baseline)], cfg, now=NOW)
    verdicts += validate.check([_write(tmp_path, "framing", devtools)], cfg, now=NOW)
    verdicts += validate.check([_write(tmp_path, "framing", typo)], cfg, now=NOW)
    assert [v.state for v in verdicts] == ["refused"] * 3 and not any(v.admitted for v in verdicts)
    assert "neither arm reads ['action.continuation.framing']" in verdicts[0].note
    assert "has no handle" in verdicts[1].note
    assert "unknown key" in verdicts[2].note


def test_the_command_prints_each_decision_and_fails_when_any_spec_is_refused(live, capsys):
    tmp_path, cfg = live
    good = _write(tmp_path, "filter", FILTER)
    (tmp_path / "bad").mkdir()
    bad = _write(tmp_path / "bad", "x", "id = [")
    args = argparse.Namespace(specs=[str(good)], queue_dir=None, threads_dir=str(cfg.threads_dir),
                              ledger_dir=str(cfg.ledger_dir), instruments=str(cfg.instruments_path),
                              corpus_dir=None, experiment_calls=None)
    assert validate.main(args) == 0
    out = capsys.readouterr().out
    assert "filter.toml: admitted (ready)" in out and "estimated calls: 78" in out and "1 of 1 admitted" in out
    args.specs = [str(good), str(bad)]
    assert validate.main(args) == 1
    assert "x.toml: NOT ADMITTED (refused)" in capsys.readouterr().out


THIN = FILTER.replace('id = "filter"', 'id = "thin"').replace(
    '"action.continuation.page_filter" = true', '"action.page_replies" = 2').replace(
    "[baseline]", 'sources = ["watcher", "corpus"]\nmin_clusters = 30\n\n[baseline]') + """
[budget]
screen_calls = 500
"""


def _corpus(tmp_path):
    """30 supabase threads the devtools watch list covers, plus two it would drop: a sample under the coverage
    line and an author nobody watches."""
    threads = Threads(tmp_path / "corpus")
    texts = [f"reply number {i} with words" for i in range(13)]
    for n in range(30):
        threads.thread(NOW - timedelta(days=10, hours=n), timedelta(days=3), list(range(13, 0, -1)), author="supabase",
                       texts=texts, root_text=f"a long enough corpus root number {n}")
    threads.thread(NOW - timedelta(days=12), timedelta(days=3), list(range(13, 0, -1)), author="supabase", texts=texts,
                   root_text="a thread sampled too thinly", coverage=0.5)
    threads.thread(NOW - timedelta(days=12, hours=1), timedelta(days=3), list(range(13, 0, -1)), author="nobody",
                   texts=texts, root_text="a thread nobody watches here")
    return threads.write()


def test_a_corpus_source_adds_the_watched_threads_and_counts_them_apart(live):
    tmp_path, cfg = live
    cfg.corpus_dir = _corpus(tmp_path)
    (v,) = validate.check([_write(tmp_path, "thin", THIN)], cfg, now=NOW)
    assert v.state == "ready" and v.items_by_source == {"fixture files": 0, "watcher": 6, "corpus": 30}
    assert v.plan.estimate == 12 * 36 and v.plan.budget == 500 - 3
    assert "watcher threads (6) and corpus threads (30)" in v.plan.fit.instrument()["dataset"]
    lines = "\n".join(validate.render(v))
    assert "36 in 36 threads: 6 from watcher, 30 from corpus" in lines and "at least 30 clusters" in lines


def test_a_screen_short_of_its_floor_is_reported_waiting(live):
    tmp_path, cfg = live
    cfg.corpus_dir = _corpus(tmp_path)
    (v,) = validate.check([_write(tmp_path, "thin", THIN.replace("min_clusters = 30", "min_clusters = 40"))], cfg,
                          now=NOW)
    assert v.state == "waiting" and v.admitted and "a screen waiting for 40 threads rooted by" in v.note
    assert "it has 36" in v.note
