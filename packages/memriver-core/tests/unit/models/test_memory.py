import re

import pytest
from memriver_core.models import (
    AccessContext,
    Memory,
    ProjectId,
    Scope,
    now,
    now_strictly_after,
    sanitize_name,
)

SOURCE = {"harness": "claude-code", "session": "s1", "method": "agent"}


def test_new_generates_ulid_and_timestamps():
    m = Memory.new(body="用户偏好中文回复", type="user", scope=Scope.global_(), source=SOURCE)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", m.id)  # ULID
    assert m.created == m.updated
    assert m.created.endswith("Z") or "+" in m.created
    assert m.sync is True and m.trust == "agent"


def test_now_emits_microseconds_so_consecutive_calls_can_be_ordered():
    # at second resolution two updates within the same wall second produced
    # identical strings and freshness ordering could not tell them apart.
    # The microsecond field is what makes ordering possible; a strict advance
    # is not asserted because the clock's own tick may be coarser than a call.
    stamp = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z"
    first = now()
    second = now()
    assert re.fullmatch(stamp, first)
    assert re.fullmatch(stamp, second)
    assert second >= first


def test_now_strictly_after_advances_even_when_the_clock_stands_still(monkeypatch):
    # the clock's own tick may be coarser than two consecutive updates, and it
    # can even step backwards; `updated` is the freshness sort key, so a
    # rewrite has to land strictly after the value it replaces regardless
    monkeypatch.setattr("memriver_core.models.memory.now",
                        lambda: "2026-08-29T10:00:00.000000Z")
    assert now_strictly_after("2026-08-29T10:00:00.000000Z") == "2026-08-29T10:00:00.000001Z"
    assert now_strictly_after("2026-08-29T10:00:00.000001Z") == "2026-08-29T10:00:00.000002Z"
    # a clock that jumped backwards must not drag `updated` back with it
    assert now_strictly_after("2027-01-01T00:00:00.000000Z") == "2027-01-01T00:00:00.000001Z"
    # ...and a clock that has genuinely moved on is what wins
    assert now_strictly_after("2020-01-01T00:00:00.000000Z") == "2026-08-29T10:00:00.000000Z"


@pytest.mark.parametrize("previous", ["not-a-date", "", "2026-08-29T10:00:00Z",
                                      "0001-01-01 00:00:00+14:00"])
def test_now_strictly_after_falls_back_to_the_clock_for_a_non_canonical_value(
        previous, monkeypatch):
    # nothing comparable to advance past: a stored value outside the canonical
    # form is a diagnostics finding, not a reason to refuse the update
    monkeypatch.setattr("memriver_core.models.memory.now",
                        lambda: "2026-08-29T10:00:00.000000Z")
    assert now_strictly_after(previous) == "2026-08-29T10:00:00.000000Z"


def test_invalid_type_rejected():
    with pytest.raises(ValueError):
        Memory.new(body="x", type="task", scope=Scope.global_(), source=SOURCE)


def test_new_accepts_caller_id():
    m = Memory.new(body="b", type="project", scope=Scope.global_(),
                    source={}, id="my-slug")
    assert m.id == "my-slug"


def test_new_generates_ulid_without_id():
    m = Memory.new(body="b", type="project", scope=Scope.global_(), source={})
    assert len(m.id) == 26


def test_old_types_rejected():
    with pytest.raises(ValueError):
        Memory.new(body="b", type="preference", scope=Scope.global_(), source={})


def test_invalid_trust_rejected():
    with pytest.raises(ValueError):
        Memory.new(body="b", type="user", scope=Scope.global_(), source={}, trust="bogus")


def test_description_is_stripped():
    m = Memory.new(body="b", type="user", scope=Scope.global_(), source={},
                    description="  a one-line recall cue  ")
    assert m.description == "a one-line recall cue"


def test_description_defaults_empty():
    m = Memory.new(body="b", type="user", scope=Scope.global_(), source={})
    assert m.description == ""


def test_scope_parse_and_storage_roundtrip():
    assert Scope.parse("global").to_storage() == "global"
    assert Scope.parse("project:abc-123").to_storage() == "project:abc-123"
    with pytest.raises(ValueError):
        Scope.parse("team:x")
    with pytest.raises(ValueError):
        Scope.parse("project:")


def test_access_context_visibility():
    assert AccessContext(project_id=None).visible_scopes() == (Scope.global_(),)
    ctx = AccessContext(project_id=ProjectId("abc-123"))
    assert ctx.visible_scopes() == (
        Scope.global_(),
        Scope.project(ProjectId("abc-123")),
    )


def test_memory_owns_typed_scope():
    memory = Memory.new(
        body="body",
        type="project",
        scope=Scope.project(ProjectId("abc-123")),
        source={"harness": "test", "method": "agent"},
    )
    assert memory.scope == Scope.project(ProjectId("abc-123"))


def test_sanitize_name_behavior_preserved():
    assert sanitize_name("mise-runtime-management") == "mise-runtime-management"
    assert sanitize_name("Mise Runtime_Mgmt!") == "mise-runtime-mgmt"
    assert sanitize_name("--weird--") == "weird"
    assert len(sanitize_name("a" * 200)) == 64


@pytest.mark.parametrize("name", ["", "!!!", "记忆", "記憶"])
def test_sanitize_name_unsalvageable(name):
    assert sanitize_name(name) is None
