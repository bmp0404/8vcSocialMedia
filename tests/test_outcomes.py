"""harness.outcomes: matching, checkpoints, labels, not_posted timing and rerun idempotency, all offline."""

import json
import sys
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness import config, memory
from harness.outcomes import ledger
from harness.outcomes.handoff import ApprovedAction, HandleError, HandoffError, load_approved, resolve_handle
from harness.outcomes.label import BASELINE_MIN, BaselinePoint, baseline, checkpoint_for, label
from harness.outcomes.match import match, normalize
from harness.outcomes.reader import Counts, FxOutcomeReader, OwnPost, ReaderMissing, Timeline, fx_reader
from harness.outcomes.sync import status_rows, sync

T0 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
H = timedelta(hours=1)


def own(pid, created, text="hello world", kind="original", eng=(1, 0, 0, 0), reply_to=None, quoted=None):
    return OwnPost(pid, created, kind, text, reply_to, quoted, Counts("", *eng, views=100, author_followers=2))


def approved(aid, text, kind="post", at=T0, target=None, original=None):
    return ApprovedAction(aid, 1, kind, text, original, target, at, "approved-run1.json")


class FakeReader:
    """Serves posts created by `now`, with their counts stamped at `now`. Counts every request."""

    def __init__(self, posts=(), exhausted=True):
        self.posts, self.exhausted, self.now = list(posts), exhausted, T0
        self.timeline_calls, self.status_calls = 0, []
        self.requests = 0

    def timeline(self, handle, limit):
        self.timeline_calls += 1
        self.requests += 1
        stamp = self.now.isoformat()
        seen = sorted((p for p in self.posts if p.created_at <= self.now), key=lambda p: p.created_at, reverse=True)
        return Timeline([replace(p, counts=replace(p.counts, fetched_at=stamp)) for p in seen[:limit]], stamp,
                        self.exhausted, 1)

    def status(self, status_id):
        self.status_calls.append(status_id)
        self.requests += 1
        p = next(p for p in self.posts if p.id == status_id)
        return replace(p.counts, fetched_at=self.now.isoformat())


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "harness.db")
    out = tmp_path / "out"
    out.mkdir()
    return SimpleNamespace(out=out, ledger=tmp_path / "outcomes.db", tmp=tmp_path)


def write_approved(out, actions, run_id=1):
    rows = [{"id": a.id, "type": a.type, "text": a.text, "approved_at": a.approved_at.isoformat(),
             "target_url": f"https://x.com/someone/status/{a.target_status}" if a.target_status else None}
            for a in actions]
    (out / f"approved-run{run_id}.json").write_text(json.dumps({"run_id": run_id, "actions": rows}), encoding="utf-8")


def run(env, reader, now):
    reader.now = now
    return sync("brand", env.out, env.ledger, reader_factory=lambda: reader, now=now)


def stored_outcomes():
    with closing(memory.connect()) as conn:
        return {r["action_id"]: json.loads(r["metrics"]) for r in conn.execute("SELECT action_id, metrics FROM outcomes")}


def history_posts(n, start, eng_of=lambda i: (i, 0, 0, 0)):
    """n earlier originals, one per 6 hours going back from `start`."""
    return [own(f"old{i}", start - i * 6 * H, f"older post number {i}", eng=eng_of(i)) for i in range(1, n + 1)]


# ---------------------------------------------------------------------------------------- matching

def test_normalize_drops_links_verify_gaps_reply_prefix_and_case():
    raw = "@Fever @WNBA  Clark’s [VERIFY: 3-pt %] FIRE https://t.co/abc  "
    assert normalize(raw) == "clark's fire"


def test_reply_anchored_on_target_tolerates_a_rewrite_that_text_alone_would_reject():
    a = approved("r1", "AB will be back stronger, fever need her", kind="reply", target="111")
    rewrite = "AB back stronger than ever, the fever need her"
    on_target = own("p1", T0 + H, rewrite, kind="reply", reply_to="111")
    off_target = own("p2", T0 + H, rewrite, kind="reply", reply_to="999")
    assert [m.status_id for m in match([a], [off_target])] == []
    [m] = match([a], [off_target, on_target])
    assert (m.status_id, m.anchored) == ("p1", True)


