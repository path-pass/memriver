import logging
import time

import pytest
import yaml
from memriver_core.models import Memory
from memriver_core.repository.filesystem.markdown_codec import decode, encode

SOURCE = {"harness": "claude-code", "method": "agent"}
P = "aaaaaaaaaa"


def _m(**kw):
    kw.setdefault("body", "内容")
    kw.setdefault("type", "project")
    kw.setdefault("project_id", P)
    kw.setdefault("source", SOURCE)
    return Memory.new(**kw)


def test_roundtrip_keeps_all_ten_fields():
    m = _m(description="cue", sync=False, trust="user")
    assert decode(encode(m)) == m


def test_project_id_is_written_and_scope_is_not():
    text = encode(_m())
    assert f"project_id: {P}" in text
    assert "scope:" not in text


@pytest.mark.parametrize("bad", ["global", "project:demo", "AAAAAAAAAA", ""])
def test_a_stored_project_id_outside_the_id_rule_does_not_decode(bad):
    text = encode(_m()).replace(f"project_id: {P}", f"project_id: '{bad}'")
    with pytest.raises(ValueError):
        decode(text)


def test_a_stored_id_outside_the_id_rule_does_not_decode():
    m = _m()
    text = encode(m).replace(f"id: {m.id}", "id: 'AAAAAAAAAA'")
    with pytest.raises(ValueError):
        decode(text)


def test_a_file_without_project_id_does_not_decode():
    text = "\n".join(line for line in encode(_m()).splitlines()
                     if not line.startswith("project_id:"))
    with pytest.raises(KeyError):
        decode(text)


def test_unknown_type_reads_as_project_and_is_logged(caplog):
    text = encode(_m(type="user")).replace("type: user", "type: preference")
    with caplog.at_level(logging.WARNING):
        assert decode(text).type == "project"
    assert "coercing unknown type" in caplog.text


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("True", True), ("false", False),
                                               ("yes", False), ("on", False), ("'true'", True)])
def test_sync_is_parsed_strictly(raw, expected):
    text = encode(_m()).replace("sync: true", f"sync: {raw}")
    assert decode(text).sync is expected


def test_a_hand_edited_offset_timestamp_is_canonicalized():
    m = _m()
    text = encode(m).replace(f"created: '{m.created}'", "created: 2026-09-23T10:00:00+02:00")
    assert decode(text).created == "2026-09-23T08:00:00.000000Z"


def test_an_unparseable_timestamp_is_left_untouched():
    m = _m()
    text = encode(m).replace(f"updated: '{m.updated}'", "updated: yesterday")
    assert decode(text).updated == "yesterday"


def test_description_defaults_empty_and_is_stripped():
    assert decode(encode(_m())).description == ""
    text = encode(_m()).replace("description: ''", "description: '  cue  '")
    assert decode(text).description == "cue"


# 8 nested alias levels: ~600 bytes of YAML, 10**8 leaves if every alias were expanded
_ALIAS_BOMB = "\n".join(
    ["  l0: &l0 [" + ", ".join(["a"] * 10) + "]"]
    + [f"  l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]" for i in range(1, 8)])
# one 20k-char scalar aliased 2000 times: a ~26 KB file, a 40 MB value if every alias were expanded
_SCALAR_ALIAS = '  s: &s "' + "a" * 20_000 + '"\n  r: [' + ", ".join(["*s"] * 2000) + "]"


@pytest.mark.parametrize(("old", "new"), [
    ("description: cue", 'description: "\\udc80"'),
    ("harness: claude-code", 'harness: "\\udc80"'),
    ("harness: claude-code", 'harness: claude-code\n  tags: !!set {"\\udc80": null}'),
    ("harness: claude-code", 'harness: claude-code\n  blob: !!binary gA=='),
    ("trust: agent", 'trust: !!binary gA=='),
    ("harness: claude-code", 'harness: claude-code\n  r: &x [*x]'),  # a cycle has no JSON form
    ("harness: claude-code", 'harness: claude-code\n  r: &x {k: *x}'),
    ("harness: claude-code", 'harness: claude-code\n  a: &x [1, 2]\n  b: *x'),  # memriver never writes aliases
    pytest.param("harness: claude-code", "harness: claude-code\n" + _ALIAS_BOMB, id="alias-bomb"),
    pytest.param("harness: claude-code", "harness: claude-code\n" + _SCALAR_ALIAS, id="scalar-alias"),
    ("harness: claude-code", 'harness: claude-code\n  o: !!omap [{"\\udc80": 1}]'),
])
def test_an_unservable_stored_value_does_not_decode(old, new):
    text = encode(_m(description="cue")).replace(old, new)
    assert new in text
    start = time.monotonic()
    with pytest.raises((ValueError, yaml.YAMLError)):     # aliases fail at the YAML loader
        decode(text)
    assert time.monotonic() - start < 1.0


def test_a_surrogate_in_the_body_does_not_decode():
    with pytest.raises(ValueError):
        decode(encode(_m(body="plain")).replace("plain", "a\udc80b"))


def test_non_bmp_characters_still_decode():
    m = _m(description="cue \U0001F600", body="body \U0001F600")
    assert decode(encode(m)) == m
    escaped = encode(_m(description="cue")).replace("description: cue",
                                                    'description: "\\U0001F600"')
    assert decode(escaped).description == "\U0001F600"


_NOT_STORABLE = "memory cannot be stored: its fields must be plain values without shared references"


def test_a_shared_container_is_refused_by_encode_not_written_as_an_alias():
    shared = ["a", "b"]
    with pytest.raises(ValueError) as caught:
        encode(_m(source={"first": shared, "second": shared}))
    assert str(caught.value) == _NOT_STORABLE


def test_a_cyclic_value_is_refused_by_encode():
    cycle: list = []
    cycle.append(cycle)
    start = time.monotonic()
    with pytest.raises(ValueError) as caught:
        encode(_m(source={"harness": "claude-code", "r": cycle}))
    assert str(caught.value) == _NOT_STORABLE
    assert time.monotonic() - start < 1.0


def test_equal_but_separate_containers_still_encode_and_decode():
    m = _m(source={"harness": "claude-code", "first": ["a", "b"], "second": ["a", "b"]})
    assert decode(encode(m)) == m
