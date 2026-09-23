import logging

import pytest
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
