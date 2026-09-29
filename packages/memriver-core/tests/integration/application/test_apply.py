"""`MemoryService.apply` and the agent wrappers over a real store (spec §4.1).

Covers acceptance §10 items 1-3 for create, update and soft delete, and item 15.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from memriver_core.models import ProjectContext, ReadWriteSet, new_id
from memriver_core.models.changes import Create, SoftDelete, Update
from memriver_core.models.errors import (
    BatchConflict,
    ContentRejected,
    GlobalReadOnly,
    MemoryNotFound,
    ProjectNotFound,
    VersionConflict,
)

SECRET = "aws key AKIAIOSFODNN7EXAMPLE ok"          # from the secret-scanner tests
TABLES = ("memories", "memory_versions", "memory_sources", "changes", "change_steps",
          "memory_reads")


def _counts(world) -> dict[str, int]:
    return {table: world["sql"](f"SELECT count(*) FROM {table}")[0][0] for table in TABLES}


def _owners(world, memory_id: str) -> list[tuple]:
    """Each version with the change and step that own it."""
    return world["sql"](
        "SELECT v.version, v.deleted, c.changed_by, c.changed_via, s.op, s.before_version "
        "FROM memory_versions v JOIN changes c ON c.change_id = v.change_id "
        "JOIN change_steps s ON s.change_id = c.change_id AND s.memory_id = v.memory_id "
        "AND s.after_version = v.version WHERE v.memory_id = ? ORDER BY v.version", memory_id)


def _row_is_latest_version(world, memory_id: str) -> bool:
    row = world["sql"]("SELECT type, trust, sync, description, body, deleted_at IS NOT NULL "
                       "FROM memories WHERE id = ?", memory_id)
    latest = world["sql"](
        "SELECT v.type, v.trust, v.sync, v.description, v.body, v.deleted "
        "FROM memory_versions v JOIN memories m ON m.id = v.memory_id AND m.version = v.version "
        "WHERE v.memory_id = ?", memory_id)
    return len(row) == 1 and row == latest


def _make_global(world, project_id: str) -> None:
    """`project_id` becomes the global project behind every context already built."""
    world["sql"]("UPDATE projects SET is_global = 0 WHERE id = ?", world["global"])
    world["sql"]("UPDATE projects SET root = NULL, is_global = 1 WHERE id = ?", project_id)


def _record(world, context=None, content="uv manages python", description="cue"):
    return world["memory"].record(content=content, type="project", sync=True, harness="codex",
                                  description=description, context=context or world["context"])


# --- item 1: versions -------------------------------------------------------

def test_every_write_path_creates_one_version_owned_by_one_change(world):
    memory = world["memory"]
    created = memory.apply([Create(world["mine"], "project", "cue", "v1")],
                           changed_by="human", changed_via="cli")
    memory_id = created.steps[0].memory_id
    assert (created.changed_by, created.changed_via, created.step_count, created.undoes) == \
        ("human", "cli", 1, None)
    assert memory.update(memory_id, "v2", world["context"], expected_version=1).version == 2
    assert memory.delete(memory_id, world["context"], expected_version=2) == 3
    assert _owners(world, memory_id) == [(1, 0, "human", "cli", "create", None),
                                         (2, 0, "mcp", None, "update", 1),
                                         (3, 1, "mcp", None, "soft_delete", 2)]
    assert _row_is_latest_version(world, memory_id)

    recorded = _record(world)
    assert recorded.source == {"harness": "codex", "method": "mcp"}
    assert _owners(world, recorded.id) == [(1, 0, "mcp", "codex", "create", None)]
    assert _row_is_latest_version(world, recorded.id)


def test_sources_are_carried_to_every_new_version(world):
    source, derived = world["create"]("the source"), world["create"]("the derived")
    world["sql"]("INSERT INTO memory_sources (memory_id, version, source_id, source_version) "
                 "VALUES (?, 1, ?, 1)", derived, source)
    world["memory"].apply([Update(derived, 1, body="edited")], changed_by="human")
    world["memory"].apply([SoftDelete(derived, 2)], changed_by="human")
    assert world["sql"]("SELECT version, source_id, source_version FROM memory_sources "
                        "WHERE memory_id = ? ORDER BY version", derived) == \
        [(1, source, 1), (2, source, 1), (3, source, 1)]


def test_agents_never_see_history_or_deleted_rows(world):
    memory, context = world["memory"], world["context"]
    written = _record(world, content="first words")
    memory.update(written.id, "second words", context, expected_version=1)
    assert memory.search("first", context) == []
    [hit] = memory.search("second", context)
    assert (hit.id, hit.version, hit.body) == (written.id, 2, "second words")
    memory.delete(written.id, context, expected_version=2)
    assert memory.search("second", context) == []
    assert written.id not in memory.index(context)


def test_a_soft_delete_moves_updated_like_every_state_change(world):
    memory_id = world["create"]()
    before = world["sql"]("SELECT updated FROM memories WHERE id = ?", memory_id)[0][0]
    world["memory"].apply([SoftDelete(memory_id, 1)], changed_by="human")
    updated, deleted_at = world["sql"]("SELECT updated, deleted_at FROM memories WHERE id = ?",
                                       memory_id)[0]
    assert updated > before and deleted_at == updated


# --- item 2: the behavior matrix (create, update, soft delete) ---------------

@pytest.mark.parametrize("op", [lambda m: Update(m, 2, body="x"), lambda m: SoftDelete(m, 2)])
def test_a_target_at_another_version_is_a_version_conflict(world, op):
    memory_id = world["create"]()
    with pytest.raises(BatchConflict) as excinfo:
        world["memory"].apply([op(memory_id)], changed_by="human")
    assert (excinfo.value.index, excinfo.value.memory_id, excinfo.value.reason) == \
        (0, memory_id, "version")


@pytest.mark.parametrize("op", [lambda m: Update(m, 2, body="x"), lambda m: SoftDelete(m, 2)])
def test_an_update_or_soft_delete_of_a_deleted_memory_is_refused(world, op):
    memory_id = world["create"]()
    world["memory"].apply([SoftDelete(memory_id, 1)], changed_by="human")
    with pytest.raises(BatchConflict) as excinfo:
        world["memory"].apply([op(memory_id)], changed_by="human")
    assert excinfo.value.reason == "deleted"


@pytest.mark.parametrize("op", [lambda m: Update(m, 1, body=" a fact "),
                                lambda m: Update(m, 1, description="cue", body="a fact"),
                                lambda m: Update(m, 1)])
def test_a_result_equal_to_the_current_state_is_refused(world, op):
    memory_id = world["create"]()
    before = _counts(world)
    with pytest.raises(BatchConflict) as excinfo:
        world["memory"].apply([op(memory_id)], changed_by="human")
    assert (excinfo.value.memory_id, excinfo.value.reason) == (memory_id, "same-state")
    assert _counts(world) == before


def test_the_same_memory_twice_in_one_batch_is_a_value_error(world):
    memory_id = world["create"]()
    before = _counts(world)
    with pytest.raises(ValueError):
        world["memory"].apply([Update(memory_id, 1, body="x"), SoftDelete(memory_id, 1)],
                              changed_by="human")
    assert _counts(world) == before


def test_a_missing_target_or_project_is_refused(world):
    missing = new_id()
    with pytest.raises(BatchConflict) as excinfo:
        world["memory"].apply([Update(missing, 1, body="x")], changed_by="human")
    assert (excinfo.value.memory_id, excinfo.value.reason) == (missing, "missing")
    with pytest.raises(ProjectNotFound):
        world["memory"].apply([Create(missing, "project", "", "b")], changed_by="human")


def test_a_read_at_or_after_unread_since_refuses_the_soft_delete(world):
    memory_id = world["create"]()
    world["sql"]("UPDATE memories SET last_read_at = '2026-09-27T10:00:00.000000Z' WHERE id = ?",
                 memory_id)
    with pytest.raises(BatchConflict) as excinfo:
        world["memory"].apply(
            [SoftDelete(memory_id, 1, unread_since="2026-09-27T10:00:00.000000Z")],
            changed_by="human")
    assert excinfo.value.reason == "read-since"
    change = world["memory"].apply(
        [SoftDelete(memory_id, 1, unread_since="2026-09-27T10:00:00.000001Z")],
        changed_by="human")
    assert change.steps[0].after_version == 2


def test_the_content_policy_and_the_body_limit_refuse_creates_and_updates(world):
    memory, memory_id = world["memory"], world["create"]()
    before = _counts(world)
    with pytest.raises(ContentRejected) as created:
        memory.apply([Create(world["mine"], "project", "cue", SECRET)], changed_by="human")
    with pytest.raises(ContentRejected) as updated:
        memory.apply([Update(memory_id, 1, body=SECRET)], changed_by="human")
    with pytest.raises(ContentRejected) as described:
        memory.apply([Update(memory_id, 1, description=SECRET)], changed_by="human")
    with pytest.raises(ContentRejected) as too_large:
        memory.apply([Create(world["mine"], "project", "", "x" * 8001)], changed_by="human")
    assert created.value.memory_id is None
    assert updated.value.memory_id == described.value.memory_id == memory_id
    assert created.value.rule_id == updated.value.rule_id not in ("", "empty", "too-large")
    assert too_large.value.rule_id == "too-large"
    assert SECRET not in str(created.value)
    assert _counts(world) == before


def test_a_soft_delete_of_a_memory_whose_text_now_hits_the_policy_is_refused(world, session):
    """Spec §0.2: every resulting state passes the policy, a deleted one too; such a memory
    goes by the cascade hard delete."""
    memory_id = _record(world, session["context"]).id
    world["sql"]("UPDATE memories SET body = ? WHERE id = ?", SECRET, memory_id)
    world["sql"]("UPDATE memory_versions SET body = ? WHERE memory_id = ?", SECRET, memory_id)
    before = _counts(world)
    with pytest.raises(ContentRejected) as by_apply:
        world["memory"].apply([SoftDelete(memory_id, 1)], changed_by="human")
    with pytest.raises(ContentRejected) as by_wrapper:
        world["memory"].delete(memory_id, session["context"], expected_version=1)
    assert by_apply.value.rule_id == by_wrapper.value.rule_id != ""
    assert by_apply.value.memory_id == by_wrapper.value.memory_id == memory_id
    assert _counts(world) == before


def test_a_conflict_on_the_last_operation_leaves_no_version_source_change_or_step(world):
    first, second = world["create"]("first"), world["create"]("second")
    before = _counts(world)
    with pytest.raises(BatchConflict) as excinfo:
        world["memory"].apply([Create(world["mine"], "project", "", "new"),
                               Update(first, 1, body="changed"),
                               SoftDelete(second, 7)], changed_by="human")
    assert (excinfo.value.index, excinfo.value.memory_id) == (2, second)
    assert _counts(world) == before
    assert world["sql"]("SELECT body, version FROM memories WHERE id = ?", first) == \
        [("first", 1)]


def test_one_batch_is_one_change_with_a_step_per_memory(world):
    first, second = world["create"]("first"), world["create"]("second")
    change = world["memory"].apply([Create(world["mine"], "project", "", "merged"),
                                    Update(first, 1, body="changed"), SoftDelete(second, 1)],
                                   changed_by="human")
    assert change.step_count == 3
    assert [(s.step, s.op, s.before_version, s.after_version) for s in change.steps] == \
        [(1, "create", None, 1), (2, "update", 1, 2), (3, "soft_delete", 1, 2)]
    assert world["sql"]("SELECT step_count FROM changes WHERE change_id = ?",
                        change.change_id) == [(3,)]
    assert world["sql"]("SELECT count(*) FROM change_steps WHERE change_id = ?",
                        change.change_id) == [(3,)]


def test_global_is_an_ordinary_target_on_this_path_while_the_wrapper_refuses_it(world):
    memory = world["memory"]
    global_id = world["create"]("a principle", project_id=world["global"])
    assert memory.apply([Update(global_id, 1, body="a sharper principle")],
                        changed_by="human").steps[0].after_version == 2
    with pytest.raises(GlobalReadOnly):
        memory.update(global_id, "agent edit", world["context"], expected_version=2)
    as_global = ProjectContext("registered", "", ReadWriteSet(project_id=world["global"],
                                                              global_project_id=world["global"]))
    with pytest.raises(GlobalReadOnly):
        _record(world, as_global)


# --- item 3: the wrappers -----------------------------------------------------

def test_the_wrappers_refuse_another_projects_memory_when_called_directly(world):
    theirs = _record(world, world["other_context"])
    with pytest.raises(MemoryNotFound):
        world["memory"].update(theirs.id, "mine now", world["context"], expected_version=1)
    with pytest.raises(MemoryNotFound):
        world["memory"].delete(theirs.id, world["context"], expected_version=1)


def test_a_project_made_global_after_the_context_was_built_is_refused_in_the_transaction(
        world):
    written = _record(world)
    _make_global(world, world["mine"])
    before = _counts(world)
    with pytest.raises(GlobalReadOnly):
        _record(world, content="another fact")
    with pytest.raises(GlobalReadOnly):
        world["memory"].update(written.id, "changed", world["context"], expected_version=1)
    with pytest.raises(GlobalReadOnly):
        world["memory"].delete(written.id, world["context"], expected_version=1)
    assert _counts(world) == before


def test_a_policy_violation_is_refused_identically_through_a_wrapper_and_apply(world):
    memory, memory_id = world["memory"], world["create"]()
    with pytest.raises(ContentRejected) as by_record:
        _record(world, content=SECRET)
    with pytest.raises(ContentRejected) as by_update:
        memory.update(memory_id, SECRET, world["context"], expected_version=1)
    with pytest.raises(ContentRejected) as by_apply:
        memory.apply([Update(memory_id, 1, body=SECRET)], changed_by="human")
    assert by_record.value.rule_id == by_update.value.rule_id == by_apply.value.rule_id != ""


def test_apply_never_marks_a_session_saved_and_record_does(world, session):
    world["memory"].apply([Create(world["mine"], "project", "", "from elsewhere")],
                          changed_by="plugin", changed_via="codex")
    assert session["watermark"]() == (1, 0)
    _record(world, session["context"])
    assert session["watermark"]() == (1, 1)


def test_writes_leave_read_facts_unchanged(world):
    written = _record(world)
    world["sql"]("UPDATE memories SET last_read_at = '2026-09-27T10:00:00.000000Z' WHERE id = ?",
                 written.id)
    world["memory"].update(written.id, "changed", world["context"], expected_version=1)
    world["memory"].apply([SoftDelete(written.id, 2)], changed_by="human")
    assert world["sql"]("SELECT last_read_at FROM memories WHERE id = ?", written.id) == \
        [("2026-09-27T10:00:00.000000Z",)]
    assert _counts(world)["memory_reads"] == 0


# --- item 15: the wrapper no-op -------------------------------------------------

def test_a_same_text_update_returns_the_current_version_writes_nothing_and_marks_saved(
        world, session):
    written = _record(world, session["context"])
    session["prompt"]()
    assert session["watermark"]() == (2, 1)
    before = _counts(world)
    for description in ("cue", None):
        same = world["memory"].update(written.id, " uv manages python ", session["context"],
                                      expected_version=1, description=description)
        assert (same.version, same.updated, same.body) == (1, written.updated,
                                                           "uv manages python")
    assert _counts(world) == before
    assert session["watermark"]() == (2, 2)


def test_same_text_with_a_stale_version_is_refused(world, session):
    written = _record(world, session["context"])
    world["memory"].update(written.id, "v2", session["context"], expected_version=1)
    session["prompt"]()
    with pytest.raises(VersionConflict):
        world["memory"].update(written.id, "v2", session["context"], expected_version=1)
    assert session["watermark"]() == (2, 1)          # a failure never marks saved


def test_same_text_after_the_project_became_global_is_refused(world):
    written = _record(world)
    _make_global(world, world["mine"])
    with pytest.raises(GlobalReadOnly):
        world["memory"].update(written.id, "uv manages python", world["context"],
                               expected_version=1)


def test_delete_never_marks_saved(world, session):
    written = _record(world, session["context"])
    session["prompt"]()
    world["memory"].delete(written.id, session["context"], expected_version=1)
    assert session["watermark"]() == (2, 1)


# --- the wrappers answer with what they wrote, not with a later read ----------------

def _remove_row(world, memory_id: str) -> None:
    """What a cascade hard delete does to one uncited memory, by a peer."""
    with closing(sqlite3.connect(world["store"] / "memriver.db", isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))


@pytest.mark.parametrize("peer", ["update", "soft-delete", "hard-delete"])
def test_a_peer_write_after_the_commit_does_not_change_what_the_wrapper_returns(
        world, session, monkeypatch, peer):
    memory = world["memory"]
    written = _record(world, session["context"], content="first words")

    def peer_writes(context):
        # runs after the wrapper's own transaction committed, before it returns
        if peer == "update":
            memory.apply([Update(written.id, 2, body="a peer's words")], changed_by="human")
        elif peer == "soft-delete":
            memory.apply([SoftDelete(written.id, 2)], changed_by="human")
        else:
            _remove_row(world, written.id)

    monkeypatch.setattr(memory, "_mark_saved", peer_writes)
    returned = memory.update(written.id, "my words", session["context"], expected_version=1)
    assert (returned.id, returned.version, returned.body, returned.deleted_at) == \
        (written.id, 2, "my words", None)


def test_a_peer_write_after_a_checked_no_op_does_not_change_what_it_returns(
        world, session, monkeypatch):
    memory = world["memory"]
    written = _record(world, session["context"], content="same words")
    monkeypatch.setattr(memory, "_mark_saved", lambda context: memory.apply(
        [Update(written.id, 1, body="a peer's words")], changed_by="human"))
    returned = memory.update(written.id, "same words", session["context"], expected_version=1)
    assert (returned.version, returned.body, returned.updated) == \
        (1, "same words", written.updated)


def test_a_peer_write_after_a_record_does_not_change_what_it_returns(
        world, session, monkeypatch):
    memory, peer_ids = world["memory"], []

    def peer_soft_deletes(context):
        [row] = world["sql"]("SELECT id FROM memories WHERE body = 'recorded words'")
        peer_ids.append(row[0])
        memory.apply([SoftDelete(row[0], 1)], changed_by="human")

    monkeypatch.setattr(memory, "_mark_saved", peer_soft_deletes)
    returned = _record(world, session["context"], content="recorded words")
    assert (returned.id, returned.version, returned.deleted_at) == (peer_ids[0], 1, None)


# --- a global memory is soft-deleted through apply; there is no second entry point ----

def test_there_is_no_separate_global_delete(world):
    from memriver_core.application.memory import MemoryService
    from memriver_core.repository.protocol import MemoryStore
    from memriver_core.repository.sqlite import SqliteMemoryStore

    assert not any(hasattr(cls, "delete_global")
                   for cls in (MemoryService, MemoryStore, SqliteMemoryStore))


def test_apply_soft_deletes_a_global_memory_as_human(world):
    global_id = world["create"]("a principle", project_id=world["global"])
    world["memory"].apply([SoftDelete(global_id, 1)], changed_by="human")
    assert _owners(world, global_id)[-1] == (2, 1, "human", None, "soft_delete", 1)


# --- an unaddressable id is a missing one, not storage damage (agent write entry) ----

def test_update_or_delete_with_an_unaddressable_id_is_not_found(world):
    surrogate = "\udc80" * 10        # a lone surrogate: legal in a Python str, not in UTF-8
    before = _counts(world)
    with pytest.raises(MemoryNotFound):
        world["memory"].update(surrogate, "x", world["context"], expected_version=1)
    with pytest.raises(MemoryNotFound):
        world["memory"].delete(surrogate, world["context"], expected_version=1)
    assert _counts(world) == before
