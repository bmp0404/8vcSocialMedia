"""The review server: POSTs only come from the page itself, the queue puts hot targets first, a verdict on a
hot target lands in memory and the hand-off under the outcomes ledger's id, posts come from syndication or
the harness's own copy, history pairs verdicts with outcomes, and audit hides the models' votes until the
person answers."""

import io
import json
import os
import shutil
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness import config, memory
from harness.context.http import FetchError
from harness.models import Action
from harness.review import Handler, audit, history, hot, queue, status, tweets

PORT = 8765
FIXTURES = Path(__file__).parent / "fixtures"
CONTINUATION, STYLED, HUMAN = "2110885747083399199", "2110896237490942447", "2110838124351836441"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(tweets, "CACHE_DIR", tmp_path / "tweets")
    monkeypatch.setattr(Handler, "port", PORT)
    monkeypatch.delenv("HARNESS_ACCOUNT_HANDLE", raising=False)
    return tmp_path


@pytest.fixture
def hot_files(store):
    """The three arms' hot files, written by the watcher's run_hot (sam/watch) with the mock provider."""
    d = config.OUT_DIR / "hot"
    d.mkdir(parents=True)
    for p in (FIXTURES / "hot").glob("*.json"):
        shutil.copy(p, d / p.name)
    return d


def save_round(*actions: Action) -> int:
    return memory.save_run(list(actions), account="_sgu_")


