import pytest

from harness.minds import gate
from harness.minds.settings import GateLimits


def rules(text: str, kind: str = "reply") -> set[str]:
    return {f.rule for f in gate.check(text, kind).findings}


# Real top replies from action/fixtures/x_threads.json. The gate must let every one through.
REAL_REPLIES = [
    "Soft launching that gambling sponsorship",
    "Now fire the coach",
    "Skip unrivaled this time. Focus on your on health.",
    "I’m gonna miss trolling you Clark, but same time next year, same results right \U0001F602\U0001F602\U0001F602\U0001F602",
    "Went 0-10 in playoffs since college",
    "Proud of you GOAT \U0001F979",
    "dream in four, idgaf!! \U0001FAF0\U0001F3FD",
]


@pytest.mark.parametrize("text", REAL_REPLIES)
def test_real_replies_pass(text):
    res = gate.check(text, "reply")
    assert res.passed, res.reasons()


def test_em_dash_and_spaced_en_dash_and_double_hyphen():
    assert rules("Ten straight wins — the DB effect") == {"em_dash"}
    assert rules("Ten straight wins – the DB effect") == {"em_dash"}
    assert rules("Ten straight wins -- the DB effect") == {"em_dash"}
    assert rules("2025–2026 season") == set()  # an en dash in a range is not a dash
    assert rules("a 10-0 run") == set()


def test_not_x_but_y_variants():
    assert "not_x_but_y" in rules("Not just a win, but a statement")
    assert "not_x_but_y" in rules("It's not about the stats, it's about heart")
    assert "not_x_but_y" in rules("She isn't just a shooter")
    assert "not_x_but_y" in rules("More than just a game")
    assert rules("not bad for a rookie") == set()


def test_ai_vocab_and_sports_cliches():
    assert "ai_vocab" in rules("A testament to her work")
    assert "ai_vocab" in rules("Her resilience is unmatched")
    assert "ai_vocab" in rules("Here's to a healthy return")
    assert "ai_vocab" in rules("Boston is just getting started")
    assert rules("Shes washed") == set()


def test_assistant_voice():
    assert "assistant_voice" in rules("Here's a reply: go dream")
    assert "assistant_voice" in rules("Reply: dream in 4")
    assert "assistant_voice" in rules("Certainly! Dream in 4")
    assert "assistant_voice" in rules("I hope this helps")
    assert "assistant_voice" in rules("dream in **4**")
    assert rules("I hope this team signs a backup center") == set()


def test_emoji_counts_distinct_and_ignores_repeats_and_skin_tones():
    assert rules("lol \U0001F602\U0001F602\U0001F602\U0001F602\U0001F602") == set()
    assert rules("yes \U0001F44F\U0001F3FD\U0001F44F\U0001F3FF") == set()  # same emoji, two skin tones
    assert rules("gold \U0001F947\U0001F1FA\U0001F1F8") == set()  # medal + one flag = 2
    assert "emoji" in rules("lets go \U0001F525\U0001F4AF\U0001F64C")


def test_distinct_emoji_treats_a_flag_as_one():
    assert gate.distinct_emoji("\U0001F1FA\U0001F1F8") == {"\U0001F1FA\U0001F1F8"}


def test_hashtag_caps_differ_by_kind():
    assert rules("go #Fever") == set()
    assert "hashtags" in rules("go #Fever #WNBA")
    assert rules("go #Fever #WNBA", "post") == set()
    assert "hashtags" in rules("go #Fever #WNBA #CC22", "post")
    assert rules("AT&T#1 fan") == set()


def test_length_cap_is_x_limit_by_default_and_configurable():
    assert rules("a" * 280, "reply") == set()
    assert "length" in rules("a" * 281, "reply")
    tight = GateLimits(max_chars={"reply": 100, "quote": 100, "post": 100, "mention": 100})
    res = gate.check("a" * 101, "post", limits=tight)
    assert [(f.rule, f.start, f.end) for f in res.findings] == [("length", 100, 101)]


def test_placeholders_block_shipping():
    assert "placeholder" in rules("she shot [VERIFY: 45%] from three")
    assert "placeholder" in rules("hi {name}")
    assert "placeholder" in rules("TODO")


def test_wrapped_quotes_and_empty():
    assert "wrapped_in_quotes" in rules('"dream in 4"')
    assert "wrapped_in_quotes" in rules("“dream in 4”")
    assert rules('she said "dream in 4"') == set()
    assert rules("   ") == {"empty"}


def test_reply_sentence_cap_is_off_by_default_and_reply_only():
    three = "One. Two. Three."
    assert "reply_sentences" not in rules(three, "reply")
    capped = GateLimits(max_reply_sentences=2)
    assert "reply_sentences" in {f.rule for f in gate.check(three, "reply", limits=capped).findings}
    assert "reply_sentences" not in {f.rule for f in gate.check(three, "post", limits=capped).findings}


def test_unknown_kind_is_loud():
    with pytest.raises(ValueError, match="unknown kind 'dm'"):
        gate.check("hi", "dm")


def test_reasons_name_rule_and_detail():
    res = gate.check("A testament — truly", "reply")
    assert not res.passed
    assert "em_dash: dash at char 12" in res.reasons()
    assert any(r.startswith("ai_vocab: ") for r in res.reasons())