def test_quote_anchors_on_quoted_status():
    a = approved("q1", "nice arena now add caitlin", kind="quote", target="222")
    [m] = match([a], [own("p1", T0 + H, "nice arena, now add caitlin 👀", quoted="222")])
    assert m.anchored and m.score >= 0.9


def test_post_matches_small_edit_but_not_a_different_post():
    a = approved("a1", "get well soon AB. fever need you healthy for next season")
    edited = own("p1", T0 + H, "get well soon AB! the fever need you healthy next season 💛 https://t.co/x")
    other = own("p2", T0 + H, "WNBA Finals start Friday, who you got")
    [m] = match([a], [other, edited])
    assert (m.status_id, m.anchored) == ("p1", False)
    assert match([a], [other]) == []


def test_short_text_without_an_anchor_must_match_exactly():
    a = approved("a1", "This team 🫶")
    assert match([a], [own("p1", T0 + H, "This team 💛")]) == []
    assert [m.status_id for m in match([a], [own("p2", T0 + H, "this team 🫶")])] == ["p2"]


def test_post_made_before_approval_never_matches():
    a = approved("a1", "same words", at=T0)
    assert match([a], [own("p1", T0 - H, "same words")]) == []


def test_each_post_goes_to_one_action_best_match_first():
    a1 = approved("a1", "clark to the 2k cover, finally")
    a2 = approved("a2", "clark on the 2k cover, finally!!")
    posts = [own("p1", T0 + H, "clark on the 2k cover, finally!!"), own("p2", T0 + 2 * H, "clark to the 2k cover, finally")]
    assert {(m.action_id, m.status_id) for m in match([a1, a2], posts)} == {("a1", "p2"), ("a2", "p1")}


def test_follow_is_never_matched():
    assert match([approved("f1", "follow them", kind="follow")], [own("p1", T0 + H, "follow them")]) == []


# ---------------------------------------------------------------------------------------- labels

@pytest.mark.parametrize("age,cp", [(0.5, None), (1, 1), (1.99, 1), (2, None), (5.9, None), (6, 6), (11.9, 6),
                                    (12, None), (24, 24), (47.9, 24), (48, 48), (500, 48)])
def test_checkpoint_windows(age, cp):
    assert checkpoint_for(age) == cp


def test_label_needs_ten_earlier_posts():
    assert label(5, [1] * (BASELINE_MIN - 1)).outcome == "insufficient_baseline"
    assert label(5, [1] * BASELINE_MIN).outcome == "success"


def test_label_at_the_median_is_failure_above_is_success():
    values = list(range(1, 12))  # median 6
    assert label(6, values).outcome == "failure"
    lab = label(7, values)
    assert (lab.outcome, lab.baseline_median, lab.baseline_n) == ("success", 6.0, 11)


def test_baseline_takes_earlier_posts_of_the_same_group_only():
    pts = [BaselinePoint("early", T0 - H, "original", 1), BaselinePoint("later", T0 + H, "original", 2),
           BaselinePoint("self", T0, "original", 3), BaselinePoint("rep", T0 - H, "reply", 4),
           BaselinePoint("thread", T0 - H, "self_reply", 5)]
    assert baseline(pts, "self", T0, "original") == [1]
    assert baseline(pts, "x", T0, "reply") == [4]
    assert baseline(pts, "x", T0, "self_reply") == []


# ---------------------------------------------------------------------------------------- inputs

def test_handle_refuses_placeholder_and_names_both_keys(tmp_path, monkeypatch):
    toml, account = tmp_path / "observe.toml", tmp_path / "account.json"
    toml.write_text('[brand]\nx_handle = ""\n', encoding="utf-8")
    account.write_text('{"handle": "clarkfanaccount"}', encoding="utf-8")
    monkeypatch.setenv("HARNESS_OBSERVE_CONFIG", str(toml))
    monkeypatch.setenv("HARNESS_ACCOUNT", str(account))
    with pytest.raises(HandleError) as e:
        resolve_handle()
    assert "[brand] x_handle" in str(e.value) and '"handle"' in str(e.value)
    assert resolve_handle("@IowaWBB") == ("IowaWBB", "--handle")
    account.write_text('{"handle": "realfan"}', encoding="utf-8")
    assert resolve_handle()[0] == "realfan"
    toml.write_text('[brand]\nx_handle = "otherfan"\n', encoding="utf-8")
    with pytest.raises(HandleError, match="both must name"):
        resolve_handle()


