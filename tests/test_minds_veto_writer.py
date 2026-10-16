import json
import re

import pytest

from harness.llm import LLM, AnthropicLLM, MockLLM
from harness.minds import settings, thread, veto, writer
from harness.minds.settings import ActionConfig, GateLimits

CFG = ActionConfig()


class Judge(LLM):
    """Flags candidates by text: 'ai' -> high P(AI), 'lie' -> unsupported, 'mean' -> risk,
    'cheer' -> off mission.
    `flip` flags 'edge' only on the first (forward) pass, to make a split verdict."""

    def __init__(self, flip=False, count_off=False):
        self.flip, self.count_off, self.calls = flip, count_off, 0

    def complete_json(self, system, prompt, mock=None):
        self.calls += 1
        cands = re.findall(r"^\d+\. (.*)$", prompt.split("CANDIDATES:")[1], re.M)
        out = [{"p_ai": 90 if "ai" in c or (self.flip and self.calls == 1 and "edge" in c) else 5,
                "unsupported": "lie" in c, "risk": "mean" in c, "off_mission": "cheer" in c, "reason": c} for c in cands]
        return {"verdicts": out[:-1] if self.count_off else out}


def test_veto_maps_reversed_verdicts_back_to_the_right_draft():
    res = veto.veto(Judge(), CFG, ["fine one", "sounds ai", "fine two"], lane="reply", context="ctx", mission="m")
    assert [(r.text, r.passed) for r in res] == [("fine one", True), ("sounds ai", False), ("fine two", True)]
    assert all(r.fwd.reason == r.rev.reason == r.text for r in res)


def test_veto_asks_in_two_orders():
    j = Judge()
    veto.veto(j, CFG, ["a b", "c d"], lane="reply", context="ctx", mission="m")
    assert j.calls == 2


def test_veto_reasons():
    res = {r.text: r for r in veto.veto(Judge(), CFG, ["ai voice", "a lie", "so mean", "fine"], lane="reply", context="c", mission="m")}
    assert res["ai voice"].why == "reads as AI"
    assert res["a lie"].why == "unsupported claim"
    assert res["so mean"].why == "mute/report risk"
    assert res["fine"].passed and res["fine"].orders_agree


def test_split_verdict_is_a_veto():
    (r,) = veto.veto(Judge(flip=True), CFG, ["edge case"], lane="reply", context="c", mission="m")
    assert not r.passed and r.why == "split verdict between orders" and not r.orders_agree


def test_wrong_verdict_count_vetoes_the_batch():
    res = veto.veto(Judge(count_off=True), CFG, ["a", "b"], lane="reply", context="c", mission="m")
    assert all(not r.passed and "2 candidates" in r.why for r in res)


def test_threshold_is_a_knob():
    loose = ActionConfig(veto_max_p_ai=95)
    assert veto.veto(Judge(), loose, ["sounds ai"], lane="reply", context="c", mission="m")[0].passed


def test_mock_judge_passes():
    assert all(r.passed for r in veto.veto(MockLLM(), CFG, ["a", "b"], lane="reply", context="c", mission="m"))


def test_judge_model_is_opus_for_the_gateway(monkeypatch):
    llm = fake_anthropic("x")
    monkeypatch.setattr(AnthropicLLM, "__init__", lambda self, model: setattr(self, "model", model))
    assert veto._judge_llm(llm, CFG).model == "claude-opus-5-5"


# ---- writer


def test_page_uses_neutral_handles_and_no_counts():
    page = writer.build_page("WNBA", "Finals week", ["first reply", "second reply"])
    assert page.endswith("Replies (sorted by relevance):\n\n@user01: first reply\n@user02: second reply")
    assert "likes" not in page and "followers" not in page


class FakeClient:
    def __init__(self, text):
        self.text, self.kwargs = text, []
        self.messages = self

    def create(self, **kw):
        self.kwargs.append(kw)

        class Block:
            type, text = "text", self.text

        class Resp:
            content = [Block()]

        return Resp()


def fake_anthropic(text):
    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.client, llm.model = FakeClient(text), "claude-sonnet-5-5"
    return llm


def test_continuation_prefills_the_page_and_stops_at_the_next_reply():
    llm = fake_anthropic(' "state farm about to be loud"\nsecond line')
    out = writer.continue_page(llm, "PAGE", "kaymarie", CFG)
    kw = llm.client.kwargs[0]
    assert out == "state farm about to be loud"
    assert kw["model"] == "claude-opus-4-5-20251101" and kw["stop_sequences"] == ["\n@"]
    assert kw["messages"][-1] == {"role": "assistant", "content": "PAGE\n@kaymarie:"}


