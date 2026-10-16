"""harness.outcomes: matching, checkpoints, labels, not_posted timing and rerun idempotency, all offline."""

import json
import sys
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness import config, memory
from harness.context import snowflake
from harness.outcomes import ledger
from harness.outcomes.cli import _positive
from harness.outcomes.handoff import ApprovedAction, HandleError, HandoffError, load_approved, resolve_handle
from harness.outcomes.label import BASELINE_MIN, BaselinePoint, baseline, checkpoint_for, label
from harness.outcomes.match import Match, match, normalize
from harness.outcomes.rank import FAILURE, INSUFFICIENT_THREAD, SUCCESS, UNRANKED, rank_in_thread
from harness.outcomes.reader import Counts, FxOutcomeReader, OwnPost, ReaderMissing, fx_reader, own_timeline
from harness.outcomes.sync import TRACK_FOR, status_rows, sync

T0 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
H = timedelta(hours=1)


def own(pid, created, text="hello world", kind="original", eng=(1, 0, 0, 0), reply_to=None, quoted=None):
    return OwnPost(pid, created, kind, text, reply_to, quoted, Counts("", *eng, views=100, author_followers=2))


def approved(aid, text, kind="post", at=T0, target=None, original=None):
    return ApprovedAction(aid, 1, kind, text, original, target, at, "approved-run1.json")


def pair(actions, posts):
    return match(actions, posts, TRACK_FOR)


def xpost(pid, created, text="", author="brand", reply_to=None, reply_status=None, quoted=None, repost=False,
          context=False, eng=(1, 0, 0, 0), fetched_at=""):
    """An XReader XPost (harness/context/xreader.py on PR #18), with the fields sync reads."""
    return SimpleNamespace(id=pid, text=text, author_handle=author, created_at=created, fetched_at=fetched_at,
                           likes=eng[0], reposts=eng[1], replies=eng[2], quotes=eng[3], views=100, author_followers=2,
                           replying_to=reply_to, replying_to_status=reply_status, quoted_id=quoted, is_repost=repost,
                           timeline_context=context)


class FakeReader:
    """Serves posts created by `now` as a with-replies feed, counts stamped at `now`. Counts its calls.

    `threads` maps another account's status id to its likes-ordered replies, and `parents` to its counts.
    """

    def __init__(self, posts=(), threads=None, parents=None):
        self.posts, self.now = list(posts), T0
        self.timeline_reads, self.status_reads, self.conversation_reads, self.status_calls = 0, 0, 0, []
        self.threads, self.parents = dict(threads or {}), dict(parents or {})
        self.gone: set[str] = set()  # deleted posts: status raises like the reader's FetchError

    def conversation(self, status_id):
        self.conversation_reads += 1
        return list(self.threads[status_id])

    def timeline(self, handle, limit):
        self.timeline_reads += 1
        stamp = self.now.isoformat()
        seen = sorted((p for p in self.posts if p.created_at <= self.now), key=lambda p: p.created_at, reverse=True)
        feed = [xpost(p.id, p.created_at, p.text, reply_to={"reply": "fan", "self_reply": "brand"}.get(p.kind),
                      reply_status=p.replying_to_status, quoted=p.quoted_id, fetched_at=stamp,
                      eng=(p.counts.likes, p.counts.reposts, p.counts.replies, p.counts.quotes))
                for p in seen[:limit]]
        return own_timeline(feed, handle, stamp)

    def status(self, status_id):
        self.status_reads += 1
        self.status_calls.append(status_id)
        if status_id in self.gone:
            raise RuntimeError(f"fxtwitter /status/{status_id} returned code 404: not found")
        if status_id in self.parents:
            return replace(self.parents[status_id], fetched_at=self.now.isoformat())
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
    assert normalize(raw, strip_mentions=True) == "clark's fire"
    assert normalize(raw) == "@fever @wnba clark's fire"


def test_reply_anchored_on_target_tolerates_a_rewrite_that_text_alone_would_reject():
    a = approved("r1", "AB will be back stronger, fever need her", kind="reply", target="111")
    rewrite = "AB back stronger than ever, the fever need her"
    on_target = own("p1", T0 + H, rewrite, kind="reply", reply_to="111")
    off_target = own("p2", T0 + H, rewrite, kind="reply", reply_to="999")
    assert [m.status_id for m in pair([a], [off_target])] == []
    [m] = pair([a], [off_target, on_target])
    assert (m.status_id, m.anchored) == ("p1", True)


