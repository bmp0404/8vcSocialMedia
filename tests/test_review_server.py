"""The review server: the original API keeps its shapes, POSTs only come from the page itself,
static files can't escape their folder, and overrules and takes land in the hand-off file."""

import io
import json

import pytest

from harness import config, memory
from harness.models import Action
from harness.review import Handler, checkpoint, rounds


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    actions = [
        Action(type="reply", text="draft one", mind="reply_lane", narrative_id="n1", target_url="https://x.com/WNBA/status/1", id="a1"),
        Action(type="post", text="draft two", mind="stat_nerd", narrative_id="n1", id="a2"),
    ]
    run_id = memory.save_run(actions)
    return run_id


PORT = 8765


@pytest.fixture
def server(store):
    Handler.port = PORT
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


def test_original_routes_keep_their_shapes(server, store):
    code, runs = request(server, "GET", "/api/runs")
    assert code == 200 and runs[0]["id"] == store and runs[0]["n_actions"] == 2
    code, actions = request(server, "GET", f"/api/runs/{store}")
    assert code == 200 and {a["id"] for a in actions} == {"a1", "a2"}
    assert actions[0]["review"] == {"status": "pending", "text": None, "note": "", "reviewed_at": None}


def test_review_post_writes_the_handoff_file(server, store):
    code, res = request(server, "POST", "/api/actions/a1/review", {"status": "approved", "text": "edited", "note": "ok"})
    assert code == 200 and res["ok"]
    handoff = json.loads((config.OUT_DIR / f"approved-run{store}.json").read_text(encoding="utf-8"))
    assert handoff["run_id"] == store
    assert [a["id"] for a in handoff["actions"]] == ["a1"]
    assert handoff["actions"][0]["text"] == "edited" and handoff["actions"][0]["original_text"] == "draft one"


def test_kill_reasons_are_validated_and_kept(server, store):
    code, res = request(server, "POST", "/api/actions/a2/review", {"status": "killed", "reasons": ["made_up"]})
    assert code == 400 and "reasons" in res["error"]
    code, _ = request(server, "POST", "/api/actions/a2/review", {"status": "killed", "note": "meh", "reasons": ["slop", "tone"]})
    assert code == 200
    review = next(a for a in memory.run_actions(store) if a["id"] == "a2")["review"]
    if hasattr(memory, "REVIEW_REASONS"):  # sam/memory stores them in review_events
        assert review["note"] == "meh"
    else:
        assert review["note"] == "Reasons: reads as AI, wrong tone. meh"


@pytest.mark.parametrize(
    "headers, code",
    [
        ({"Host": "evil.example:80"}, 421),  # DNS rebinding: wrong Host
        ({"Content-Type": "text/plain"}, 403),  # a cross-site form can send text/plain without preflight
        ({"Origin": "https://evil.example"}, 403),
    ],
)
def test_posts_from_other_sites_are_refused(server, store, headers, code):
    got, res = request(server, "POST", "/api/actions/a1/review", {"status": "approved"}, headers)
    assert got == code, res
    assert next(a for a in memory.run_actions(store) if a["id"] == "a1")["review"]["status"] == "pending"


def test_static_files_cannot_escape_their_folder(server):
    assert request(server, "GET", "/static/js/app.js")[0] == 200
    assert request(server, "GET", "/static/../__init__.py")[0] == 404
    assert request(server, "GET", "/static/%2e%2e/__init__.py")[0] == 404
    assert request(server, "GET", "/api/runs/1/nope")[0] == 404
    assert request(server, "POST", "/api/runs", {})[0] == 405


