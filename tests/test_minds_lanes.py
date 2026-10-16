import json
import re
from datetime import datetime, timedelta

import pytest

from harness import config
from harness.llm import LLM, MockLLM
from harness.minds import brand, decide, lanes, writer
from harness.minds.settings import ActionConfig
from harness.models import Action, Narrative

ESPN = "https://www.espn.com/wnba/story/_/id/1/boston-surgery"
X_URL = "https://x.com/AtlantaDream/status/2110808408366493714"
X_URL2 = "https://x.com/WNBA/status/2110808211347456506"
REPLIES = [{"text": t, "likes": 10} for t in (
    "im buying several tickets and figuring out who comes later", "there you go hawks you did the thing",
    "all the fans complaining better show up now", "good now the internet can stop whining",
    "tell the benz to let us use it for two games", "state farm about to be loud for real",
)]


@pytest.fixture(autouse=True)
def tmp_out(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    lanes.PENDING.clear()
    return tmp_path


def narrative(targets=None, signal_urls=None, nid="finals") -> Narrative:
    n = Narrative(id=nid, title="Finals tip off Saturday", summary="Dream at Valkyries, Game 1 Oct 17",
                  relevance=0.9, velocity=0.8, signal_urls=signal_urls or [])
    if targets is not None:
        n.reply_targets = targets  # Observe's field, set duck-typed until sam/observe merges
    return n


def target(url=X_URL, author="AtlantaDream", age=10.0, eph=500.0, replies=REPLIES, text="ATL, show up for Game 3 at State Farm Arena"):
    # Observe's shape: `thread` holds the replies, `replies` is the count.
    return {"url": url, "author": author, "text": text, "age_minutes": age, "engagement_per_hour": eph,
            "thread": replies, "replies": len(replies)}


class Routed(LLM):
    """Answers by which prompt it gets: plan, styled replies, captions or the veto judge."""

    def __init__(self, plan=None, styled=None, captions=None, veto=None):
        self.plan, self.styled, self.captions, self.veto = plan, styled, captions, veto
        self.calls = []

    def complete_json(self, system, prompt, mock=None):
        if "decide whether the account should reply" in system:
            self.calls.append("plan")
            return self.plan(prompt) if self.plan else mock()
        if "screen candidate" in system:
            self.calls.append("veto")
            cands = re.findall(r"^\d+\. (.*)$", prompt.split("CANDIDATES:")[1], re.M)
            return {"verdicts": [self.veto(c) for c in cands]} if self.veto else mock()
        if "caption posts" in system:
            self.calls.append("captions")
            return {"captions": self.captions} if self.captions is not None else mock()
        self.calls.append("styled")
        return {"replies": self.styled} if self.styled is not None else mock()


def ok(_c):
    return {"p_ai": 10, "unsupported": False, "off_mission": False, "risk": False, "reason": "fine"}


def run(llm, narratives, chorus=(), takes=(), cfg=None, budget=None):
    cfg = cfg or ActionConfig()
    return lanes.run(llm, "mission", "lessons", narratives, list(chorus), cfg=cfg,
                     budget=budget or lanes.Budget(cfg), takes=list(takes), fetch=lambda url: [])


def by_lane(decisions, lane):
    return [d for d in decisions if d.lane == lane]


# ---- the bug PR #6 left on main: replies aimed at news links


def test_mock_decide_never_replies_to_signal_urls():
    actions = decide(MockLLM(), "mission", "lessons", [narrative(signal_urls=[ESPN, X_URL])], per_mind=1)
    assert not [a for a in actions if a.type == "reply"]
    assert [a.target_url for a in actions if a.type == "quote"] == [X_URL]  # quoting a cited X post is fine
    assert (lanes.PENDING[0].lane, lanes.PENDING[0].tool, lanes.PENDING[0].reason) == ("reply", "abstain", "no_target")


def test_every_reply_targets_an_offered_url():
    actions, _ = run(MockLLM(), [narrative([target()], signal_urls=[ESPN])])
    replies = [a for a in actions if a.type == "reply"]
    assert replies and all(a.target_url == X_URL for a in replies)


def test_plan_naming_an_unoffered_url_skips_every_target():
    llm = Routed(plan=lambda p: {"plans": [{"target_url": ESPN, "tool": "reply"}]})
    actions, decisions = run(llm, [narrative([target()])])
    assert not [a for a in actions if a.type == "reply"]
    d = by_lane(decisions, "reply")[0]
    assert d.reason == "no_angle" and "was not offered" in d.detail


# ---- reply lane


def test_continuation_reply_carries_lane_fields_and_evidence():
    actions, decisions = run(MockLLM(), [narrative([target()])])
    (a,) = [a for a in actions if a.type == "reply"]
    assert (a.lane, a.mind, a.target_url, a.target_handle) == ("reply", "reply_lane", X_URL, "AtlantaDream")
    assert a.text.startswith("mock continuation")
    m = a.draft_meta
    assert m["method"] == "continuation" and m["writer_model"] == "claude-opus-4-5-20251101"
    assert m["page_replies"] == len(REPLIES) and m["target"]["url"] == X_URL and m["target"]["thread_attached"] == len(REPLIES)
    assert m["judge"]["orders_agree"] and m["judge"]["passed"] and m["judge"]["pick"] == "random among drafts that passed"
    assert by_lane(decisions, "reply")[0].action_id == a.id


def test_thin_thread_falls_back_to_the_styled_writer():
    actions, _ = run(MockLLM(), [narrative([target(replies=REPLIES[:2])])])
    (a,) = [a for a in actions if a.type == "reply"]
    assert a.draft_meta["method"] == "styled" and a.text.startswith("mock styled reply")


def test_provider_without_prefill_uses_styled():
    llm = Routed(styled=["state farm about to be loud"], veto=ok)
    actions, decisions = run(llm, [narrative([target()])])
    assert [a.text for a in actions if a.type == "reply"] == ["state farm about to be loud"]
    assert "can't prefill" in by_lane(decisions, "reply")[0].detail


def test_targets_from_every_narrative_are_considered():
    actions, _ = run(MockLLM(), [narrative([], nid="top"), narrative([target()], nid="reply-lane")])
    assert [a.narrative_id for a in actions if a.type == "reply"] == ["reply-lane"]


@pytest.mark.parametrize("age", [31.0, None])
def test_stale_or_undated_target_abstains(age):
    actions, decisions = run(MockLLM(), [narrative([target(age=age)])])
    assert not [a for a in actions if a.type == "reply"]
    assert by_lane(decisions, "reply")[0].reason == "stale_target"


def test_stale_cutoff_is_a_config_knob():
    actions, _ = run(MockLLM(), [narrative([target(age=50.0)])], cfg=ActionConfig(reply_max_age_minutes=60))
    assert [a.type for a in actions if a.lane == "reply"] == ["reply"]


def test_one_reply_per_conversation_root():
    assert len(lanes.reply_targets([narrative([target(), target(eph=900.0)])])) == 1


def test_fastest_target_first():
    n = narrative([target(url=X_URL, eph=100.0), target(url=X_URL2, author="WNBA", eph=900.0)])
    assert [t["url"] for t in lanes.reply_targets([n])] == [X_URL2, X_URL]


def test_plan_can_abstain_with_a_reason():
    llm = Routed(plan=lambda p: {"plans": [{"target_url": X_URL, "tool": "abstain", "reason": "mute_risk", "detail": "injury"}]})
    actions, decisions = run(llm, [narrative([target()])])
    assert by_lane(decisions, "reply")[0].reason == "mute_risk"


def test_reply_budget_caps_the_run():
    cfg = ActionConfig(run_caps={**ActionConfig().run_caps, "reply": 1})
    n = narrative([target(url=X_URL, eph=900.0), target(url=X_URL2, author="WNBA", eph=100.0)])
    actions, decisions = run(MockLLM(), [n], cfg=cfg)
    assert [a.target_url for a in actions if a.type == "reply"] == [X_URL]
    assert [d.reason for d in by_lane(decisions, "reply")] == ["", "budget_exhausted"]


def test_gate_drops_continuations_that_invent_numbers_names_or_links(monkeypatch):
    drafts = ["she drops 27 in game 1", "Stephanie said White is done", "watch www.espn.com/clip", "[video]",
              "@somebody agreed", "state farm about to be loud"]
    monkeypatch.setattr(writer, "continuations", lambda *a, **k: drafts)
    actions, decisions = run(MockLLM(), [narrative([target()])])
    assert [a.text for a in actions if a.type == "reply"] == ["state farm about to be loud"]
    rules = {f["rule"] for c in by_lane(decisions, "reply")[0].candidates for f in c.get("gate", [])}
    assert {"ungrounded_number", "ungrounded_name", "url", "placeholder", "unknown_handle"} <= rules


def test_all_drafts_failing_the_gate_abstains(monkeypatch):
    monkeypatch.setattr(writer, "continuations", lambda *a, **k: ["Ten wins — a testament"])
    actions, decisions = run(MockLLM(), [narrative([target()])])
    assert by_lane(decisions, "reply")[0].reason == "gate_rejected"


def test_veto_rejecting_everything_abstains_and_keeps_evidence():
    llm = Routed(styled=["state farm about to be loud", "ok fine then, game three"],
                 veto=lambda c: {"p_ai": 80, "unsupported": False, "off_mission": False, "risk": False, "reason": "explains the joke"})
    actions, decisions = run(llm, [narrative([target(replies=[])])])
    d = by_lane(decisions, "reply")[0]
    assert (d.reason, d.detail) == ("judge_rejected", "veto: 2x reads as AI")
    assert all(c["veto"]["fwd"]["p_ai"] == 80 for c in d.candidates)


def test_pick_is_random_among_survivors_but_reproducible():
    llm = Routed(styled=["a reply one", "a reply two", "a reply three"], veto=ok)
    picks = {run(llm, [narrative([target(replies=[])])])[0][0].text for _ in range(3)}
    assert len(picks) == 1


# ---- post lane


def chorus_action(type_, text="", handle=None, mind="meme_lord"):
    return Action(type=type_, text=text, mind=mind, narrative_id="finals", target_handle=handle)


def test_chorus_text_never_ships_but_becomes_angles():
    chorus = [chorus_action("post", "the finals start saturday"), chorus_action("mention", "@IndianaFever hi", "IndianaFever")]
    actions, decisions = run(MockLLM(), [narrative([])], chorus=chorus)
    assert not [a for a in actions if a.mind in ("meme_lord",)]
    ask = [d for d in decisions if d.tool == "ask_human"][0]
    assert [c["text"] for c in ask.candidates] == ["the finals start saturday", "@IndianaFever hi"]


def test_chorus_follows_ship_only_for_offered_handles():
    n = narrative([], signal_urls=["https://x.com/IndianaFever/status/1"])
    chorus = [chorus_action("follow", handle="randomfan"), chorus_action("follow", handle="@IndianaFever")]
    actions, decisions = run(MockLLM(), [n], chorus=chorus)
    assert [(a.type, a.target_handle) for a in actions if a.type == "follow"] == [("follow", "indianafever")]
    assert any("'randomfan' was not offered" in d.detail for d in decisions)


def test_human_take_ships_verbatim_even_with_style_findings():
    take = "this team owes AB a backup center — period"
    actions, decisions = run(MockLLM(), [narrative([])], takes=[{"text": take}])
    (a,) = [a for a in actions if a.mind == "human"]
    assert a.text == take and a.type == "post"
    assert [w["rule"] for w in a.draft_meta["gate_warnings"]] == ["em_dash"]
    assert not [d for d in decisions if d.tool == "ask_human"]


def test_human_take_quote_needs_an_offered_url():
    actions, decisions = run(MockLLM(), [narrative([])], takes=[{"text": "lol", "quote_url": X_URL}])
    assert not [a for a in actions if a.mind == "human"]
    assert any(d.reason == "no_source" and "was not offered" in d.detail for d in decisions)


def test_human_take_already_shipped_is_skipped(tmp_out):
    (tmp_out / "decisions-run1.json").write_text(json.dumps({"run_id": 1, "at": "2026-10-14T10:00:00", "decisions": [
        {"tool": "post", "opportunity": "old take", "detail": "human take, verbatim", "action_id": "a1"}]}), encoding="utf-8")
    actions, _ = run(MockLLM(), [narrative([])], takes=[{"text": "old take"}])
    assert not [a for a in actions if a.mind == "human"]


GNEWS = "https://news.google.com/rss/articles/CBMiXmh0dHBzOi8vd3d3LmVzcG4uY29tL3duYmE?oc=5"


def test_caption_quotes_a_reply_target():
    actions, _ = run(MockLLM(), [narrative([target(age=500.0)])])
    (q,) = [a for a in actions if a.mind == "caption"]
    assert (q.type, q.target_url) == ("quote", X_URL)


def test_caption_credits_the_publisher_by_name():
    cfg = ActionConfig()
    actions, _ = lanes.run(MockLLM(), "mission", "lessons", [narrative(signal_urls=[ESPN])], [], cfg=cfg,
                           budget=lanes.Budget(cfg), takes=[], sources={ESPN: "ESPN"})
    (p,) = [a for a in actions if a.mind == "caption"]
    assert p.type == "post" and p.text.endswith(f" via ESPN {ESPN}") and "(via" not in p.text
    assert p.draft_meta["credit"] == "ESPN"


def test_caption_without_a_known_publisher_credits_the_domain():
    actions, _ = run(MockLLM(), [narrative(signal_urls=[ESPN])])
    assert [a.text.endswith(f" via espn.com {ESPN}") for a in actions if a.mind == "caption"] == [True]


def test_caption_prefers_a_publisher_link_over_a_google_news_redirect():
    actions, _ = run(MockLLM(), [narrative(signal_urls=[GNEWS, ESPN])])
    (p,) = [a for a in actions if a.mind == "caption"]
    assert ESPN in p.text and "news.google.com" not in p.text


def test_only_redirect_links_means_no_caption():
    actions, decisions = run(MockLLM(), [narrative(signal_urls=[GNEWS])])
    assert not [a for a in actions if a.mind == "caption"]
    assert [d.reason for d in decisions if d.lane == "post" and d.tool == "abstain"] == ["no_linkable_source"]


def test_credit_for():
    assert lanes.credit_for("https://x.com/espnW/status/1", {}) == "@espnW"
    assert lanes.credit_for(ESPN, {ESPN: "ESPN"}) == "ESPN"
    assert lanes.credit_for(ESPN, {}) == "espn.com"


def test_retry_with_nothing_revisable_returns_nothing():
    caption = Action(type="post", text="rest up", mind="caption", narrative_id="finals", critique="opaque link")
    assert decide(MockLLM(), "mission", "lessons", [narrative(signal_urls=[ESPN])], vetoed=[caption]) == []
    assert lanes.PENDING == []  # no second caption, no decisions


def test_revising_call_never_recaptions_or_reships_takes():
    cfg = ActionConfig()
    actions, decisions = lanes.run(MockLLM(), "mission", "lessons", [narrative([target()], signal_urls=[ESPN])], [],
                                   revising=True, cfg=cfg, budget=lanes.Budget(cfg), takes=[{"text": "my take"}])
    assert actions == [] and decisions == []


def test_caption_over_cap_or_invented_fact_is_rejected():
    llm = Routed(captions=["x" * 101, "she scored 40 tonight"], veto=ok)
    actions, decisions = run(llm, [narrative(signal_urls=[ESPN])])
    assert not [a for a in actions if a.mind == "caption"]
    rules = {f["rule"] for d in decisions for c in d.candidates for f in c.get("gate", [])}
    assert {"length", "ungrounded_number"} <= rules


# ---- budget and log


def test_write_decision_log_uses_the_run_id_and_clears(tmp_out):
    decide(MockLLM(), "mission", "lessons", [narrative([target()])], per_mind=1)
    path = lanes.write_decision_log(7)
    log = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "decisions-run7.json" and log["run_id"] == 7 and log["decisions"] and log["provider"] == "mock"
    assert lanes.PENDING == [] and lanes.write_decision_log(8) is None


def write_approved(tmp_out, run_id, actions):
    (tmp_out / f"approved-run{run_id}.json").write_text(json.dumps({"run_id": run_id, "actions": actions}), encoding="utf-8")


def test_daily_budget_and_post_spacing_count_only_approvals(tmp_out):
    now = datetime(2026, 10, 15, 21, 0)
    stamp = lambda minutes: (now - timedelta(minutes=minutes)).astimezone().isoformat()  # noqa: E731
    write_approved(tmp_out, 1, [{"type": "reply", "approved_at": stamp(40)}, {"type": "quote", "approved_at": stamp(30)}])
    write_approved(tmp_out, 0, [{"type": "reply", "approved_at": "2026-10-14T21:00:00+00:00"}])  # yesterday
    b = lanes.Budget.from_approvals(ActionConfig(), now=now)
    assert b.used_today == {"reply": 1, "quote": 1}
    assert b.left("post") == 0 and b.left("quote") == 0  # spaced: an approved quote went out 30 minutes ago
    assert b.left("reply") == min(ActionConfig().run_caps["reply"], ActionConfig().daily_caps["reply"] - 1)
    assert lanes.Budget.from_approvals(ActionConfig(), now=now + timedelta(hours=2)).left("post") == 1


def test_recommendations_without_approval_spend_nothing(tmp_out):
    now = datetime(2026, 10, 15, 21, 0)
    # A run recommended a quote; nobody approved it (Integration: run 22 then blocked run 31).
    (tmp_out / "decisions-run22.json").write_text(json.dumps({"run_id": 22, "at": now.isoformat(), "provider": "AnthropicLLM",
        "decisions": [{"tool": "quote", "action_id": "q"}]}), encoding="utf-8")
    write_approved(tmp_out, 22, [])
    b = lanes.Budget.from_approvals(ActionConfig(), now=now)
    assert b.used_today == {} and b.left("quote") == ActionConfig().run_caps["quote"]


def test_killing_an_approval_frees_the_budget(tmp_out):
    now = datetime(2026, 10, 15, 21, 0)
    write_approved(tmp_out, 3, [{"type": "post", "approved_at": (now - timedelta(minutes=5)).astimezone().isoformat()}])
    assert lanes.Budget.from_approvals(ActionConfig(), now=now).left("post") == 0
    write_approved(tmp_out, 3, [])  # the dashboard rewrites the file when the approval is killed
    assert lanes.Budget.from_approvals(ActionConfig(), now=now).left("post") == 1


def test_page_drops_replies_the_judge_reads_as_bots():
    bot = "The defensive talent in the WNBA is unreal and the energy is electric right now"
    thread = REPLIES + [{"text": bot, "likes": 1}]
    llm = Routed(styled=["state farm about to be loud"],
                 veto=lambda c: {"p_ai": 95 if "energy is electric" in c else 5, "unsupported": False, "off_mission": False, "risk": False, "reason": ""})
    actions, _ = run(llm, [narrative([target(replies=thread)])])
    (a,) = [a for a in actions if a.type == "reply"]
    assert a.draft_meta["page_dropped_as_bots"] == [bot] and a.draft_meta["page_replies"] == len(REPLIES)


def test_bot_filter_is_a_knob():
    thread = REPLIES + [{"text": "the energy is electric right now for real", "likes": 1}]
    llm = Routed(styled=["ok then sure"], veto=lambda c: {"p_ai": 95 if "electric" in c else 5, "unsupported": False, "off_mission": False, "risk": False})
    actions, _ = run(llm, [narrative([target(replies=thread)])], cfg=ActionConfig(filter_page_bots=False))
    assert [a.draft_meta["page_dropped_as_bots"] for a in actions if a.type == "reply"] == [[]]


def test_mock_behind_a_proxy_never_touches_network_logs_or_budget(tmp_out):
    from tests.test_minds_veto_writer import Proxy

    write_approved(tmp_out, 1, [{"type": "quote", "approved_at": datetime.now().astimezone().isoformat()}])
    # thread=[] with a nonzero reply count would make a real run fetch; mock must not.
    t = target(replies=[]) | {"replies": 40}
    actions, _ = lanes.run(Proxy(MockLLM()), "mission", "lessons", [narrative([t])], [],
                           cfg=ActionConfig(), takes=[], fetch=lambda url: pytest.fail("mock run fetched the network"))
    assert lanes.PENDING_PROVIDER["name"] == "mock"
    assert [a.type for a in actions] == ["reply", "quote"]  # a real budget would have blocked the quote (spacing)



def test_caption_of_hand_supplied_material_has_no_link():
    # The operator dashboard ingests pasted signals with no URL (engine/demo, tests/test_dashboard.py).
    n = Narrative(id="manual", title="A sample release", summary="A sample release", relevance=0.5, velocity=0.5)
    actions, decisions = run(MockLLM(), [n])
    (p,) = [a for a in actions if a.mind == "caption"]
    assert p.type == "post" and "http" not in p.text and " via " not in p.text
    assert p.draft_meta["method"] == "caption, no link" and p.draft_meta["source_url"] is None


def test_target_narratives_adds_the_best_narrative_with_reply_targets():
    from harness.minds import target_narratives

    plain = [Narrative(id=f"n{i}", title="t", summary="s", relevance=0.9 - i * 0.1, velocity=0.5) for i in range(3)]
    weak, strong = narrative([target()], nid="weak"), narrative([target(url=X_URL2)], nid="strong")
    weak.relevance, strong.relevance = 0.2, 0.6
    picked = target_narratives(plain + [weak, strong], top=2, explore=0)
    assert [p["id"] for p in picked] == ["n0", "n1", "strong"]
    assert [p["reply_targets_fallback"] for p in picked] == [False, False, True]
    assert not picked[-1]["explore"]


def test_target_narratives_leaves_picks_alone_when_one_has_targets():
    from harness.minds import target_narratives

    first = narrative([target()], nid="first")
    other = narrative([target(url=X_URL2)], nid="other")
    other.relevance = 0.1
    picked = target_narratives([first, Narrative(id="n1", title="t", summary="s"), other], top=2, explore=0)
    assert [p["id"] for p in picked] == ["first", "n1"]


def rival_plan(prompt):
    return {"plans": [{"target_url": X_URL, "tool": "reply", "side": "rival", "intent": "jab"}]}


def test_rival_thread_banters_with_the_styled_writer():
    prompts = []

    class Spy(Routed):
        def complete_json(self, system, prompt, mock=None):
            prompts.append(prompt)
            return super().complete_json(system, prompt, mock)

    llm = Spy(plan=rival_plan, styled=["state farm is still loud for game three"], veto=ok)
    actions, _ = run(llm, [narrative([target()])])
    (a,) = [a for a in actions if a.type == "reply"]
    assert (a.draft_meta["method"], a.draft_meta["side"]) == ("banter", "rival")
    assert any(brand.text("rival_angle") in p for p in prompts)


def test_rival_thread_can_abstain():
    actions, decisions = run(Routed(plan=rival_plan, veto=ok), [narrative([target()])],
                             cfg=ActionConfig(rival_thread_mode="abstain"))
    assert not [a for a in actions if a.type == "reply"]
    assert [(d.reason, d.detail) for d in decisions if d.lane == "reply"] == [("off_mission", "rival thread; rival_thread_mode is abstain")]


def test_own_side_thread_continues_under_the_framing_handle(monkeypatch, tmp_path):
    path = tmp_path / "reply_framing.json"
    path.write_text(json.dumps({"framing_handle": "me"}), encoding="utf-8")
    monkeypatch.setenv("HARNESS_REPLY_FRAMING", str(path))
    actions, _ = run(MockLLM(), [narrative([target()])])
    (a,) = [a for a in actions if a.type == "reply"]
    assert (a.draft_meta["method"], a.draft_meta["side"], a.draft_meta["written_as"]) == ("continuation", "own", "me")
