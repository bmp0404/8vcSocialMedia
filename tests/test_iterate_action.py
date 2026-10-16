"""Tests for the action lineup instrument's single-answer pieces."""

import pytest

from harness.iterate.fitness import action
from harness.iterate.llm import Meter, MeteredLLM, MockMeteredLLM


def test_pipeline_mode_dispatches_on_type_not_attributes():
    meter = Meter(10)
    assert action.pipeline_mode(MockMeteredLLM(meter, "arm", lambda s, p: {})) == "mock"
    real = object.__new__(MeteredLLM)  # skip __init__: no client, no network
    real.meter, real.arm = meter, "arm"
    assert action.pipeline_mode(real) == "metered"

    class Lookalike:  # has a meter and an arm, but is neither type
        pass

    fake = Lookalike()
    fake.meter, fake.arm = meter, "arm"
    with pytest.raises(TypeError):
        action.pipeline_mode(fake)


def test_lineup_is_seeded_and_holds_the_candidate_once():
    thread = {"id": "t1", "pool": [{"text": f"real {i}"} for i in range(6)]}
    shown, pos = action.lineup(thread, "candidate", seed=0)
    assert len(shown) == 5 and shown.count("candidate") == 1 and shown[pos - 1] == "candidate"
    assert action.lineup(thread, "candidate", seed=0) == (shown, pos)
    assert len({tuple(action.lineup(thread, "candidate", seed=s)[0]) for s in range(20)}) > 1


def test_tidy_draft_strips_quotes_mentions_links_and_extra_lines():
    assert action.tidy_draft('"@fan1 @fan2 what a shot https://t.co/abc"\nsecond line') == "what a shot"
    assert action.tidy_draft("") == ""


def test_mock_detector_finds_the_mock_reply_at_its_method_rate():
    hits = 0
    for i in range(200):
        prompt = "REPLIES:\n1. real a\n2. mock continuation reply\n3. real b\n4. real c\n5. real d\n" + f"#{i}"
        hits += action.mock_detector(action.LINEUP_SYSTEM, prompt)["ai_reply"] == 2
    assert 50 < hits < 95  # 0.35 rate over 200 draws


class FakeMessages:
    """The SDK's `messages` with canned answers by prompt; records the model of every request."""

    def __init__(self, sent):
        self.sent = sent

    def create(self, **kw):
        import re
        import types

        self.sent.append(kw["model"])
        system, content = kw.get("system", ""), kw["messages"][0]["content"]
        if system.startswith("You screen candidate"):
            n = len(re.findall(r"^\d+\. ", content.split("CANDIDATES:\n")[-1], re.M))
            verdict = '{"p_ai": 10, "unsupported": false, "off_mission": false, "risk": false, "reason": "ok"}'
            text = '{"verdicts": [' + ", ".join([verdict] * n) + "]}"
        else:
            text = "fever by twelve, book it\n"  # a continuation line, ended by the stop sequence
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1), model=kw["model"])


def test_the_pipeline_arm_charges_every_model_call_to_its_meter(monkeypatch):
    import types

    import anthropic

    from harness.iterate.knobs import DEFAULT
    from harness.minds import routing

    sent = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: types.SimpleNamespace(messages=FakeMessages(sent)))
    meter, cache = Meter(100), {}
    thread = {"id": "1", "author": "IndianaFever", "url": "https://x.com/IndianaFever/status/1",
              "text": "fever win in overtime tonight", "pool": [],
              "context": [{"text": f"what a finish number {i}", "likes": 1} for i in range(12)]}
    cfg = DEFAULT.with_changes({"action.reply.generation": "pipeline", "action.samples": 6})
    draft = action.gen_pipeline(cfg, thread, lambda m: cache.setdefault(m, MeteredLLM(m, meter, "arm")), seed=0)
    assert draft == "fever by twelve, book it"
    # The page filter, six continuations and the veto in two orders, each one charged to the arm.
    assert len(sent) == 9 and meter.calls == 9
    assert routing.AnthropicLLM.__name__ == "AnthropicLLM"  # the swap is undone


