"""The review server: the original API keeps its shapes, POSTs only come from the page itself,
static files can't escape their folder, overrules and takes land in the hand-off file, and a round
started from the page always runs the same command."""

import io
import json
import os
import queue
import signal
import sys

import pytest

from harness import config, memory
from harness.models import Action
from harness.review import Handler, checkpoint, rounds

PORT = 8765
HELD = "https://x.com/WNBA/status/9"
QUOTED = "https://x.com/AtlantaDream/status/7"
# A decision log: a reply the gate held back with one draft, then a question for a person.
DECISIONS = [
    {"lane": "reply", "opportunity": HELD, "tool": "abstain", "reason": "gate_rejected",
     "detail": "0/2 usable", "candidates": [{"text": "first try", "gate": [{"rule": "em_dash"}]}]},
    {"lane": "post", "opportunity": "n1", "tool": "ask_human", "question": "Your take?", "candidates": []},
]
# The trace's narratives: one owns the held post as a reply target, the other cites the quoted post.
NARRATIVES = [
    {"id": "n-reply", "reply_targets": [{"url": HELD}], "signal_urls": []},
    {"id": "n-quote", "reply_targets": [], "signal_urls": ["https://espn.com/story", QUOTED]},
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    actions = [
        Action(type="reply", text="draft one", mind="reply_lane", narrative_id="n1", target_url="https://x.com/WNBA/status/1", id="a1"),
        Action(type="post", text="draft two", mind="stat_nerd", narrative_id="n1", id="a2"),
    ]
    return memory.save_run(actions)


@pytest.fixture
def port(store, monkeypatch):
    monkeypatch.setattr(Handler, "port", PORT)
    return PORT


class FakeConnection:
    """Feeds one raw HTTP request to the handler and collects its reply; tests never open sockets."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.out = bytearray()

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.raw)

    def sendall(self, data):
        self.out.extend(data)


def request(port, method, path, body=None, headers=None):
    h = {"Host": f"127.0.0.1:{port}"}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items())
    conn = FakeConnection(head.encode() + f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    Handler(conn, ("127.0.0.1", 0), None)
    status_line, _, rest = bytes(conn.out).partition(b"\r\n")
    payload = rest.split(b"\r\n\r\n", 1)[1]
    code = int(status_line.split()[1])
    try:
        return code, json.loads(payload)
    except json.JSONDecodeError:
        return code, payload


def write_decisions(run_id, decisions):
    config.OUT_DIR.mkdir(exist_ok=True)
    (config.OUT_DIR / f"decisions-run{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "decisions": decisions}), encoding="utf-8"
    )


def write_trace(run_id, narratives):
    config.OUT_DIR.mkdir(exist_ok=True)
    (config.OUT_DIR / f"trace-run{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "narratives": narratives}), encoding="utf-8"
    )


def handoff(run_id):
    return json.loads((config.OUT_DIR / f"approved-run{run_id}.json").read_text(encoding="utf-8"))


def review_of(run_id, action_id):
    return next(a for a in memory.run_actions(run_id) if a["id"] == action_id)["review"]


def test_original_routes_keep_their_shapes(port, store):
    code, runs = request(port, "GET", "/api/runs")
    assert code == 200 and runs[0]["id"] == store and runs[0]["n_actions"] == 2
    code, actions = request(port, "GET", f"/api/runs/{store}")
    assert code == 200 and {a["id"] for a in actions} == {"a1", "a2"}
    assert actions[0]["review"] == {"status": "pending", "text": None, "note": "", "reviewed_at": None}


def test_review_post_writes_the_handoff_file(port, store):
    code, res = request(port, "POST", "/api/actions/a1/review", {"status": "approved", "text": "edited", "note": "ok"})
    assert code == 200 and res["ok"]
    out = handoff(store)
    assert out["run_id"] == store
    assert [a["id"] for a in out["actions"]] == ["a1"]
    assert out["actions"][0]["text"] == "edited" and out["actions"][0]["original_text"] == "draft one"


def test_kill_reasons_are_kept(port, store):
    code, _ = request(port, "POST", "/api/actions/a2/review", {"status": "killed", "note": "meh", "reasons": ["slop", "tone"]})
    assert code == 200
    if hasattr(memory, "REVIEW_REASONS"):  # sam/memory stores them in review_events
        assert review_of(store, "a2")["note"] == "meh"
    else:
        assert review_of(store, "a2")["note"] == "Reasons: reads as AI, wrong tone. meh"


@pytest.mark.parametrize("reasons", [["made_up"], [[1]], "slop", ["slop", 3]])
def test_kill_reasons_must_be_a_list_of_known_codes(port, store, reasons):
    code, res = request(port, "POST", "/api/actions/a2/review", {"status": "killed", "reasons": reasons})
    assert code == 400 and "reasons" in res["error"], res
    assert review_of(store, "a2")["status"] == "pending"


@pytest.mark.parametrize(
    "headers, code",
    [
        ({"Host": "evil.example:80"}, 421),  # DNS rebinding: wrong Host
        ({"Content-Type": "text/plain"}, 403),  # a cross-site form can send text/plain without preflight
        ({"Origin": "https://evil.example"}, 403),
    ],
)
def test_posts_from_other_sites_are_refused(port, store, headers, code):
    got, res = request(port, "POST", "/api/actions/a1/review", {"status": "approved"}, headers)
    assert got == code, res
    assert review_of(store, "a1")["status"] == "pending"


def test_static_files_cannot_escape_their_folder(port):
    assert request(port, "GET", "/static/js/app.js")[0] == 200
    assert request(port, "GET", "/static/../__init__.py")[0] == 404
    assert request(port, "GET", "/static/%2e%2e/__init__.py")[0] == 404


def test_unknown_route_is_404_and_wrong_method_405(port):
    assert request(port, "GET", "/api/runs/1/nope") == (404, {"error": "not found"})
    assert request(port, "POST", "/api/runs", {}) == (405, {"error": "method not allowed"})


def test_overrule_ships_a_held_reply_once(port, store):
    write_decisions(store, DECISIONS)
    write_trace(store, NARRATIVES)
    body = {"decision": 0, "candidate": 0, "text": "first try, fixed"}
    code, res = request(port, "POST", f"/api/runs/{store}/overrule", body)
    assert code == 200, res
    a = res["action"]
    assert (a["type"], a["target_url"], a["target_handle"], a["origin"]) == ("reply", HELD, "WNBA", "overrule")
    assert a["narrative_id"] == "n-reply"  # the narrative whose reply targets hold the post
    code, again = request(port, "POST", f"/api/runs/{store}/overrule", body)
    assert code == 200 and again["action"]["id"] == a["id"]  # a double click doesn't add a second action
    assert [x["text"] for x in handoff(store)["actions"]] == ["first try, fixed"]


def test_a_post_lane_hold_on_an_x_post_is_overruled_as_a_quote(port, store):
    write_decisions(store, [{"lane": "post", "opportunity": QUOTED, "tool": "abstain", "reason": "judge_rejected",
                             "detail": "veto", "candidates": []}])
    write_trace(store, NARRATIVES)
    code, res = request(port, "POST", f"/api/runs/{store}/overrule", {"decision": 0, "candidate": None, "text": "my own caption"})
    assert code == 200, res
    q = res["action"]
    assert (q["type"], q["target_url"], q["narrative_id"], q["draft_meta"]["method"]) == (
        "quote", QUOTED, "n-quote", "written by a person, overruling an abstention")


@pytest.mark.parametrize(
    "body",
    [
        {"decision": 5, "text": "x"},  # no such decision
        {"decision": 0, "text": "  "},  # no words
        {"decision": 1, "text": "x"},  # a question for a person, not an abstention
        {"decision": 0, "candidate": 1, "text": "x"},  # the abstention has one candidate
        {"decision": 0, "candidate": "0", "text": "x"},
    ],
)
def test_overrule_refuses_a_bad_request(port, store, body):
    write_decisions(store, DECISIONS)
    code, res = request(port, "POST", f"/api/runs/{store}/overrule", body)
    assert code == 400, res
    assert memory.approved_actions(store) == []


def test_a_second_overrule_replaces_the_first(port, store):
    write_decisions(store, DECISIONS)
    url = f"/api/runs/{store}/overrule"
    code, first = request(port, "POST", url, {"decision": 0, "candidate": 0, "text": "first try, fixed"})
    assert code == 200, first
    code, second = request(port, "POST", url, {"decision": 0, "candidate": None, "text": "something else entirely"})
    assert code == 200, second
    assert second["action"]["id"] == first["action"]["id"]
    assert [a["text"] for a in handoff(store)["actions"]] == ["something else entirely"]


def test_a_take_is_cleared_in_the_persons_words(port, store):
    write_decisions(store, DECISIONS)
    code, res = request(port, "POST", f"/api/runs/{store}/takes", {"decision": 1, "text": "Boston played hurt all year."})
    assert code == 200, res
    a = res["action"]
    assert (a["type"], a["mind"], a["text"], a["origin"]) == ("post", "human", "Boston played hurt all year.", "take")
    assert a["draft_meta"]["method"] == "human take, verbatim"  # learning tells a person's words by this
    code, res2 = request(port, "POST", f"/api/runs/{store}/takes", {"decision": 1, "text": "Changed my mind."})
    assert code == 200, res2
    assert res2["action"]["id"] == a["id"]  # resubmitting replaces the take
    assert [x["text"] for x in memory.approved_actions(store)] == ["Changed my mind."]


def test_decision_log_problems_are_named(port, store):
    code, res = request(port, "GET", f"/api/runs/{store}/decisions")
    assert code == 200 and res["available"] is False and "decisions-run" in res["reason"]
    config.OUT_DIR.mkdir(exist_ok=True)
    (config.OUT_DIR / f"decisions-run{store}.json").write_text(json.dumps({"run_id": store + 1, "decisions": []}), encoding="utf-8")
    code, res = request(port, "GET", f"/api/runs/{store}/decisions")
    assert code == 500 and f"not {store}" in res["error"]


class FakeRound:
    """Stands in for a round's child process. It records how it was started, replays the lines a
    test feeds it, and exits when the test or the runner's stop says so. Nothing runs."""

    pid = 2**22 + 1  # above every OS's pid limit; the `child` fixture swaps os.killpg out anyway

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.feed = queue.SimpleQueue()
        self.stdout = iter(self.feed.get, None)
        self.code = None

    def say(self, *lines):
        for line in lines:
            self.feed.put(line + "\n")

    def exit(self, code):
        self.code = code
        self.feed.put(None)

    def wait(self):
        return self.code


@pytest.fixture
def child(port, monkeypatch):
    """A fresh runner whose rounds are FakeRounds; yields the ones started. Its stop reaches them
    through a stand-in os.killpg, so no test signals a real process."""
    started = []

    def popen(argv, **kwargs):
        started.append(FakeRound(argv, **kwargs))
        return started[-1]

    def killpg(pid, sig):
        next(p for p in started if p.pid == pid).exit(-sig)

    monkeypatch.setattr(rounds, "RUNNER", rounds.Runner())
    monkeypatch.setattr(rounds.subprocess, "Popen", popen)
    monkeypatch.setattr(rounds.os, "killpg", killpg, raising=False)
    yield started
    for p in started:
        p.exit(0)
    if rounds.RUNNER.pump:  # let the pump save into tmp_path before monkeypatch restores OUT_DIR
        rounds.RUNNER.pump.join(timeout=5)


def test_request_data_never_reaches_a_rounds_command(port, child):
    # The page can start a round but never shape its command line (lead's requirement).
    code, rnd = request(port, "POST", "/api/rounds", {"provider": "openai", "argv": ["--top", "99"]})
    assert code == 200 and rnd["status"] == "running", rnd
    (proc,) = child
    assert proc.argv == [sys.executable, *rounds.ARGV]
    assert proc.kwargs["env"] == {**os.environ, "PYTHONUNBUFFERED": "1"} and "shell" not in proc.kwargs
    assert request(port, "POST", "/api/rounds", {})[0] == 409  # one round at a time
    proc.say("[observe] sample: 4 signals", "[decide] 5 minds proposed 5 actions", "Top 1 recommendations (run 3) -> x")
    proc.exit(0)
    rounds.RUNNER.pump.join(timeout=5)
    assert not rounds.RUNNER.pump.is_alive()
    snap = rounds.RUNNER.snapshot()
    assert (snap["status"], snap["run_id"], snap["exit_code"]) == ("done", 3, 0)
    assert [ln["stage"] for ln in snap["lines"]] == ["observe", "decide", None]


def test_a_stopped_round_stays_stopped_after_it_exits(port, child):
    assert rounds.last_saved() is None  # no out/rounds/ yet
    request(port, "POST", "/api/rounds", {})
    code, rnd = request(port, "POST", "/api/rounds/stop", {})
    assert code == 200 and rnd["status"] == "stopped", rnd
    rounds.RUNNER.pump.join(timeout=5)
    assert not rounds.RUNNER.pump.is_alive()
    assert (rounds.RUNNER.snapshot()["exit_code"], rounds.last_saved()["status"]) == (-signal.SIGTERM, "stopped")
    assert request(port, "POST", "/api/rounds/stop", {})[0] == 409


def test_gate_route_says_why_when_the_gate_is_missing(port):
    code, res = request(port, "POST", "/api/gate", {"text": "hello", "kind": "reply"})
    assert code == 200
    try:
        import harness.minds.gate  # noqa: F401
    except ImportError:
        assert res["available"] is False and "harness.minds.gate" in res["reason"]
    else:
        assert res["available"] is True and "passed" in res


def test_notes_refuse_more_than_fifty_ids(port):
    code, res = request(port, "GET", "/api/memory/notes?ids=" + ",".join(f"n{i}" for i in range(51)))
    assert code == 400 and "at most 50" in res["error"], res


def test_meta_lists_kill_reasons_matching_memory():
    reasons = checkpoint.get_meta(None)["kill_reasons"]
    if hasattr(memory, "REVIEW_REASONS"):
        assert set(reasons) == set(memory.REVIEW_REASONS)
    assert "slop" in reasons and "other" in reasons