def test_approved_file_without_approved_at_is_an_error(tmp_path):
    (tmp_path / "approved-run3.json").write_text(json.dumps({"run_id": 3, "actions": [
        {"id": "a1", "type": "post", "text": "hi"}]}), encoding="utf-8")
    with pytest.raises(HandoffError, match="approved_at"):
        load_approved(tmp_path)


def test_missing_reader_names_pr_18(monkeypatch):
    monkeypatch.setitem(sys.modules, "harness.context", None)
    with pytest.raises(ReaderMissing, match="PR #18"):
        fx_reader()


def test_fx_timeline_keeps_own_posts_and_spots_replies_fxtwitter_left_unmarked():
    def raw(pid, author, text, reply=None, start=0, reposted_by=None):
        return {"id": pid, "author": author, "text": text, "reply": reply, "reposted_by": reposted_by,
                "raw_text": {"display_text_range": [start, len(text)]}}

    page = {"code": 200, "results": [
        raw("1", "brand", "plain post"),
        raw("2", "brand", "@fan it is", start=5),  # a reply fxtwitter returned without replying_to
        raw("3", "brand", "@CaitlinClark22 nice", reply=("CaitlinClark22", "7"), start=16),
        raw("4", "brand", "more", reply=("brand", "1")),
        raw("7", "CaitlinClark22", "the post brand replied to"),
        raw("8", "brand", "a repost", reposted_by="brand"),
    ], "cursor": {"bottom": None}}

    def parse(r, fetched_at):
        to, status = r["reply"] or (None, None)
        return SimpleNamespace(id=r["id"], text=r["text"], author_handle=r["author"], created_at=T0, fetched_at=fetched_at,
                               likes=0, reposts=0, replies=0, quotes=0, views=None, author_followers=None,
                               replying_to=to, replying_to_status=status, quoted_id=None,
                               is_repost=bool(r["reposted_by"]))

    paths = []
    fake_reader = SimpleNamespace(_get=lambda path, max_age: paths.append(path) or (page, T0.isoformat()))
    xreader = SimpleNamespace(FxTwitterReader=None, _parse=parse)
    tl = FxOutcomeReader(xreader, reader=fake_reader).timeline("brand", 20)
    assert paths == ["/2/profile/brand/statuses?with_replies=1"]
    assert {p.id: p.kind for p in tl.posts} == {"1": "original", "2": "reply", "3": "reply", "4": "self_reply"}
    assert tl.exhausted


# ---------------------------------------------------------------------------------------- sync

def test_sync_matches_snapshots_labels_and_records(env):
    a = approved("a1", "get well soon AB. fever need you healthy", at=T0)
    write_approved(env.out, [a])
    ours = own("900", T0 + H, "get well soon AB! fever need you healthy", eng=(30, 2, 1, 0))
    reader = FakeReader([ours] + history_posts(12, T0))
    report = run(env, reader, T0 + 50 * H)  # ours is 49h old: the +48h checkpoint is due

    assert [(m.action_id, m.status_id) for m in report.matched] == [("a1", "900")]
    assert reader.status_calls == ["900"] and report.snapshots == ["a1 +48h"]
    out = stored_outcomes()["a1"]
    assert out["outcome"] == "success" and out["checkpoint"] == "+48h"
    assert (out["likes"], out["reposts"], out["replies"], out["quotes"], out["engagement"]) == (30, 2, 1, 0, 33)
    assert (out["views"], out["author_followers"], out["status_id"]) == (100, 2, "900")
    assert out["baseline_n"] == 12 and out["baseline_median"] == 6.5
    assert [h["checkpoint"] for h in out["history"]] == ["+48h"]