def test_continuations_use_distinct_handles():
    llm = fake_anthropic("ok")
    out = writer.continuations(llm, "WNBA", "post", ["r"] * 20, ActionConfig(reply_samples=6, page_replies=3), seed="u")
    handles = [k["messages"][-1]["content"].rsplit("@", 1)[1] for k in llm.client.kwargs]
    assert len(out) == 6 and len(set(handles)) == 6
    assert all(k["messages"][-1]["content"].count("@user") == 3 for k in llm.client.kwargs)  # page_replies caps the page


def test_continuation_needs_a_prefill_provider():
    with pytest.raises(writer.WriterUnavailable, match="can't prefill"):
        writer.continue_page(LLM(), "PAGE", "h", CFG)


# ---- thread


def test_clean_strips_reply_mentions_and_tco():
    assert thread.clean("@IndianaFever @AmberLCox rest up AB https://t.co/abc123") == "rest up AB"
    assert thread.clean("line one\nline two") == "line one / line two"


def test_replies_for_prefers_the_attached_thread_and_drops_noise():
    t = {"url": "https://x.com/a/status/1", "replies": 2,
         "thread": [{"author": "fan1", "text": "@a so true honestly lol"}, {"author": "fan2", "text": "@a ok"}]}
    got = thread.replies_for(t, fetch=lambda u: pytest.fail("no fetch"))
    assert [(r["author"], r["text"]) for r in got] == [("fan1", "so true honestly lol")]


def test_replies_for_fetches_only_when_the_post_has_replies():
    assert thread.replies_for({"url": "u", "thread": [], "replies": 5}, fetch=lambda u: [{"text": "x"}]) == [{"text": "x"}]
    assert thread.replies_for({"url": "u", "thread": [], "replies": 0}, fetch=lambda u: pytest.fail("no fetch")) == []


def test_status_id():
    assert thread.status_id("https://x.com/WNBA/status/2110808211347456506?s=20") == "2110808211347456506"
    with pytest.raises(thread.ThreadUnavailable, match="no status id"):
        thread.status_id("https://www.espn.com/x")


# ---- settings


def test_config_round_trips_and_rejects_unknown_fields(tmp_path, monkeypatch):
    cfg = ActionConfig.from_dict({"reply_samples": 8, "gate": {"max_distinct_emoji": 3}})
    assert cfg.reply_samples == 8 and cfg.gate == GateLimits(max_distinct_emoji=3)
    assert ActionConfig.from_dict(cfg.to_dict()) == cfg
    with pytest.raises(ValueError, match=r"unknown field\(s\) \['samples'\]"):
        ActionConfig.from_dict({"samples": 3})
    with pytest.raises(ValueError, match=r"GateLimits: unknown field\(s\) \['emoji'\]"):
        ActionConfig.from_dict({"gate": {"emoji": 3}})
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"reply_max_age_minutes": 45}), encoding="utf-8")
    monkeypatch.setenv("HARNESS_ACTION_CONFIG", str(path))
    assert settings.load().reply_max_age_minutes == 45


# ---- routing survives wrappers (the tracing console wraps the LLM in a proxy)


class Proxy:
    """Forwards attributes to the wrapped LLM, the way harness/trace.py's TracingLLM does."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete_json(self, system, prompt, mock=None):
        return self.inner.complete_json(system, prompt, mock=mock)


class Opaque:
    """A wrapper that forwards nothing but calls. Routing can't see a client through it."""

    def __init__(self, inner):
        self._inner = inner

    def complete_json(self, system, prompt, mock=None):
        return self._inner.complete_json(system, prompt, mock=mock)


def test_continuation_through_a_proxy_still_uses_opus_4_5():
    llm = fake_anthropic("loud for real")
    assert writer.continue_page(Proxy(llm), "PAGE", "kaymarie", CFG) == "loud for real"
    assert llm.client.kwargs[0]["model"] == "claude-opus-4-5-20251101"


def test_veto_through_a_proxy_still_uses_opus_5_5(monkeypatch):
    llm = fake_anthropic("x")
    monkeypatch.setattr(AnthropicLLM, "__init__", lambda self, model: setattr(self, "model", model))
    assert veto._judge_llm(Proxy(llm), CFG).model == "claude-opus-5-5"


def test_mock_through_a_proxy_is_still_mock():
    from harness.minds import routing

    assert routing.is_mock(Proxy(MockLLM())) and routing.is_mock(Proxy(Proxy(MockLLM())))
    assert writer.continue_page(Proxy(MockLLM()), "PAGE", "h", CFG) == "mock continuation alpha"