def test_overrule_ships_a_held_draft_once(server, store):
    write_decisions(store, [
        {"lane": "reply", "opportunity": "https://x.com/WNBA/status/9", "tool": "abstain", "reason": "gate_rejected",
         "detail": "0/2 usable", "candidates": [{"text": "first try", "gate": [{"rule": "em_dash"}]}]},
        {"lane": "post", "opportunity": "n1", "tool": "ask_human", "question": "Your take?", "candidates": []},
    ])
    body = {"decision": 0, "candidate": 0, "text": "first try, fixed"}
    code, res = request(server, "POST", f"/api/runs/{store}/overrule", body)
    assert code == 200, res
    assert res["action"]["type"] == "reply" and res["action"]["target_url"] == "https://x.com/WNBA/status/9"
    assert res["action"]["origin"] == "overrule" and res["action"]["target_handle"] == "WNBA"
    again = request(server, "POST", f"/api/runs/{store}/overrule", body)[1]
    assert again["action"]["id"] == res["action"]["id"]  # a double click doesn't add a second action
    handoff = json.loads((config.OUT_DIR / f"approved-run{store}.json").read_text(encoding="utf-8"))
    assert [a["text"] for a in handoff["actions"]] == ["first try, fixed"]
    # A post-lane hold on an X post is overruled as a quote of that post.
    write_decisions(store, [{"lane": "post", "opportunity": "https://x.com/AtlantaDream/status/7", "tool": "abstain",
                             "reason": "judge_rejected", "detail": "veto", "candidates": []}])
    q = request(server, "POST", f"/api/runs/{store}/overrule", {"decision": 0, "text": "my own caption"})[1]["action"]
    assert (q["type"], q["target_url"], q["draft_meta"]["method"]) == (
        "quote", "https://x.com/AtlantaDream/status/7", "written by a person, overruling an abstention")
    write_decisions(store, [
        {"lane": "reply", "opportunity": "https://x.com/WNBA/status/9", "tool": "abstain", "reason": "gate_rejected",
         "detail": "0/2 usable", "candidates": [{"text": "first try", "gate": [{"rule": "em_dash"}]}]},
        {"lane": "post", "opportunity": "n1", "tool": "ask_human", "question": "Your take?", "candidates": []},
    ])
    # Only abstentions can be overruled, and only with text.
    assert request(server, "POST", f"/api/runs/{store}/overrule", {"decision": 1, "text": "x"})[0] == 400
    assert request(server, "POST", f"/api/runs/{store}/overrule", {"decision": 0, "text": "  "})[0] == 400
    assert request(server, "POST", f"/api/runs/{store}/overrule", {"decision": 5, "text": "x"})[0] == 400


def test_a_take_is_cleared_in_the_persons_words(server, store):
    write_decisions(store, [{"lane": "post", "opportunity": "n1", "tool": "ask_human", "question": "Your take?", "candidates": []}])
    code, res = request(server, "POST", f"/api/runs/{store}/takes", {"decision": 0, "text": "Boston played hurt all year."})
    assert code == 200, res
    a = res["action"]
    assert (a["type"], a["mind"], a["text"], a["origin"]) == ("post", "human", "Boston played hurt all year.", "take")
    assert a["draft_meta"]["method"] == "human take, verbatim"  # learning tells a person's words by this
    code, res2 = request(server, "POST", f"/api/runs/{store}/takes", {"decision": 0, "text": "Changed my mind."})
    assert res2["action"]["id"] == a["id"]  # resubmitting replaces the take
    approved = memory.approved_actions(store)
    assert [x["text"] for x in approved] == ["Changed my mind."]


def test_decision_log_problems_are_named(server, store):
    code, res = request(server, "GET", f"/api/runs/{store}/decisions")
    assert code == 200 and res["available"] is False and "decisions-run" in res["reason"]
    config.OUT_DIR.mkdir(exist_ok=True)
    (config.OUT_DIR / f"decisions-run{store}.json").write_text(json.dumps({"run_id": store + 1, "decisions": []}), encoding="utf-8")
    code, res = request(server, "GET", f"/api/runs/{store}/decisions")
    assert code == 500 and f"not {store}" in res["error"]


def test_round_command_is_fixed():
    # The page can start a round but never shape its command line (lead's requirement).
    assert rounds.ARGV == ["-m", "harness", "run", "--narratives", "2", "--per-mind", "1", "--retries", "1"]


def test_gate_route_says_why_when_the_gate_is_missing(server):
    code, res = request(server, "POST", "/api/gate", {"text": "hello", "kind": "reply"})
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