THREAD = {"id": "2", "author": "IndianaFever", "url": "https://x.com/IndianaFever/status/2", "text": "fever win in overtime",
          "pool": [{"text": f"top reply number {i}", "likes": 50 - i} for i in range(6)],
          "context": [{"text": f"page reply number {i}", "likes": 3} for i in range(12)]}


class Recording:
    """A config that notes every knob an arm asks it for."""

    def __init__(self, cfg):
        self.cfg, self.seen = cfg, set()

    def get(self, path):
        self.seen.add(path)
        return self.cfg.get(path)


def _mock_llm_for(meter, respond=action.ActionLineup.mock_respond):
    cache = {}
    return lambda model: cache.setdefault(model, MockMeteredLLM(meter, "arm", respond, model))


@pytest.mark.parametrize("changes", [
    {"action.reply.generation": "persona_json"},
    {"action.reply.generation": "styled"},
    {"action.reply.generation": "continuation"},
    {"action.reply.generation": "continuation", "action.continuation.page_filter": True},
    {"action.reply.generation": "continuation", "action.continuation.framing": "profile"},
    {"action.reply.generation": "pipeline", "action.continuation.framing": "profile"},
])
def test_reads_names_exactly_the_knobs_an_arm_asks_for(changes):
    from harness import config
    from harness.iterate.knobs import DEFAULT

    cfg = DEFAULT.with_changes(changes)
    rec = Recording(cfg)
    with config.use_profile("wnba"):
        _, row = action.ActionLineup()._one(rec, THREAD, _mock_llm_for(Meter(100)), seed=0, lineup_seed=0)
    assert row["shipped"], row
    assert rec.seen == set(action.ActionLineup.reads(cfg))
    assert set(action.ActionLineup.reads(cfg)) <= set(action.ActionLineup.knobs)


class Scripted(MockMeteredLLM):
    """A metered mock whose JSON answers come from `respond` even when the caller passes a `mock=`."""

    def complete_json(self, system, prompt, mock=None):
        self.meter.reserve(self.arm)
        return self.respond(system, prompt)


def _prefill(cfg, profile="wnba", judge=None):
    """The page raw continuation sends, and the calls it made. `judge(n)` gives the P(AI) verdicts."""
    from harness import config

    sent, meter = [], Meter(100)

    def answer(system, prompt):
        if system.startswith("You screen candidate"):
            return judge(prompt.split("CANDIDATES:\n")[-1].count("\n") + 1)
        sent.append(prompt)
        return "a reply"

    cache = {}
    with config.use_profile(profile):
        action.gen_continuation(cfg, THREAD, lambda m: cache.setdefault(m, Scripted(meter, "arm", answer, m)), seed=0)
    return sent[-1], meter.calls


def test_framing_profile_writes_under_the_profile_handle_and_neutral_under_the_next_user():
    from harness.iterate.knobs import DEFAULT

    neutral, _ = _prefill(DEFAULT)
    assert neutral.endswith("\n@user13:") and neutral.count("\n@user") == 13
    framed, _ = _prefill(DEFAULT.with_changes({"action.continuation.framing": "profile"}))
    assert framed.endswith("\n@clarkfanaccount:") and framed[: -len("clarkfanaccount:")] == neutral[: -len("user13:")]
    # devtools' reply_framing.json has a null handle, which means neutral handles.
    devtools, _ = _prefill(DEFAULT.with_changes({"action.continuation.framing": "profile"}), profile="devtools")
    assert devtools.endswith("\n@user13:")


def test_page_replies_cuts_the_page_and_renumbers_the_writer():
    from harness.iterate.knobs import DEFAULT

    page, calls = _prefill(DEFAULT.with_changes({"action.page_replies": 2}))
    assert page.count("\n@user") == 3 and page.endswith("page reply number 1\n@user03:") and calls == 1