def test_quote_anchors_on_quoted_status():
    a = approved("q1", "nice arena now add caitlin", kind="quote", target="222")
    [m] = pair([a], [own("p1", T0 + H, "nice arena, now add caitlin 👀", quoted="222")])
    assert m.anchored and m.score >= 0.9


def test_post_matches_small_edit_but_not_a_different_post():
    a = approved("a1", "get well soon AB. fever need you healthy for next season")
    edited = own("p1", T0 + H, "get well soon AB! the fever need you healthy next season 💛 https://t.co/x")
    other = own("p2", T0 + H, "WNBA Finals start Friday, who you got")
    [m] = pair([a], [other, edited])
    assert (m.status_id, m.anchored) == ("p1", False)
    assert pair([a], [other]) == []


def test_short_text_without_an_anchor_must_match_exactly():
    a = approved("a1", "This team 🫶")
    assert pair([a], [own("p1", T0 + H, "This team 💛")]) == []
    assert [m.status_id for m in pair([a], [own("p2", T0 + H, "this team 🫶")])] == ["p2"]


def test_post_made_before_approval_never_matches():
    a = approved("a1", "same words", at=T0)
    assert pair([a], [own("p1", T0 - H, "same words")]) == []


def test_each_post_goes_to_one_action_best_match_first():
    a1 = approved("a1", "clark to the 2k cover, finally")
    a2 = approved("a2", "clark on the 2k cover, finally!!")
    posts = [own("p1", T0 + H, "clark on the 2k cover, finally!!"), own("p2", T0 + 2 * H, "clark to the 2k cover, finally")]
    assert {(m.action_id, m.status_id) for m in pair([a1, a2], posts)} == {("a1", "p2"), ("a2", "p1")}


def test_mention_drafts_keep_their_handles():
    to_fan = approved("aaaa", "@hoopsfan22 welcome to the fever fam, glad you're here", kind="mention")
    to_beat = approved("ffff", "@iowabeat welcome to the fever fam, glad you're here", kind="mention")
    post = own("p1", T0 + H, "@hoopsfan22 welcome to the fever fam, glad you're here")
    assert [(m.action_id, m.status_id) for m in pair([to_fan, to_beat], [post])] == [("aaaa", "p1")]


def test_short_draft_with_a_verify_gap_matches_once_the_bot_fills_it():
    a = approved("a1", "[VERIFY: pts] pts for CC 🔥")
    assert [m.status_id for m in pair([a], [own("p1", T0 + H, "38 pts for CC 🔥")])] == ["p1"]
    assert pair([a], [own("p2", T0 + H, "38 pts for AB 🔥")]) == []


def test_post_more_than_a_week_after_approval_is_not_the_actions():
    a = approved("a1", "get well soon AB. fever need you healthy for next season")
    late = own("p1", T0 + TRACK_FOR + H, "get well soon AB. fever need you healthy for next season")
    assert pair([a], [late]) == []


def test_follow_is_never_matched():
    assert pair([approved("f1", "follow them", kind="follow")], [own("p1", T0 + H, "follow them")]) == []


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

def test_handle_comes_from_brand_x_handle_and_the_env_overrides_it(tmp_path, monkeypatch):
    toml = tmp_path / "observe.toml"
    toml.write_text('[brand]\nx_handle = ""\n', encoding="utf-8")
    monkeypatch.setenv("HARNESS_OBSERVE_CONFIG", str(toml))
    with pytest.raises(HandleError, match=r"\[brand\] x_handle is empty.*HARNESS_ACCOUNT_HANDLE.*--handle"):
        resolve_handle()
    assert resolve_handle("@IowaWBB") == ("IowaWBB", "--handle")
    toml.write_text('[brand]\nx_handle = "@_sgu_"\n', encoding="utf-8")
    assert resolve_handle() == ("_sgu_", f"{toml} [brand] x_handle")
    monkeypatch.setenv("HARNESS_ACCOUNT_HANDLE", "@otherfan")
    assert resolve_handle() == ("otherfan", "HARNESS_ACCOUNT_HANDLE")
    assert resolve_handle("IowaWBB") == ("IowaWBB", "--handle")


