"""Strict tests for the knob registry and config hashing."""

import hashlib
import json

import pytest

from harness.iterate import knobs
from harness.iterate.knobs import Config, Knob, KnobError


def test_default_config_sets_every_registered_knob():
    assert set(knobs.DEFAULT.values) == set(knobs.BY_PATH)
    assert Config.build() == knobs.DEFAULT
    assert len(knobs.DEFAULT.hash) == 64 and knobs.DEFAULT.ref == f"cfg-{knobs.DEFAULT.hash[:12]}"


def test_hash_is_sha256_of_an_independently_built_canonical_form():
    cfg = knobs.DEFAULT
    values = {p: list(v) if isinstance(v, tuple) else v for p, v in cfg.values.items()}
    expected = hashlib.sha256(json.dumps({"schema": knobs.SCHEMA, "values": values}, sort_keys=True,
                                         separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
    assert cfg.hash == expected


def test_canonical_json_golden():
    assert knobs.canonical_json({"b": [2, 1], "a": (1, 2), "c": "é", "d": 1.5}) == \
        '{"a":[1,2],"b":[2,1],"c":"\\u00e9","d":1.5}'
    with pytest.raises(ValueError):
        knobs.canonical_json({"x": float("nan")})
    with pytest.raises(TypeError):
        knobs.canonical_json({"x": object()})


def test_hash_ignores_key_order_and_set_member_order():
    a = Config.build({"qualify.panel": "single_reader", "action.lanes": ["reply", "post"]})
    b = Config.build({"action.lanes": ("post", "reply"), "qualify.panel": "single_reader"})
    assert a.hash == b.hash
    assert a.get("action.lanes") == ("post", "reply")


def test_any_value_change_changes_the_hash():
    assert Config.build({"orient.narratives": 6}).hash != knobs.DEFAULT.hash
    assert Config.build({"action.lanes": ["reply"]}).hash != knobs.DEFAULT.hash


def test_validation_is_strict():
    with pytest.raises(KnobError, match="unknown knob"):
        Config.build({"action.temperature": 0.7})
    with pytest.raises(KnobError, match="not one of"):
        Config.build({"action.reply.generation": "freestyle"})
    with pytest.raises(KnobError, match="outside"):
        Config.build({"orient.narratives": 99})
    with pytest.raises(KnobError, match="integer"):
        Config.build({"orient.narratives": True})  # bool is an int subclass; refuse it
    with pytest.raises(KnobError, match="integer"):
        Config.build({"orient.narratives": 5.0})
    with pytest.raises(KnobError, match="duplicate"):
        Config.build({"action.lanes": ["post", "post"]})
    with pytest.raises(KnobError, match="not in"):
        Config.build({"action.lanes": ["post", "dm"]})
    with pytest.raises(KnobError, match="list"):
        Config.build({"action.lanes": "post"})


def test_float_and_bool_kinds():
    f = Knob("x.rate", float, 0.5, "x", "test", lo=0.0, hi=1.0)
    assert f.check(1) == 1.0 and isinstance(f.check(1), float)
    with pytest.raises(KnobError):
        f.check(True)
    with pytest.raises(KnobError):
        f.check(float("inf"))
    b = Knob("x.flag", bool, False, "x", "test")
    assert b.check(True) is True
    with pytest.raises(KnobError):
        b.check(1)


def test_with_changes_returns_a_new_config_and_diff_lists_only_changes():
    base = knobs.DEFAULT
    changed = base.with_changes({"qualify.model": knobs.GPT_SOL, "orient.narratives": 3})
    assert base.get("qualify.model") == knobs.SONNET  # unchanged
    assert knobs.diff(base, changed) == {
        "orient.narratives": {"from": 5, "to": 3},
        "qualify.model": {"from": knobs.SONNET, "to": knobs.GPT_SOL},
    }
    assert knobs.diff(changed, changed) == {}
    assert "qualify.model" in knobs.unified_diff(base, changed)


def test_json_round_trip_and_schema_check():
    cfg = knobs.DEFAULT.with_changes({"action.lanes": ["reply"]})
    assert Config.from_json(json.loads(json.dumps(cfg.to_json()))).hash == cfg.hash
    with pytest.raises(KnobError, match="schema"):
        Config.from_json({"schema": "iterate.config/v0", "values": {}})
    stale = cfg.to_json()
    stale["values"]["action.removed_knob"] = 1
    with pytest.raises(KnobError, match="unknown knob"):
        Config.from_json(stale)


def test_every_default_is_valid_and_readers_are_strings():
    for k in knobs.REGISTRY:
        assert k.check(k.default) == knobs.DEFAULT.values[k.path]
        assert isinstance(k.reader, str)