def test_the_page_filter_drops_replies_the_judge_reads_as_bots():
    from harness.iterate.knobs import DEFAULT

    def first_is_a_bot(n):
        return {"verdicts": [{"p_ai": 90 if i == 0 else 10, "unsupported": False, "off_mission": False,
                              "risk": False, "reason": "x"} for i in range(n)]}

    cfg = DEFAULT.with_changes({"action.continuation.page_filter": True})
    page, calls = _prefill(cfg, judge=first_is_a_bot)
    assert "page reply number 0" not in page and page.count("\n@user") == 12 and page.endswith("\n@user12:")
    assert calls == 2  # the judge, then the writer
    whole, _ = _prefill(cfg, judge=lambda n: {"verdicts": []})
    assert "page reply number 0" in whole  # as in the live lane, a malformed answer leaves the page whole
    unfiltered, calls = _prefill(DEFAULT, judge=first_is_a_bot)
    assert "page reply number 0" in unfiltered and calls == 1


def test_the_estimate_counts_every_call_an_arm_can_make():
    from harness.iterate.knobs import DEFAULT

    fit = action.ActionLineup()
    assert fit.estimate_calls(DEFAULT, 10) == 40
    assert fit.estimate_calls(DEFAULT.with_changes({"action.reply.generation": "continuation"}), 10) == 40
    assert fit.estimate_calls(DEFAULT.with_changes({"action.reply.generation": "continuation",
                                                    "action.continuation.page_filter": True}), 10) == 50
    # Pipeline: two detectors, the instrument's veto, its page filter, six samples, its veto in two orders.
    assert fit.estimate_calls(DEFAULT.with_changes({"action.reply.generation": "pipeline", "action.samples": 6}), 10) == 120


def test_a_profile_framing_without_a_handle_is_refused_before_it_costs_anything():
    from harness import config
    from harness.iterate.knobs import DEFAULT

    fit = action.ActionLineup()
    framed = DEFAULT.with_changes({"action.reply.generation": "continuation", "action.continuation.framing": "profile"})
    with config.use_profile("wnba"):
        assert fit.check_config(framed) is None
    with config.use_profile("devtools"):
        assert "has no handle" in fit.check_config(framed)
        # persona_json doesn't read the framing knob, so the knob can't mislead it
        assert fit.check_config(DEFAULT.with_changes({"action.continuation.framing": "profile"})) is None


def test_the_pipeline_client_counts_every_retry_and_leaves_none_to_the_sdk(monkeypatch):
    import types

    import anthropic

    from harness.iterate import llm as metered

    built, sent = [], []

    class Flaky:
        def create(self, **kw):
            sent.append(kw["model"])
            if len(sent) < 3:
                raise anthropic.APIConnectionError(request=None)  # the SDK's own type; the request isn't read
            return types.SimpleNamespace(usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

    def client(*a, **k):
        built.append(k)
        return types.SimpleNamespace(messages=Flaky())

    monkeypatch.setattr(anthropic, "Anthropic", client)
    monkeypatch.setattr(metered.time, "sleep", lambda s: None)
    meter = Meter(10)
    llm = metered.metered_anthropic_class(meter, "arm")("claude-opus-5-5")
    llm.client.messages.create(model="claude-opus-5-5", max_tokens=5, messages=[])
    assert built == [{"timeout": metered.CALL_TIMEOUT_S, "max_retries": 0}]
    assert len(sent) == 3 and meter.calls == 3 and meter.by_arm["arm"]["errors"] == 2
    capped = metered.metered_anthropic_class(Meter(2), "arm")("claude-opus-5-5")
    sent.clear()
    with pytest.raises(metered.BudgetExceeded):  # the third attempt would pass the cap, so it never goes out
        capped.client.messages.create(model="claude-opus-5-5", max_tokens=5, messages=[])
    assert len(sent) == 2