class FakeConnection:
    """Feeds one raw HTTP request to the handler and collects its reply; tests never open sockets."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.out = bytearray()

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.raw)

    def sendall(self, data):
        self.out.extend(data)


def request(method, path, body=None, headers=None):
    h = {"Host": f"127.0.0.1:{PORT}"}
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


def handoff(run_id):
    return json.loads((config.OUT_DIR / f"approved-run{run_id}.json").read_text(encoding="utf-8"))


def review(action_id, **body):
    return request("POST", f"/api/actions/{action_id}/review", body)


# ---------------------------------------------------------------- the server itself


@pytest.mark.parametrize(
    "headers, code",
    [
        ({"Host": "evil.example:80"}, 421),  # DNS rebinding: wrong Host
        ({"Content-Type": "text/plain"}, 403),  # a cross-site form can send text/plain without a preflight
        ({"Origin": "https://evil.example"}, 403),
    ],
)
def test_posts_from_other_sites_are_refused(store, headers, code):
    save_round(Action(type="post", text="a draft", mind="caption", narrative_id="n", id="a1"))
    got, res = request("POST", "/api/actions/a1/review", {"status": "approved"}, headers)
    assert got == code, res
    assert memory.run_actions(1)[0]["review"]["status"] == "pending"


def test_the_page_is_served_and_files_cannot_escape_dist(store, tmp_path, monkeypatch):
    import harness.review as review

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>app", encoding="utf-8")
    (dist / "assets" / "app-1a2b.js").write_text("js", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    monkeypatch.setattr(review, "DIST", dist.resolve())
    assert request("GET", "/")[1] == b"<!doctype html>app"
    assert request("GET", "/audit")[1] == b"<!doctype html>app"  # the app reads the view from the URL
    assert request("GET", "/assets/app-1a2b.js") == (200, b"js")
    assert request("GET", "/assets/../../secret.txt")[0] == 404
    assert request("GET", "/assets/%2e%2e/%2e%2e/secret.txt")[0] == 404


def test_unknown_api_route_is_404_and_wrong_method_405(store):
    assert request("GET", "/api/nope") == (404, {"error": "not found"})
    assert request("POST", "/api/queue", {}) == (405, {"error": "method not allowed"})


# ---------------------------------------------------------------- the queue


def test_kill_reasons_match_memorys_codes():
    assert set(queue.KILL_REASONS) == memory.REVIEW_REASONS


def test_hot_targets_come_first_newest_post_first_and_the_human_arm_has_no_draft(hot_files):
    save_round(Action(type="reply", text="round draft", mind="reply_lane", narrative_id="n",
                      target_url="https://x.com/WNBA/status/77", target_handle="WNBA", id="r1"))
    code, res = request("GET", "/api/queue")
    assert code == 200, res
    cards = res["cards"]
    assert [c["id"] for c in cards] == [f"hot-{STYLED}", f"hot-{CONTINUATION}", f"hot-{HUMAN}", "r1"]
    by_arm = {c["arm"]: c for c in cards if c["source"] == "hot"}
    assert by_arm["continuation"]["draft"] == "mock continuation alpha"
    assert by_arm["human"]["draft"] is None
    assert by_arm["styled"]["target"] == {"status_id": STYLED, "url": f"https://x.com/valkyries/status/{STYLED}",
                                          "author": "valkyries"}
    assert all(c["account"] == "_sgu_" for c in cards)
    assert res["problems"] == [] and res["limit"] == 280


def test_an_abstention_is_not_a_card_and_a_broken_hot_file_is_named(hot_files):
    rec = json.loads((hot_files / f"{STYLED}.json").read_text(encoding="utf-8"))
    rec.update(offer="abstain", draft=None, abstain_reason="mute_risk")
    (hot_files / f"{STYLED}.json").write_text(json.dumps(rec), encoding="utf-8")
    (hot_files / "123.json").write_text(json.dumps({"status_id": "123", "offer": "draft"}), encoding="utf-8")
    _, res = request("GET", "/api/queue")
    assert [c["id"] for c in res["cards"]] == [f"hot-{CONTINUATION}", f"hot-{HUMAN}"]
    assert res["problems"] == ["123.json has no url, arm, profile, finished_at; the watcher's run_hot writes them"]


def test_the_queue_leaves_out_vetoed_textless_old_and_decided_drafts(store):
    run_id = save_round(
        Action(type="post", text="keep me", mind="caption", narrative_id="n", target_text="Story headline", id="p1"),
        Action(type="post", text="vetoed", mind="caption", narrative_id="n", vetoed=True, id="p2"),
        Action(type="follow", text="", mind="community_builder", narrative_id="n", target_handle="x", id="f1"),
        Action(type="quote", text="decided", mind="caption", narrative_id="n", id="q1"),
    )
    memory.set_review("q1", "killed", reasons=["tone"])
    old = save_round(Action(type="post", text="yesterday's", mind="caption", narrative_id="n", id="o1"))
    with memory.connect() as conn:
        conn.execute("UPDATE runs SET started_at = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"), old))
    _, res = request("GET", "/api/queue")
    assert [(c["id"], c["run_id"], c["context"], c["target"]) for c in res["cards"]] == [("p1", run_id, "Story headline", None)]


def test_mock_rounds_are_shown_only_when_the_harness_runs_on_mock(store, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "mock")
    save_round(Action(type="reply", text="mock draft", mind="reply_lane", narrative_id="n",
                      target_url="https://x.com/sample_beatwriter/status/1", id="m1"))
    (card,) = request("GET", "/api/queue")[1]["cards"]
    assert card["target"]["author"] == "sample_beatwriter"  # named from the URL when the draft has no handle
    monkeypatch.setattr(config, "PROVIDER", "anthropic")
    save_round(Action(type="post", text="live draft", mind="caption", narrative_id="n", id="l1"))
    assert [c["id"] for c in request("GET", "/api/queue")[1]["cards"]] == ["l1"]


# ---------------------------------------------------------------- verdicts


def test_approving_a_hot_draft_makes_one_run_and_hands_it_off_under_the_ledgers_id(hot_files):
    aid = f"hot-{CONTINUATION}"
    assert review(aid, status="approved") == (200, {"ok": True, "export": str(config.OUT_DIR / "approved-run1.json")})
    out = handoff(1)
    assert out["account"] == "_sgu_"
    assert [(a["id"], a["type"], a["text"], a["target_url"]) for a in out["actions"]] == [
        (aid, "reply", "mock continuation alpha", f"https://x.com/AtlantaDream/status/{CONTINUATION}")]
    assert out["actions"][0]["draft_meta"]["arm"] == "continuation"
    review(aid, status="approved", text="two days. ballhalla better be loud")  # a second verdict, same run
    with memory.connect() as conn:
        runs = conn.execute("SELECT id, provider, sources FROM runs").fetchall()
        profile = conn.execute("SELECT profile FROM run_profiles WHERE run_id = 1").fetchone()[0]
    assert [tuple(r) for r in runs] == [(1, "mock", "watch")] and profile == "wnba"
    assert handoff(1)["actions"][0]["text"] == "two days. ballhalla better be loud"
    assert memory.profile_run_ids("wnba") == {1}  # learn reads it with the profile's rounds


def test_the_human_arm_needs_the_persons_words_before_it_can_be_approved(hot_files):
    aid = f"hot-{HUMAN}"
    code, res = review(aid, status="approved")
    assert code == 400 and res["error"] == "there is nothing to post yet: write the reply first"
    assert review(aid, status="approved", text="sun set, legacy didn't", note="A person's own take.")[0] == 200
    (a,) = handoff(1)["actions"]
    assert (a["text"], a["original_text"], a["mind"], a["review_note"]) == (
        "sun set, legacy didn't", "", "human", "A person's own take.")


def test_a_kill_keeps_its_reasons_and_undo_returns_the_card(hot_files):
    aid = f"hot-{STYLED}"
    assert review(aid, status="killed", reasons=["stale_story", "tone"], note="game's over")[0] == 200
    assert memory.review_history()[-1]["reasons"] == ["stale_story", "tone"]
    assert aid not in [c["id"] for c in request("GET", "/api/queue")[1]["cards"]]
    assert review(aid, status="pending")[0] == 200
    assert aid in [c["id"] for c in request("GET", "/api/queue")[1]["cards"]]


@pytest.mark.parametrize("body, needle", [
    ({"status": "killed", "reasons": ["made_up"]}, "reasons"),
    ({"status": "killed", "reasons": "slop"}, "reasons"),
    ({"status": "approved", "text": 5}, "text"),
    ({"status": "approved", "note": ["x"]}, "note"),
    ({"status": "maybe"}, "status must be one of"),
    ({"status": "approved", "text": "x" * 281}, "weighs 281"),
    ({"status": "approved", "text": "clark had [VERIFY: 12 assists]"}, "resolve [VERIFY: 12 assists]"),
])
def test_a_bad_verdict_is_refused_and_nothing_is_saved(hot_files, body, needle):
    code, res = review(f"hot-{CONTINUATION}", **body)
    assert code == 400 and needle in res["error"], res
    assert memory.review_history() == []


def test_a_verdict_on_an_unknown_action_is_404(hot_files):
    assert review("nope", status="killed")[0] == 404
    assert review("hot-999", status="killed") == (404, {"error": "unknown action hot-999"})


# ---------------------------------------------------------------- posts


# Computed with node: ((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, "")
TOKENS = {"1": "bhi2ay3f28n", "20": "6dq1a2xwd93", "943518274679132161": "2ac5ei57e8z",
          "2110052298747433341": "544xajhlvng", "1570786353264168962": "3t2rr1luhfj",
          "2040511285998313827": "4y2gevhhhm", "1683920951807971329": "42y6zv7ufp", "999999999999999999": "2f9lc2ug9mm"}


@pytest.mark.parametrize("sid", sorted(TOKENS))
def test_the_syndication_token_matches_react_tweets(sid):
    assert tweets.token(sid) == TOKENS[sid]


def http_404(url):
    raise FetchError(f"HTTP 404 from cdn.syndication.twimg.com for {url}") from urllib.error.HTTPError(url, 404, "nf", {}, None)


def offline(url):
    raise FetchError("URLError reaching cdn.syndication.twimg.com")


TWEET = {"__typename": "Tweet", "id_str": "5", "text": "hi", "user": {"screen_name": "WNBA"}}


def test_syndication_answers_are_cached_and_missing_posts_are_told_apart(store):
    calls = []
    assert tweets.syndication("5", get=lambda u: calls.append(u) or json.dumps(TWEET)) == {"result": "ok", "data": TWEET}
    assert "token=" + tweets.token("5") in calls[0]
    assert tweets.syndication("5", get=offline)["data"] == TWEET  # served from the cache, no request
    assert tweets.syndication("6", get=http_404)["result"] == "not_found"
    assert tweets.syndication("7", get=lambda u: "{}")["result"] == "not_found"
    assert tweets.syndication("8", get=lambda u: '{"__typename": "TweetTombstone"}')["result"] == "tombstone"
    with pytest.raises(FetchError):
        tweets.syndication("9", get=offline)


def test_an_expired_entry_is_refetched_and_kept_when_x_is_unreachable(store):
    tweets.syndication("5", get=lambda u: json.dumps(TWEET))
    path = tweets.CACHE_DIR / "5.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["fetched_at"] = (datetime.now(timezone.utc) - timedelta(seconds=tweets.FOUND_TTL_S + 5)).isoformat()
    path.write_text(json.dumps(rec), encoding="utf-8")
    newer = {**TWEET, "text": "edited"}
    assert tweets.syndication("5", get=offline)["data"] == TWEET  # stale beats nothing
    assert tweets.syndication("5", get=lambda u: json.dumps(newer))["data"] == newer


def test_a_missing_post_comes_with_the_harnesss_copy(hot_files, monkeypatch):
    monkeypatch.setattr(tweets, "syndication", lambda sid: {"result": "not_found", "data": None})
    code, res = request("GET", f"/api/tweet/{CONTINUATION}")
    assert code == 200 and res["data"] is None and res["missing"] == "not_found"
    s = res["stored"]
    assert (s["author"], s["status_id"], s["likes"], s["replies"]) == ("AtlantaDream", CONTINUATION, 40, 10)
    assert s["text"].startswith("We're only two days away from the 2026 WNBA Finals")
    assert s["created_at"] == "2026-10-16T00:09:15+00:00"


def test_a_rounds_target_is_found_in_its_trace(store, monkeypatch):
    monkeypatch.setattr(tweets, "syndication", lambda sid: (_ for _ in ()).throw(FetchError("offline")))
    run_id = save_round(Action(type="reply", text="d", mind="reply_lane", narrative_id="n",
                               target_url="https://x.com/WNBA/status/42", target_handle="WNBA",
                               target_text="tip-off", id="r1"))
    config.OUT_DIR.mkdir(exist_ok=True)
    (config.OUT_DIR / f"trace-run{run_id}.json").write_text(json.dumps({"narratives": [{"reply_targets": [
        {"url": "https://x.com/WNBA/status/42", "author": "WNBA", "text": "tip-off soon", "likes": 900,
         "replies": 31, "created_at": "2026-10-16T01:00:00+00:00", "author_followers": 951900}]}]}), encoding="utf-8")
    code, res = request("GET", "/api/tweet/42")
    assert code == 200 and res["missing"] == "unreachable"
    assert {k: res["stored"][k] for k in ("text", "likes", "replies", "author_followers")} == {
        "text": "tip-off soon", "likes": 900, "replies": 31, "author_followers": 951900}
    assert request("GET", "/api/tweet/43")[0] == 502  # unreachable, and no copy anywhere


def test_the_account_profile_falls_back_to_its_handle(store):
    assert tweets.account("_sgu_", get=offline) == {"handle": "_sgu_", "name": None, "avatar_url": None, "verified": False}
    body = json.dumps({"code": 200, "user": {"screen_name": "_sgu_", "name": "Sam", "avatar_url": "https://pbs/a.jpg",
                                              "verification": {"verified": False}}})
    assert tweets.account("_sgu_", get=lambda u: body)["name"] == "Sam"
    assert tweets.account("_sgu_", get=offline)["name"] == "Sam"  # cached for a day


# ---------------------------------------------------------------- history


# One row per shape `status_rows` returns (harness/outcomes/sync.py): a reply ranked in its thread (sam/watch),
# and a post with only counts (sam/platform, which has no rank key).
RANKED = {"action_id": "a1", "state": "posted", "url": "https://x.com/_sgu_/status/9", "posted_at": "2026-10-16T01:00:00+00:00",
          "latest": {"checkpoint": "+1h", "age_hours": 1.2, "likes": 4},
          "rank": {"checkpoint": "+1h", "age_hours": 1.3, "fetched_at": "2026-10-16T02:18:00+00:00", "rank": 3,
                   "likes_rank": 3, "page_replies": 35, "thread_replies": 41, "percentile": 0.95, "percentile_max": None,
                   "likes": 5, "followers": 2, "outcome": "success"}}
COUNTED = {"action_id": "a2", "state": "posted", "url": "https://x.com/_sgu_/status/8", "posted_at": "2026-10-16T01:00:00+00:00",
           "latest": {"checkpoint": "+6h", "age_hours": 6.5, "likes": 12}}


def test_an_outcome_reads_rank_likes_and_when_it_was_checked():
    assert history.outcome(RANKED) == {"state": "posted", "url": "https://x.com/_sgu_/status/9",
                                       "posted_at": "2026-10-16T01:00:00+00:00", "rank": 3, "page_replies": 35,
                                       "thread_replies": 41, "likes": 5, "checked_at": "2026-10-16T02:18:00+00:00"}
    assert history.outcome(COUNTED)["likes"] == 12 and history.outcome(COUNTED)["rank"] is None
    assert history.outcome(COUNTED)["checked_at"] == "2026-10-16T07:30:00+00:00"
    assert history.outcome({"action_id": "a3", "state": "waiting"})["checked_at"] is None
    assert history.outcome(None) is None


def test_history_lists_verdicts_newest_first_with_their_outcomes(store, monkeypatch, tmp_path):
    save_round(Action(type="reply", text="first", mind="reply_lane", narrative_id="n",
                      target_url="https://x.com/WNBA/status/42", target_handle="WNBA", id="a1"),
               Action(type="post", text="second", mind="caption", narrative_id="n", id="a2"))
    memory.set_review("a1", "approved", text="first, edited")
    memory.set_review("a2", "killed", reasons=["slop"])
    with memory.connect() as conn:
        conn.execute("UPDATE reviews SET reviewed_at = '2026-10-16T09:00:00+00:00' WHERE action_id = 'a1'")
    monkeypatch.setattr(history.ledger, "DEFAULT_PATH", tmp_path / "outcomes.db")
    code, res = request("GET", "/api/history")
    assert code == 200 and res["ledger"] is False
    assert [(i["id"], i["status"], i["text"], i["edited"], i["reasons"], i["outcome"]) for i in res["items"]] == [
        ("a2", "killed", "second", False, ["slop"], None), ("a1", "approved", "first, edited", True, [], None)]
    assert res["items"][1]["target"] == {"status_id": "42", "url": "https://x.com/WNBA/status/42", "author": "WNBA"}
    monkeypatch.setattr(history, "status_rows", lambda path: ({}, [RANKED]))
    (tmp_path / "outcomes.db").write_bytes(b"")
    res = request("GET", "/api/history")[1]
    assert res["ledger"] is True and res["items"][1]["outcome"]["rank"] == 3 and res["items"][0]["outcome"] is None


def test_history_is_empty_before_any_verdict(store):
    assert request("GET", "/api/history")[1]["items"] == []


# ---------------------------------------------------------------- audit


class FakeStore:
    """The label store contract's read and write calls (BRIEF-B2, "The label store contract")."""

    def __init__(self):
        self.human = []
        self.schema = {"version": 2, "fields": [
            {"name": "humor", "kind": "ordinal", "values": ["none", "some", "main_point"],
             "question": "How much does it rely on humor?", "applies_to": ["post", "reply"]}]}
        self.by_id = {
            "x:1": {"item_id": "x:1", "kind": "reply", "status_id": "1", "url": "https://x.com/a/status/1", "author": "a",
                    "author_followers": 10, "text": "ha", "created_at": "2026-10-01T00:00:00+00:00", "parent_id": "x:0",
                    "metrics": {"likes": 99}, "source": "reply_samples", "context": {}},
            "x:2": {"item_id": "x:2", "kind": "post", "status_id": "2", "url": "https://x.com/b/status/2", "author": "b",
                    "author_followers": 5, "text": "release", "created_at": "2026-10-02T00:00:00+00:00", "parent_id": None,
                    "metrics": {"likes": 3}, "source": "author_timelines", "context": {}},
        }

    def audit_ids(self):
        return ["x:1", "x:2"]

    def load_schema(self):
        return self.schema

    def queue(self, mode, limit=50, offset=0):
        assert mode == "audit"
        left = [i for i in self.audit_ids() if i not in {h["item_id"] for h in self.human}]
        return [{"item": self.by_id[i], "item_id": i} for i in left][offset: offset + limit]

    def items(self, ids):
        return [self.by_id[i] for i in ids]

    def append_human(self, item_id, labeler, labels, note=""):
        if item_id not in self.by_id:
            raise KeyError(item_id)
        if labels.get("humor") not in ("none", "some", "main_point"):
            raise ValueError("humor=? is not one of ['none', 'some', 'main_point']")
        row = {"item_id": item_id, "labeler": labeler, "labels": labels, "note": note, "at": "now"}
        self.human.append(row)
        return row

    def consensus(self, item_id):
        return {"humor": {"value": "some", "agreement": 1.0, "votes": {"claude-sonnet-5-5": "some", "gpt-6.1-sol": "some"},
                          "human": {"value": self.human[-1]["labels"]["humor"], "labeler": "sam", "at": "now"}}}