def test_fallback_to_the_run_model_is_loud(capsys):
    from harness.minds import routing

    routing._warned.clear()
    opaque = Opaque(fake_anthropic("x"))
    assert veto._judge_llm(opaque, CFG) is opaque
    assert "the veto judge wants claude-opus-5-5" in capsys.readouterr().out
    with pytest.raises(writer.WriterUnavailable):
        writer.continue_page(opaque, "PAGE", "h", CFG)



# ---- mission fit (Iterate's 27-thread run shipped these past the old veto)

REAL_SHIPPED = ["Let's go Angel Reese, another championship \U0001F3C6", "talking shit about a rookie"]


class MissionJudge(LLM):
    """Answers the way the mission check should on Iterate's shipped strings, and records the prompt."""

    def __init__(self):
        self.systems = []

    def complete_json(self, system, prompt, mock=None):
        self.systems.append(system)
        cands = re.findall(r"^\d+\. (.*)$", prompt.split("CANDIDATES:")[1], re.M)
        return {"verdicts": [{"p_ai": 20, "unsupported": "championship" in c, "off_mission": "Let's go Angel Reese" in c,
                              "risk": "shit about" in c, "reason": ""} for c in cands]}


def test_mission_fit_vetoes_cheering_the_rival_and_insults():
    j = MissionJudge()
    mission = "Caitlin Clark and Indiana Fever fan account. Rivalry banter beats generic praise. Never cruel."
    res = {r.text: r for r in veto.veto(j, CFG, REAL_SHIPPED + ["fever defense next year though"], lane="reply",
                                        context="POST by @WNBA: Angel Reese has found a way to win", mission=mission)}
    assert res[REAL_SHIPPED[0]].why == "unsupported claim, off mission"
    assert res[REAL_SHIPPED[1]].why == "mute/report risk"
    assert res["fever defense next year though"].passed
    assert len(j.systems) == 2 and all(mission in s and "off_mission" in s for s in j.systems)


def test_missing_off_mission_field_counts_as_a_veto():
    class OldShape(LLM):
        def complete_json(self, system, prompt, mock=None):
            return {"verdicts": [{"p_ai": 5, "unsupported": False, "risk": False}]}

    (r,) = veto.veto(OldShape(), CFG, ["fine reply here"], lane="reply", context="c", mission="m")
    assert not r.passed and r.why == "off mission"


def test_off_mission_split_between_orders_is_a_veto():
    class Flip(LLM):
        calls = 0

        def complete_json(self, system, prompt, mock=None):
            Flip.calls += 1
            return {"verdicts": [{"p_ai": 5, "unsupported": False, "risk": False, "off_mission": Flip.calls == 1}]}

    (r,) = veto.veto(Flip(), CFG, ["go valks"], lane="reply", context="c", mission="m")
    assert not r.passed and r.why == "split verdict between orders"


# ---- writing from the account's side (Iterate v3: 12 of 20 vetoes spoke as the other team's fans)

FRAMING = writer.ReplyFraming("clarkfanaccount")


def test_continuations_reply_under_the_framing_handle():
    llm = fake_anthropic("ok")
    writer.continuations(llm, "WNBA", "post", ["r"] * 8, ActionConfig(reply_samples=4), seed="u", framing=FRAMING)
    prefills = [k["messages"][-1]["content"] for k in llm.client.kwargs]
    assert len(prefills) == 4 and all(p.endswith("\n@clarkfanaccount:") for p in prefills)
    # The page itself stays as before: no header, other handles neutral, no counts.
    assert all("Signed in" not in p and "@user01: r" in p and "likes" not in p for p in prefills)


def test_load_framing_validates_loudly(tmp_path, monkeypatch):
    good = tmp_path / "a.json"
    good.write_text(json.dumps({"_note": "x", "framing_handle": "@me"}), encoding="utf-8")
    assert writer.load_framing(good) == writer.ReplyFraming("me")
    for bad in ({"framing_handle": ""}, {"framing_handle": "me", "bio": "fan"}, {"handle": "me"}, {}):
        path = tmp_path / "b.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError, match="needs exactly one non-empty 'framing_handle'"):
            writer.load_framing(path)
    assert writer.load_framing(tmp_path / "missing.json") is None
    monkeypatch.setenv("HARNESS_ACCOUNT", str(good))
    with pytest.raises(ValueError, match="HARNESS_ACCOUNT is now HARNESS_REPLY_FRAMING"):
        writer.load_framing()


def test_shipped_framing_file_loads():
    assert writer.load_framing(writer.FRAMING_PATH) == writer.ReplyFraming("clarkfanaccount")


def test_rival_thread_mode_is_validated():
    with pytest.raises(ValueError, match="rival_thread_mode must be one of"):
        ActionConfig(rival_thread_mode="cheer")
