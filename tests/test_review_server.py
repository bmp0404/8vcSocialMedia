"""The review server: the original API keeps its shapes, POSTs only come from the page itself,
static files can't escape their folder, and overrules and takes land in the hand-off file."""

import io
import json

import pytest

from harness import config, memory
from harness.models import Action
from harness.review import Handler, checkpoint

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


def test_gate_route_says_why_when_the_gate_is_missing(port):
    code, res = request(port, "POST", "/api/gate", {"text": "hello", "kind": "reply"})
    assert code == 200
    try:
        import harness.minds.gate  # noqa: F401
    except ImportError:
        assert res["available"] is False and "harness.minds.gate" in res["reason"]
    else:
        assert res["available"] is True and "passed" in res


def test_meta_lists_kill_reasons_matching_memory():
    reasons = checkpoint.get_meta(None)["kill_reasons"]
    if hasattr(memory, "REVIEW_REASONS"):
        assert set(reasons) == set(memory.REVIEW_REASONS)
    assert "slop" in reasons and "other" in reasons