def test_handle_reports_a_missing_observe_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_OBSERVE_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(HandleError, match=r"observe.toml not found at .*missing.toml"):
        resolve_handle()


def test_follow_needs_no_text_and_a_reapproval_keeps_the_first_time(tmp_path):
    def write(name, run_id, approved_at, text):
        (tmp_path / name).write_text(json.dumps({"run_id": run_id, "actions": [
            {"id": "f1", "type": "follow", "text": "", "approved_at": T0.isoformat()},
            {"id": "a1", "type": "post", "text": text, "approved_at": approved_at}]}), encoding="utf-8")

    write("approved-run1.json", 1, (T0 + 3 * H).isoformat(), "edited later")
    write("approved-run0.json", 0, "2026-10-10T07:00:00-05:00", "first draft")  # T0, in Central time
    by_id = {a.id: a for a in load_approved(tmp_path)}
    assert by_id["f1"].text == ""
    assert (by_id["a1"].text, by_id["a1"].approved_at) == ("edited later", T0)


def test_approved_file_without_approved_at_is_an_error(tmp_path):
    (tmp_path / "approved-run3.json").write_text(json.dumps({"run_id": 3, "actions": [
        {"id": "a1", "type": "post", "text": "hi"}]}), encoding="utf-8")
    with pytest.raises(HandoffError, match="approved_at"):
        load_approved(tmp_path)


def test_missing_reader_names_pr_18(monkeypatch):
    monkeypatch.setitem(sys.modules, "harness.context", None)
    with pytest.raises(ReaderMissing, match="PR #18"):
        fx_reader()


def test_reader_older_than_with_replies_is_refused(monkeypatch):
    old = SimpleNamespace(FxTwitterReader=type("R", (), {"profile_statuses": lambda self, handle, limit=20: []}))
    monkeypatch.setitem(sys.modules, "harness.context", SimpleNamespace(xreader=old))
    with pytest.raises(ReaderMissing, match="bf396e5"):
        fx_reader()


def test_timeline_reads_the_replies_tab_and_keeps_only_the_accounts_own_posts():
    calls = []

    class Reader:
        def profile_statuses(self, handle, limit=20, with_replies=False):
            calls.append(with_replies)
            return [
                xpost("5", T0 + 5 * H, "@fan nice one", reply_to="fan", reply_status="9"),
                xpost("9", T0 - 100 * H, "a fan's post we answered", author="fan", context=True),
                xpost("4", T0 + 4 * H, "more on that", reply_to="brand", reply_status="1"),
                xpost("1", T0 - 300 * H, "our old post the thread continues"),  # shown as the parent of 4
                xpost("3", T0 + 3 * H, "reposted", author="other", repost=True),
                xpost("2", T0 + 2 * H, "plain post"),
                xpost("2", T0 + 2 * H, "plain post"),  # repeated on the next page
            ]

    tl = FxOutcomeReader(Reader()).timeline("brand", 20)
    assert calls == [True]
    assert [(p.id, p.kind) for p in tl.posts] == [("5", "reply"), ("4", "self_reply"), ("2", "original"),
                                                  ("1", "original")]
    # Post 1 is a thread parent from 300 hours back, so it says nothing about how far the feed reached.
    assert tl.reach == T0 + 2 * H


def test_empty_feed_reaches_nothing():
    tl = own_timeline([], "brand", T0.isoformat())
    assert (tl.reach, tl.fetched_at) == (None, T0.isoformat())


