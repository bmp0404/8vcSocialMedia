import pytest

from harness.minds.schema import (
    ABSTAIN_REASONS,
    POST_LANE,
    REPLY_LANE,
    ActionSchemaError,
    Offer,
    json_schema,
    parse_call,
)

URL = "https://x.com/AtlantaDream/status/2110808408366493714"
OFFER = Offer(target_urls=frozenset({URL}), target_handles=frozenset({"AtlantaDream"}))


def test_valid_reply_round_trips():
    call = parse_call({"tool": "reply", "target_url": URL, "text": " dream in 4 ", "rationale": "early"}, REPLY_LANE, OFFER)
    assert (call.tool, call.lane, call.args) == ("reply", REPLY_LANE, {"target_url": URL, "text": "dream in 4", "rationale": "early"})
    assert call.ships


def test_rationale_is_optional():
    assert parse_call({"tool": "reply", "target_url": URL, "text": "x"}, REPLY_LANE, OFFER).args == {"target_url": URL, "text": "x"}


def test_abstain_needs_a_known_reason():
    call = parse_call({"tool": "abstain", "reason": "no_angle", "detail": "nothing to add"}, REPLY_LANE)
    assert not call.ships
    with pytest.raises(ActionSchemaError, match=r"abstain.reason 'meh' is not one of"):
        parse_call({"tool": "abstain", "reason": "meh"}, REPLY_LANE)


def test_abstain_reason_codes_cover_every_stage():
    assert {"no_target", "gate_rejected", "judge_rejected", "budget_exhausted"} <= set(ABSTAIN_REASONS)


def test_target_url_must_have_been_offered():
    with pytest.raises(ActionSchemaError, match=r"reply.target_url 'https://www.espn.com/x' was not offered; offered: \['https://x.com"):
        parse_call({"tool": "reply", "target_url": "https://www.espn.com/x", "text": "hi"}, REPLY_LANE, OFFER)


def test_no_offer_means_no_target_is_valid():
    with pytest.raises(ActionSchemaError, match="offered: none"):
        parse_call({"tool": "quote", "target_url": URL, "text": "hi"}, REPLY_LANE)


def test_handles_compare_without_at_sign_or_case():
    call = parse_call({"tool": "follow", "target_handle": "@atlantadream"}, REPLY_LANE, OFFER)
    assert call.args["target_handle"] == "atlantadream"


def test_tool_from_the_other_lane_is_rejected():
    with pytest.raises(ActionSchemaError, match=r"tool 'post' is not allowed in the reply lane; allowed: \['abstain', 'follow', 'quote', 'reply'\]"):
        parse_call({"tool": "post", "text": "hi"}, REPLY_LANE)
    with pytest.raises(ActionSchemaError, match="tool 'reply' is not allowed in the post lane"):
        parse_call({"tool": "reply", "target_url": URL, "text": "hi"}, POST_LANE, OFFER)


def test_missing_and_empty_required_fields():
    with pytest.raises(ActionSchemaError, match="reply: missing required field 'text'"):
        parse_call({"tool": "reply", "target_url": URL}, REPLY_LANE, OFFER)
    with pytest.raises(ActionSchemaError, match=r"reply.text is empty"):
        parse_call({"tool": "reply", "target_url": URL, "text": "   "}, REPLY_LANE, OFFER)


def test_unexpected_field_is_rejected_not_dropped():
    with pytest.raises(ActionSchemaError, match=r"reply: unexpected field\(s\) \['hashtags'\]"):
        parse_call({"tool": "reply", "target_url": URL, "text": "hi", "hashtags": ["#WNBA"]}, REPLY_LANE, OFFER)


def test_wrong_type():
    with pytest.raises(ActionSchemaError, match="post.text must be str, got int"):
        parse_call({"tool": "post", "text": 5}, POST_LANE)


def test_not_an_object_and_missing_tool():
    with pytest.raises(ActionSchemaError, match="must be a JSON object, got list"):
        parse_call([], REPLY_LANE)
    with pytest.raises(ActionSchemaError, match=r"missing string field 'tool'; got keys \['text'\]"):
        parse_call({"text": "hi"}, REPLY_LANE)


def test_unknown_lane():
    with pytest.raises(ActionSchemaError, match="unknown lane 'dm'"):
        parse_call({"tool": "reply"}, "dm")


def test_mention_text_must_tag_the_handle():
    offer = Offer(target_handles=frozenset({"IndianaFever"}))
    assert parse_call({"tool": "mention", "target_handle": "IndianaFever", "text": "@IndianaFever re-sign her"}, POST_LANE, offer).ships
    with pytest.raises(ActionSchemaError, match="mention.text must contain @indianafever"):
        parse_call({"tool": "mention", "target_handle": "IndianaFever", "text": "re-sign her"}, POST_LANE, offer)


def test_ask_human_does_not_ship():
    call = parse_call({"tool": "ask_human", "question": "your take on the Boston surgery?"}, POST_LANE)
    assert not call.ships


def test_json_schema_lists_every_tool_and_forbids_extras():
    schema = json_schema(REPLY_LANE)
    tools = {v["properties"]["tool"]["const"] for v in schema["oneOf"]}
    assert tools == {"reply", "quote", "follow", "abstain"}
    assert all(v["additionalProperties"] is False for v in schema["oneOf"])
    abstain = next(v for v in schema["oneOf"] if v["properties"]["tool"]["const"] == "abstain")
    assert abstain["properties"]["reason"]["enum"] == sorted(ABSTAIN_REASONS)
