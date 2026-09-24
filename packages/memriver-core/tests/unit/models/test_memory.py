import re
from datetime import UTC, datetime

import pytest
from memriver_core import models
from memriver_core.models import (
    ID_ALPHABET,
    ID_LENGTH,
    ID_RE,
    Memory,
    new_id,
    now,
    now_strictly_after,
    single_line,
)

SOURCE = {"harness": "claude-code", "method": "agent"}
PROJECT = "zzzzzzzzzz"


def test_new_generates_an_id_timestamps_and_keeps_the_project():
    m = Memory.new(body="  用户偏好中文回复 ", type="user", project_id=PROJECT, source=SOURCE)
    assert ID_RE.fullmatch(m.id)
    assert m.project_id == PROJECT
    assert m.created == m.updated
    assert m.body == "用户偏好中文回复"
    assert (m.trust, m.sync, m.description) == ("agent", True, "")


def test_new_never_takes_a_caller_id():
    with pytest.raises(TypeError):
        Memory.new(body="b", type="user", project_id=PROJECT, source={}, id=new_id())  # type: ignore[call-arg]


def test_new_memories_draw_fresh_ids():
    # 200 draws from 2**50: a repeat here would be a broken generator, not luck
    ids = {Memory.new(body="b", type="user", project_id=PROJECT, source={}).id for _ in range(200)}
    assert len(ids) == 200


@pytest.mark.parametrize("project_id", ["", "global", "demo-0123456789abcdef", "AAAAAAAAAA"])
def test_new_rejects_a_project_id_outside_the_id_rule(project_id):
    with pytest.raises(ValueError):
        Memory.new(body="b", type="user", project_id=project_id, source={})


@pytest.mark.parametrize("kwargs", [{"type": "task"}, {"type": "preference"},
                                    {"type": "user", "trust": "bogus"}])
def test_new_rejects_an_unknown_type_or_trust(kwargs):
    with pytest.raises(ValueError):
        Memory.new(body="b", project_id=PROJECT, source={}, **kwargs)


def test_description_is_stripped():
    m = Memory.new(body="b", type="user", project_id=PROJECT, source={}, description="  cue ")
    assert m.description == "cue"


def test_memory_has_exactly_its_fields():
    assert [f for f in Memory.__dataclass_fields__] == [
        "id", "project_id", "type", "source", "trust", "sync",
        "created", "updated", "description", "body", "version", "deleted_at"]


def test_scope_and_name_helpers_are_gone():
    for name in ("Scope", "ProjectId", "SearchHit", "IndexListing", "sanitize_name", "PROJECT_ID_RE"):
        assert not hasattr(models, name), name


def test_id_rule_is_ten_lowercase_crockford_characters():
    assert ID_ALPHABET == "0123456789abcdefghjkmnpqrstvwxyz" and ID_LENGTH == 10
    generated = [new_id() for _ in range(500)]
    assert all(ID_RE.fullmatch(i) for i in generated)
    assert set("".join(generated)) <= set(ID_ALPHABET)
    assert ID_RE.fullmatch("0123456789") and ID_RE.fullmatch("zzzzzzzzzz")
    for bad in ("AAAAAAAAAA", "aaaaaaaaa", "aaaaaaaaaaa", "aaaaaaaaai", "aaaaaaaaal",
                "aaaaaaaaao", "aaaaaaaaau", "aaaa-aaaaa", "01ARZ3NDEKTSV4RRFFQ69G5FAV"):
        assert ID_RE.fullmatch(bad) is None, bad


def test_now_emits_microseconds_so_consecutive_calls_can_be_ordered():
    first, second = now(), now()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", first)
    assert first <= second


def test_now_strictly_after_advances_even_when_the_clock_stands_still(monkeypatch):
    frozen = "2026-09-23T00:00:00.000000Z"
    monkeypatch.setattr(models.helpers, "now", lambda: frozen)
    assert now_strictly_after(frozen) == "2026-09-23T00:00:00.000001Z"
    assert now_strictly_after("2026-09-24T00:00:00.000000Z") == "2026-09-24T00:00:00.000001Z"


def test_now_strictly_after_falls_back_to_the_clock_for_a_non_canonical_value():
    stamp = now_strictly_after("yesterday")
    assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def test_single_line_collapses_control_and_line_separators():
    assert single_line("a\nb c\x00d   e\u2028f") == "a b c d e f"


def test_a_new_memory_starts_at_version_one_and_not_deleted():
    memory = Memory.new(body="b", type="user", project_id="aaaaaaaaaa", source={})
    assert (memory.version, memory.deleted_at) == (1, None)