def test_cli_limit_must_be_positive():
    with pytest.raises(Exception, match="1 or more"):
        _positive("0")


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
    assert (reader.timeline_reads, reader.status_calls, first.recorded) == (1, [], 1)
    assert stored_outcomes()["a1"] == {"outcome": "posted", "status_id": "900", "url": "https://x.com/brand/status/900",
                                       "posted_at": (T0 + H).isoformat(), "approved_at": T0.isoformat(),
                                       "history": []}
    _, [row] = status_rows(env.ledger, now=T0 + 1.5 * H)
    assert (row["state"], row["latest"], row["next"]) == ("posted", None, "+1h in 0.5h")

    again = run(env, reader, T0 + 1.6 * H)
    assert (reader.timeline_reads, reader.status_calls, again.recorded) == (1, [], 0)
    assert again.timeline.startswith("not read: last read")

    run(env, reader, T0 + 2.2 * H)  # 1.2h old: +1h due, timeline due again after 30 min
    assert (reader.timeline_reads, reader.status_calls) == (2, ["900"])
    assert stored_outcomes()["a1"]["checkpoint"] == "+1h"

    repeat = run(env, reader, T0 + 2.3 * H)
    assert (reader.timeline_reads, reader.status_calls, repeat.recorded) == (2, ["900"], 0)

    _, [row] = status_rows(env.ledger, now=T0 + 2.3 * H)
    assert row["state"] == "posted" and row["latest"]["checkpoint"] == "+1h"
    assert row["label"]["outcome"] == "insufficient_baseline" and row["next"] == "+6h in 4.7h"

    run(env, reader, T0 + 7.5 * H)  # 6.5h old
    assert reader.status_calls == ["900", "900"]
    assert [h["checkpoint"] for h in stored_outcomes()["a1"]["history"]] == ["+1h", "+6h"]


def test_not_posted_waits_24h_and_a_read_that_reaches_the_approval(env):
    write_approved(env.out, [approved("a1", "the dream in four, calling it now", at=T0)])
    recent = [own(f"new{i}", T0 + (24 + i) * H, f"unrelated post {i}") for i in range(1, 3)]
    reader = FakeReader(recent + [own("old", T0 - H, "older unrelated post")])

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


def test_first_sync_after_a_week_still_finds_the_post(env):
    write_approved(env.out, [approved("a1", "clark headlines 2k27 season 2", at=T0)])
    reader = FakeReader([own("900", T0 + H, "clark headlines 2k27 season 2")])
    report = run(env, reader, T0 + 8 * 24 * H)
    assert [m.status_id for m in report.matched] == ["900"] and report.not_posted == []
    assert stored_outcomes()["a1"]["outcome"] == "posted"  # +48h's window closed with tracking


def test_a_deleted_post_stops_being_fetched_when_tracking_ends(env):
    write_approved(env.out, [approved("a1", "clark headlines 2k27 season 2", at=T0)])
    reader = FakeReader([own("900", T0 + H, "clark headlines 2k27 season 2")])
    run(env, reader, T0 + 2.5 * H)
    reader.gone.add("900")
    assert run(env, reader, T0 + 8 * H).errors  # +6h is due and the fetch fails, loudly
    assert run(env, reader, T0 + 60 * H).errors  # +48h
    calls = len(reader.status_calls)
    assert run(env, reader, T0 + TRACK_FOR + 2 * H).errors == []
    assert len(reader.status_calls) == calls
    _, [row] = status_rows(env.ledger, now=T0 + TRACK_FOR + 2 * H)
    assert row["missed"] == ["+6h", "+24h", "+48h"] and row["next"] == ""
    assert "404" in row["error"]


def test_an_empty_feed_never_marks_anything_not_posted(env):
    write_approved(env.out, [approved("a1", "the dream in four, calling it now", at=T0)])
    report = run(env, FakeReader(), T0 + 30 * H)
    assert report.not_posted == []
    assert status_rows(env.ledger, now=T0 + 30 * H)[1][0]["state"] == "unverified"


def test_a_resaved_review_keeps_the_first_approval_time(env):
    write_approved(env.out, [approved("a1", "clark headlines 2k27 season 2", at=T0)])
    reader = FakeReader([own("900", T0 + H, "clark headlines 2k27 season 2")])
    run(env, FakeReader(), T0 + 0.5 * H)
    write_approved(env.out, [approved("a1", "clark headlines 2k27 season 2", at=T0 + 3 * H)])  # re-saved
    assert [m.status_id for m in run(env, reader, T0 + 4 * H).matched] == ["900"]


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


# ---------------------------------------------------------------------------- rank_in_thread and hot targets

PARENT = str(snowflake.floor_id(T0))  # another account's post, made at T0
MIN = timedelta(minutes=1)


def on_page(rid, likes, parent=PARENT, followers=100):
    """One reply on the parent's likes-ordered conversation page, as xreader returns it."""
    return SimpleNamespace(id=rid, replying_to_status=parent, likes=likes, author_followers=followers)


