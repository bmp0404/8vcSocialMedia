"""weighted_length against X's documented counting: twitter-text config v3 and
developer.x.com/en/docs/counting-characters (URLs 23, CJK 2, emoji 2, NFC first)."""

import pytest

from harness.xtext import MAX_WEIGHTED_CHARS, weighted_length


@pytest.mark.parametrize("text, n", [
    ("", 0),
    ("a" * 280, 280),
    ("é—’", 3),  # U+00E9, U+2014 and U+2019 sit in the light ranges
    ("…", 2),  # U+2026 is outside them (8208-8223 ends at U+201F)
    ("你好", 4),  # CJK counts 2 each
    ("こんにちは", 10),
    ("é", 1),  # NFC turns e + combining acute into é
])
def test_characters_count_by_x_ranges(text, n):
    assert weighted_length(text) == n


def test_140_cjk_characters_fill_a_post():
    assert weighted_length("字" * 140) == MAX_WEIGHTED_CHARS
    assert weighted_length("字" * 141) == 282


@pytest.mark.parametrize("text, n", [
    ("https://t.co/x", 23),
    ("https://www.espn.com/wnba/story/_/id/50189491/fever-forward-aliyah-boston-arthroscopic-knee-surgery", 23),
    ("read https://example.com/a and https://example.com/b", 5 + 23 + 5 + 23),
    ("see https://example.com/a.", 4 + 23 + 1),  # the trailing period isn't part of the link
    ("(https://example.com/a)", 1 + 23 + 1),
])
def test_every_link_counts_23(text, n):
    assert weighted_length(text) == n


@pytest.mark.parametrize("text", [
    "👍",
    "👍🏽",  # skin tone modifier
    "❤️",  # U+2764 with VS16
    "👨‍👩‍👧",  # three people joined by ZWJ
    "🇺🇸",  # two regional indicators
    "1️⃣",  # keycap
    "©️",
])
def test_an_emoji_counts_2_however_many_code_points(text):
    assert weighted_length(text) == 2


def test_lone_light_symbols_stay_light():
    assert weighted_length("© 1 #") == 5


def test_overflow_index_marks_where_the_cap_is_passed():
    from harness.xtext import overflow_index

    assert overflow_index("a" * 10, 8) == 8
    assert overflow_index("字" * 5, 6) == 3  # the fourth character takes the weight to 8
    assert overflow_index("short", 280) == 5


def test_gate_length_rule_uses_the_weighted_count():
    from harness.minds import gate

    length_only = {"length": gate.RULES["length"]}
    long_link = "this one is worth reading https://example.com/" + "x" * 300
    assert len(long_link) > 280 and gate.check(long_link, "post", rules=length_only).passed
    (finding,) = gate.check("字" * 150, "post", rules=length_only).findings
    assert finding.detail == "300 weighted chars (max 280 for post)"
    assert (finding.start, finding.end) == (140, 150)


def test_review_refuses_an_approved_edit_x_would_reject():
    from harness.review import check_edit

    check_edit("approved", "fine " + "https://example.com/" + "x" * 400)
    check_edit("killed", "字" * 200)  # only approvals hand anything off
    with pytest.raises(ValueError, match="weighs 400 as X counts it"):
        check_edit("approved", "字" * 200)