def test_rerun_fetches_only_what_is_due(env):
    write_approved(env.out, [approved("a1", "clark headlines 2k27 season 2", at=T0)])
    reader = FakeReader([own("900", T0 + H, "clark headlines 2k27 season 2")])

    first = run(env, reader, T0 + 1.5 * H)  # ours is 0.5h old: found, no checkpoint yet
    assert (reader.timeline_calls, reader.status_calls, first.recorded) == (1, [], 1)
    assert stored_outcomes()["a1"] == {"outcome": "posted", "status_id": "900", "url": "https://x.com/brand/status/900",
                                       "posted_at": (T0 + H).isoformat(), "approved_at": T0.isoformat(),
                                       "history": []}
    _, [row] = status_rows(env.ledger, now=T0 + 1.5 * H)
    assert (row["state"], row["latest"], row["next"]) == ("posted", None, "+1h in 0.5h")

    again = run(env, reader, T0 + 1.6 * H)
    assert (reader.timeline_calls, reader.status_calls, again.recorded) == (1, [], 0)
    assert again.timeline.startswith("not read: last read")

    run(env, reader, T0 + 2.2 * H)  # 1.2h old: +1h due, timeline due again after 30 min
    assert (reader.timeline_calls, reader.status_calls) == (2, ["900"])
    assert stored_outcomes()["a1"]["checkpoint"] == "+1h"

    repeat = run(env, reader, T0 + 2.3 * H)
    assert (reader.timeline_calls, reader.status_calls, repeat.recorded) == (2, ["900"], 0)

    _, [row] = status_rows(env.ledger, now=T0 + 2.3 * H)
    assert row["state"] == "posted" and row["latest"]["checkpoint"] == "+1h"
    assert row["label"]["outcome"] == "insufficient_baseline" and row["next"] == "+6h in 4.7h"

    run(env, reader, T0 + 7.5 * H)  # 6.5h old
    assert reader.status_calls == ["900", "900"]
    assert [h["checkpoint"] for h in stored_outcomes()["a1"]["history"]] == ["+1h", "+6h"]


def test_not_posted_waits_24h_and_a_read_that_reaches_the_approval(env):
    write_approved(env.out, [approved("a1", "the dream in four, calling it now", at=T0)])
    recent = [own(f"new{i}", T0 + (24 + i) * H, f"unrelated post {i}") for i in range(1, 3)]
    reader = FakeReader(recent + [own("old", T0 - H, "older unrelated post")], exhausted=False)

    run(env, reader, T0 + 23 * H)
    assert "a1" not in stored_outcomes()

    reader.posts = recent  # this read stops at T0 + 25h, after the approval
    report = run(env, reader, T0 + 26 * H)
    assert report.not_posted == [] and "a1" not in stored_outcomes()
    assert status_rows(env.ledger, now=T0 + 26 * H)[1][0]["state"] == "unverified"

    reader.posts = recent + [own("old", T0 - H, "older unrelated post")]
    report = run(env, reader, T0 + 27 * H)
    assert report.not_posted == ["a1"]
    assert stored_outcomes()["a1"]["outcome"] == "not_posted"

    reader.posts.append(own("late", T0 + 28 * H, "the dream in four. calling it now"))  # the bot posted late
    run(env, reader, T0 + 28.5 * H)
    assert stored_outcomes()["a1"]["outcome"] == "posted"
    run(env, reader, T0 + 29.5 * H)
    out = stored_outcomes()["a1"]
    assert (out["outcome"], out["status_id"], out["checkpoint"]) == ("insufficient_baseline", "late", "+1h")


def test_deleted_outcomes_table_is_refilled_on_the_next_sync(env):
    write_approved(env.out, [approved("a1", "clark headlines 2k27 season 2", at=T0)])
    reader = FakeReader([own("900", T0 + H, "clark headlines 2k27 season 2")])
    run(env, reader, T0 + 2.5 * H)
    config.DB_PATH.unlink()
    assert run(env, reader, T0 + 2.6 * H).recorded == 1
    assert reader.status_calls == ["900"]


def test_ledger_refuses_a_second_account(env):
    write_approved(env.out, [])
    run(env, FakeReader(), T0)
    with pytest.raises(ledger.LedgerHandleError, match="--ledger"):
        sync("otherbrand", env.out, env.ledger, reader_factory=FakeReader, now=T0)