def made_at(t: datetime) -> str:
    return str(snowflake.floor_id(t))


def test_rank_is_the_position_on_the_likes_page_and_its_percentile_of_the_parents_replies():
    page = [on_page(f"r{i}", 50 - i) for i in range(10)]
    r = rank_in_thread(page, PARENT, "r2", parent_replies=40)
    assert (r.rank, r.likes_rank, r.page_replies, r.thread_replies, r.likes) == (3, 3, 10, 40, 48)
    assert (r.percentile, r.percentile_max, r.outcome) == (0.95, None, SUCCESS)  # 1 - 2/40
    r = rank_in_thread(page, PARENT, "r9", parent_replies=12)
    assert (r.percentile, r.outcome) == (0.25, FAILURE)  # 1 - 9/12


def test_likes_rank_counts_replies_with_more_likes_whatever_order_x_shows():
    r = rank_in_thread([on_page("a", 5), on_page("b", 9), on_page("c", 5)], PARENT, "c", parent_replies=6)
    assert (r.rank, r.likes_rank) == (3, 2)


def test_a_reply_off_the_page_gets_only_an_upper_bound_on_its_percentile():
    page = [on_page(f"r{i}", 50 - i) for i in range(35)]
    r = rank_in_thread(page, PARENT, "ours", parent_replies=40)
    assert (r.rank, r.percentile, r.percentile_max, r.outcome) == (None, None, 0.125, FAILURE)  # 1 - 35/40
    assert rank_in_thread(page, PARENT, "ours", parent_replies=200).outcome == UNRANKED  # at most 0.825
    # Every other reply is on the page and ours isn't, so X hid it: it ranks last.
    r = rank_in_thread(page[:6], PARENT, "ours", parent_replies=6)
    assert (r.thread_replies, r.percentile_max, r.outcome) == (7, round(1 - 6 / 7, 4), FAILURE)


def test_a_small_thread_is_insufficient_and_only_direct_replies_count():
    page = [on_page("r0", 3), on_page("nested", 99, parent="r0"), on_page("r1", 1, parent=None)]
    r = rank_in_thread(page, PARENT, "r1", parent_replies=3)
    assert (r.rank, r.page_replies, r.outcome) == (2, 2, INSUFFICIENT_THREAD)  # a dropped parent link counts as direct


