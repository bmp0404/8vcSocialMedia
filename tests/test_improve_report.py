"""The morning report: every section present, demotions and pending human work surfaced, under a page."""

from datetime import datetime, timedelta, timezone

from harness.improve import report, scheduler
from harness.improve.watchday import Day

NOW = datetime(2026, 10, 22, 11, 5, tzinfo=timezone.utc)


def _instrument(status, est=0.6, ci=(0.55, 0.66), n=30):
    return {"status": status, "cutoff": "2026-10-16T14:00:00+00:00",
            "scores": {"threads": {"n": n, "estimate": est, "ci95": list(ci), "coverage": 1.0,
                                   "detail": {"origins": {"watch": n}, "pass_rate_on_real_replies": 0.8}},
                       "sgu": {"n": 0}}}


def test_the_report_names_demotions_decisions_and_what_needs_a_person():
    rec = {"id": "exp-x", "proposer": {"kind": "queue", "spec": "styled", "phase": "screen"},
           "eval": {"fitness": "action_lineup", "n": 24, "unit": "thread", "metrics": [
               {"name": "ai_detection_rate", "primary": True, "champion": {"value": 0.92}, "challenger": {"value": 0.5},
                "delta": {"value": -0.42, "ci95": [-0.6, -0.25]}},
               {"name": "abstain_rate", "champion": {"value": 0.0}, "challenger": {"value": 0.1},
                "delta": {"value": 0.1}}]},
           "decision": {"status": "candidate", "reason": "cleared the gate on the screening items"}}
    day = Day(NOW - timedelta(hours=24), running=True, polls=1000, poll_errors=90, handles_polled=45, posts_seen=210,
              fresh_posts=4, hot=[{"status_id": "1", "profile": "devtools", "handle": "vercel", "state": "done"}],
              kept={"+24h": 20, "+48h": 7}, read={"kept": 27, "low_coverage": 5})
    data = report.ReportData(
        now=NOW, since=NOW - timedelta(days=1), cycles=4, llm_calls=212, facts={"devtools": "[facts] devtools: wrote 30 facts (posts:a 10, posts:b 20); skipped 1 unreadable account(s): @c"},
        fold={"pairs": 140, "threads": 7, "weeks": {"5": {}}, "refused": {}},
        instruments={"instruments": {"slop_gate": _instrument("report_only", 0.52, (0.44, 0.6)),
                                     "reply_ranker": _instrument("gates")}},
        events=[{"instrument": "slop_gate", "event": "status", "from": "gates", "to": "report_only",
                 "why": "threads: pairwise_accuracy on 30 outcomes, 0.520 [0.440, 0.600], includes chance (0.5)"}],
        decided=[rec], champion="cfg-abc", queue=[{"spec": "qual", "fitness": "qualify_auc", "profile": "devtools",
                                                  "state": "waiting", "note": "a candidate waiting for 20 accounts"}],
        queue_errors={"bad.toml": "unknown fitness 'x'"}, day=day,
        hot_offers={"1": {"arm": "continuation", "offer": "draft"}}, disk_free_gb=6.5)
    text = report.render(data)
    for heading in ("## What ran and what changed", "## Instruments", "## Experiments", "## The watcher's day",
                    "## Needs a person"):
        assert heading in text
    assert "Slop gate dropped to report-only" in text and "Slop gate is report-only now" in text
    assert "ai_detection_rate 0.920 to 0.500, difference -0.420 [-0.600, -0.250]" in text
    assert "coverage 90% against 100%" in text
    assert "1 hot target: 1 offered a draft or asked for a one-liner (@vercel (arm continuation))" in text
    assert "Reply from _sgu_ whatever the arm" in text and "bad.toml" in text and "6.5 GB" in text
    assert "qual (qualify_auc, devtools): a candidate waiting for 20 accounts" in text
    assert "Facts for devtools: wrote 30 facts from 2 sources; 1 account(s) fxtwitter couldn't read were left out (@c)" in text
    assert len(text.splitlines()) < 60  # a page


def test_mock_runs_keep_their_state_apart_from_the_live_loop():
    mock = scheduler.ImproveConfig(provider="mock")
    live = scheduler.ImproveConfig(provider="anthropic")
    assert mock.out_dir == scheduler.OUT_DIR / "mock" and mock.ledger_dir == mock.out_dir / "experiments"
    assert live.out_dir == scheduler.OUT_DIR and live.ledger_dir.name == "experiments"
    assert live.instruments_path.name == "instruments.json" and "mock" not in str(live.instruments_path)


def test_clip_cuts_at_a_clause_and_never_inside_a_word():
    assert report._clip("short reason.") == "short reason"
    long = "first clause is here; " + "x" * 300
    assert report._clip(long, 40) == "first clause is here"
    words = "word " * 100
    clipped = report._clip(words, 33)
    assert len(clipped) <= 33 and clipped.endswith("word")


def test_api_errors_read_as_a_status_and_the_gateway_message():
    class Err(Exception):
        status_code = 429
        body = {"type": "error", "error": {"type": "rate_limit_error", "message":
                "All credentials for model gpt-5.6-sol are cooling down (last error: usage_limit_reached: ...)"}}

    assert scheduler.api_error(Err("raw body")) == "HTTP 429: All credentials for model gpt-5.6-sol are cooling down"
    assert scheduler.api_error(TimeoutError("read timed out")) == "TimeoutError: read timed out"