@pytest.fixture
def labels(store, monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(audit, "store", lambda: fake)
    monkeypatch.setenv("HARNESS_LABELER", "sam")
    return fake


def test_an_audit_item_comes_without_votes_or_counts(labels):
    code, res = request("GET", "/api/audit")
    assert code == 200, res
    assert (res["remaining"], res["total"], res["item"]["item_id"]) == (2, 2, "x:1")
    assert "metrics" not in res["item"] and "consensus" not in res and "votes" not in json.dumps(res)
    assert res["parent"] is None  # x:0 isn't an item; the reply still stands on its own
    assert request("GET", "/api/audit?offset=1")[1]["item"]["item_id"] == "x:2"  # skipping one
    assert request("GET", "/api/audit?offset=2")[1]["item"] is None
    assert request("GET", "/api/audit?offset=-1")[0] == 400


def test_an_answer_is_appended_and_then_the_votes_are_shown(labels):
    code, res = request("POST", "/api/audit/x:1", {"labels": {"humor": "main_point"}, "note": ""})
    assert code == 200, res
    assert labels.human == [{"item_id": "x:1", "labeler": "sam", "labels": {"humor": "main_point"}, "note": "", "at": "now"}]
    assert res["consensus"]["humor"]["votes"] == {"claude-sonnet-5-5": "some", "gpt-6.1-sol": "some"}
    assert request("GET", "/api/audit")[1]["item"]["item_id"] == "x:2"


@pytest.mark.parametrize("item, body, code", [
    ("x:1", {"labels": {}}, 400),
    ("x:1", {"labels": {"humor": "lots"}}, 400),
    ("x:9", {"labels": {"humor": "some"}}, 404),
])
def test_a_bad_answer_is_refused(labels, item, body, code):
    assert request("POST", f"/api/audit/{item}", body)[0] == code
    assert labels.human == []


def test_audit_says_when_the_label_store_is_missing(store, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_label_store(name, *args, **kwargs):
        if name == "harness.label.store":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_label_store)
    code, res = request("GET", "/api/audit")
    assert code == 503 and "sam/corpus-clean" in res["error"]


def test_audit_against_the_real_label_store(store, tmp_path, monkeypatch):
    """Runs once sam/corpus-clean's store is merged: the same contract, on files in the store's own format."""
    label_store = pytest.importorskip("harness.label.store")
    root = tmp_path / "labels"
    shutil.copytree(FIXTURES / "labels", root)
    monkeypatch.setattr(audit, "store", lambda: label_store.Store(root))
    monkeypatch.setenv("HARNESS_LABELER", "sam")
    res = request("GET", "/api/audit")[1]
    assert res["item"]["item_id"] == "x:101" and res["parent"]["item_id"] == "x:100"
    assert [f["name"] for f in res["schema"]["fields"]] == ["format", "humor"]
    code, res = request("POST", "/api/audit/x:101", {"labels": {"humor": "some"}})
    assert code == 200, res
    assert res["consensus"]["humor"]["votes"] == {"model-a": "some", "model-b": "none"}
    assert res["consensus"]["humor"]["human"]["value"] == "some"
    assert [json.loads(line)["labels"] for line in (root / "human.jsonl").read_text(encoding="utf-8").splitlines()] == [
        {"humor": "some"}]


# ---------------------------------------------------------------- status


def test_status_names_the_last_round_and_ignores_hot_runs(hot_files):
    save_round(Action(type="post", text="d", mind="caption", narrative_id="n", id="p1"))
    review(f"hot-{CONTINUATION}", status="approved")  # makes run 2, a hot target's
    s = request("GET", "/api/status")[1]
    assert s["last_round"]["id"] == 1 and s["watcher"] == "stopped" and s["account"] == "_sgu_"


def test_the_watcher_counts_as_running_only_when_its_pid_is_a_harness_watch(store):
    pid_file = config.OUT_DIR / "watch" / "watch.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")  # this pytest process isn't `harness watch`
    assert status.watcher() == "stopped"
    pid_file.write_text("not a pid", encoding="utf-8")
    assert status.watcher() == "stopped"


def test_hot_ids_round_trip():
    assert hot.status_id(hot.action_id("123")) == "123" and hot.status_id("a1") is None