def write_hot(out, status_id, arm="human", offer="human_prompt", draft=None, finished=T0 + MIN):
    hot_dir = out / "hot"
    hot_dir.mkdir(exist_ok=True)
    record = {"status_id": status_id, "offer": offer, "arm": arm, "profile": "wnba", "draft": draft,
              "finished_at": finished.isoformat(), "method": None if arm == "human" else arm}
    (hot_dir / f"{status_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return hot_dir


def hot_sync(env, reader, now, hot_dir):
    reader.now = now
    return sync("brand", env.out, env.ledger, reader_factory=lambda: reader, now=now, hot_dir=hot_dir)


def thread_page(ours_id, ours_likes, others):
    """`others` replies with descending likes, ours placed by its likes, as X's likes order would."""
    page = [on_page(str(int(made_at(T0 + (i + 1) * MIN)) + 1), 40 - 3 * i, followers=10) for i in range(others)]
    page.append(on_page(ours_id, ours_likes, followers=2))
    return sorted(page, key=lambda r: -r.likes)


def test_a_hot_target_matches_the_reply_link_alone_and_is_ranked_at_1h_and_24h(env):
    hot_dir = write_hot(env.out, PARENT)
    ours_id, decoy_id = made_at(T0 + 12 * MIN), made_at(T0 + 13 * MIN)
    ours = own(ours_id, snowflake.to_datetime(ours_id), "@WNBA words of my own", kind="reply", reply_to=PARENT)
    decoy = own(decoy_id, snowflake.to_datetime(decoy_id), "@WNBA words of my own", kind="reply", reply_to="999")
    reader = FakeReader([ours, decoy], threads={PARENT: thread_page(ours_id, 32, 9)},
                        parents={PARENT: Counts("", 0, 0, 20, 0)})

    first = hot_sync(env, reader, T0 + 20 * MIN, hot_dir)
    assert first.hot_targets == 1 and first.matched == [Match(f"hot-{PARENT}", ours_id, 0.0, True)]
    with closing(ledger.connect(env.ledger)) as conn:
        rm = dict(ledger.reply_match(conn, f"hot-{PARENT}"))
    assert (rm["arm"], rm["minutes_after_parent"], rm["followers_at_match"]) == ("human", 12.0, 2)
    assert first.ranks == [] and reader.conversation_reads == 0  # under an hour old: nothing due

    ours_at = snowflake.to_datetime(ours_id)
    at_1h = hot_sync(env, reader, ours_at + 1.5 * H, hot_dir)
    assert at_1h.ranks == [f"hot-{PARENT} +1h: rank 4 of 20, success"]  # 1 - 3/20 = 0.85
    assert reader.conversation_reads == 1
    hot_sync(env, reader, ours_at + 1.6 * H, hot_dir)
    assert reader.conversation_reads == 1  # +1h is ranked once
    reader.threads[PARENT] = thread_page(ours_id, 1, 12)  # by +24h it has sunk
    at_24h = hot_sync(env, reader, ours_at + 25 * H, hot_dir)
    assert at_24h.ranks == [f"hot-{PARENT} +24h: rank 13 of 20, failure"]  # 1 - 12/20 = 0.4

    assert f"hot-{PARENT}" not in stored_outcomes()  # a hot target has no row in memory's actions table
    _, rows_ = status_rows(env.ledger, now=ours_at + 25 * H)
    (row,) = [r for r in rows_ if r["action_id"] == f"hot-{PARENT}"]
    assert (row["arm"], row["state"], row["label"]["outcome"], row["label"]["basis"]) == (
        "human", "posted", FAILURE, "rank_in_thread")
    assert (row["rank"]["checkpoint"], row["rank"]["rank"], row["rank"]["percentile"]) == ("+24h", 13, 0.4)
    assert row["reply"] == {"parent_status": PARENT, "minutes_after_parent": 12.0, "followers_at_match": 2}


def test_an_abstained_hot_file_offers_nothing_to_match(env):
    hot_dir = write_hot(env.out, PARENT, offer="abstain")
    ours_id = made_at(T0 + 5 * MIN)
    reader = FakeReader([own(ours_id, snowflake.to_datetime(ours_id), "@WNBA hi", kind="reply", reply_to=PARENT)])
    report = hot_sync(env, reader, T0 + 20 * MIN, hot_dir)
    assert (report.hot_targets, report.matched) == (0, [])


def test_a_hot_file_missing_its_arm_fails_loudly(env):
    hot_dir = env.out / "hot"
    hot_dir.mkdir()
    (hot_dir / f"{PARENT}.json").write_text(json.dumps({"status_id": PARENT, "offer": "draft"}), encoding="utf-8")
    with pytest.raises(HandoffError, match="has no arm, profile, finished_at"):
        hot_sync(env, FakeReader(), T0, hot_dir)


def test_an_approved_reply_is_labeled_by_its_rank_never_the_accounts_median(env):
    write_approved(env.out, [approved("a1", "the fever bench was loud tonight", kind="reply", at=T0, target=PARENT)])
    ours_id = made_at(T0 + 5 * MIN)
    ours = own(ours_id, snowflake.to_datetime(ours_id), "@WNBA the fever bench was loud tonight", kind="reply",
               reply_to=PARENT)
    earlier = [own(made_at(T0 - (i + 2) * H), T0 - (i + 2) * H, f"older reply {i}", kind="reply", reply_to="555",
                   eng=(100, 0, 0, 0)) for i in range(12)]
    reader = FakeReader([ours] + earlier, threads={PARENT: thread_page(ours_id, 99, 9)},
                        parents={PARENT: Counts("", 0, 0, 10, 0)})
    hot_sync(env, reader, snowflake.to_datetime(ours_id) + 1.5 * H, env.out / "hot")
    out = stored_outcomes()["a1"]
    assert (out["outcome"], out["label_basis"], out["arm"]) == (SUCCESS, "rank_in_thread", None)
    assert out["rank_in_thread"][0]["rank"] == 1 and out["minutes_after_parent"] == 5.0
    with closing(ledger.connect(env.ledger)) as conn:
        assert ledger.labels(conn, "a1") == {}
