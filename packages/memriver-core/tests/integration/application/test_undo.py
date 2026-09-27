"""Undo over a real store (spec §4.1): acceptance §10 item 4, item 14's round trip, item 3."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from memriver_core.models.changes import Create, SoftDelete, SourceRef, Update
from memriver_core.models.errors import ContentRejected, UndoRefused

SECRET = "aws key AKIAIOSFODNN7EXAMPLE ok"          # from the secret-scanner tests


def _state(world, memory_id: str) -> tuple:
    return world["sql"]("SELECT version, body, deleted_at IS NOT NULL FROM memories "
                        "WHERE id = ?", memory_id)[0]


def _counts(world) -> list[int]:
    return [world["sql"](f"SELECT count(*) FROM {table}")[0][0]
            for table in ("memory_versions", "changes", "change_steps")]


def _undo(world, change_id: str):
    return world["memory"].undo(change_id, changed_by="human", changed_via="cli")


def _merge(world):
    alpha, beta = world["create"]("alpha"), world["create"]("beta")
    change = world["memory"].apply(
        [Create(world["mine"], "project", "merged", "alpha and beta",
                sources=(SourceRef(alpha, 1), SourceRef(beta, 1))),
         SoftDelete(alpha, 1), SoftDelete(beta, 1)], changed_by="human")
    return change, change.steps[0].memory_id, alpha, beta


def _remove_rows(world, *memory_ids: str) -> None:
    """What the cascade hard delete does to the rows: memories go, their versions,
    sources, reads and steps with them, change rows stay."""
    with closing(sqlite3.connect(world["store"] / "memriver.db", isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        conn.execute(f"DELETE FROM memories WHERE id IN ({', '.join('?' for _ in memory_ids)})",
                     memory_ids)
        conn.execute("COMMIT")


# --- each op's inverse ---------------------------------------------------------

def test_undoing_a_create_soft_deletes_the_memory(world):
    change = world["memory"].apply([Create(world["mine"], "project", "cue", "made")],
                                   changed_by="human")
    memory_id = change.steps[0].memory_id
    undo = _undo(world, change.change_id)
    assert (undo.undoes, undo.changed_by, undo.changed_via, undo.step_count) == \
        (change.change_id, "human", "cli", 1)
    assert [(s.op, s.memory_id, s.before_version, s.after_version) for s in undo.steps] == \
        [("soft_delete", memory_id, 1, 2)]
    assert _state(world, memory_id) == (2, "made", 1)
    assert world["sql"]("SELECT undoes FROM changes WHERE change_id = ?",
                        undo.change_id) == [(change.change_id,)]


def test_undoing_an_update_a_soft_delete_and_a_restore_restores_the_version_before(world):
    memory, memory_id = world["memory"], world["create"]("v1")
    update = memory.apply([Update(memory_id, 1, body="v2")], changed_by="human")
    assert [(s.op, s.before_version, s.after_version)
            for s in _undo(world, update.change_id).steps] == [("restore", 2, 3)]
    assert _state(world, memory_id) == (3, "v1", 0)

    delete = memory.apply([SoftDelete(memory_id, 3)], changed_by="human")
    _undo(world, delete.change_id)
    assert _state(world, memory_id) == (5, "v1", 0)

    restore = memory.restore(memory_id, 2, expected_version=5, changed_by="human")
    assert _state(world, memory_id) == (6, "v2", 0)
    _undo(world, restore.change_id)
    assert _state(world, memory_id) == (7, "v1", 0)


def test_a_merge_its_undo_and_the_undo_of_that_undo_round_trip_with_rising_versions(world):
    merge, merged, alpha, beta = _merge(world)
    undo = _undo(world, merge.change_id)
    assert [_state(world, m) for m in (merged, alpha, beta)] == \
        [(2, "alpha and beta", 1), (3, "alpha", 0), (3, "beta", 0)]

    # the originals moved on; the redo still restores the merged entry's citation of
    # alpha@1 and beta@1 (restored references), which a new citation could not make
    redo = _undo(world, undo.change_id)
    assert redo.undoes == undo.change_id
    assert [_state(world, m) for m in (merged, alpha, beta)] == \
        [(3, "alpha and beta", 0), (4, "alpha", 1), (4, "beta", 1)]
    assert world["sql"]("SELECT source_id, source_version FROM memory_sources "
                        "WHERE memory_id = ? AND version = 3 ORDER BY source_id", merged) == \
        sorted([(alpha, 1), (beta, 1)])


# --- refusals --------------------------------------------------------------------------

def test_an_old_change_after_its_undo_is_refused(world):
    merge, merged, alpha, beta = _merge(world)
    _undo(world, merge.change_id)
    with pytest.raises(UndoRefused) as excinfo:
        _undo(world, merge.change_id)
    assert (excinfo.value.reason, excinfo.value.memory_ids) == \
        ("changed", tuple(sorted((merged, alpha, beta))))


def test_content_changed_back_to_the_same_text_still_refuses(world):
    memory, memory_id = world["memory"], world["create"]("same")
    first = memory.apply([Update(memory_id, 1, body="other")], changed_by="human")
    memory.apply([Update(memory_id, 2, body="same")], changed_by="human")
    with pytest.raises(UndoRefused) as excinfo:
        _undo(world, first.change_id)
    assert (excinfo.value.reason, excinfo.value.memory_ids) == ("changed", (memory_id,))


def test_a_read_does_not_refuse_an_undo(world):
    memory, memory_id = world["memory"], world["create"]("v1")
    change = memory.apply([Update(memory_id, 1, body="v2")], changed_by="human")
    memory.read(memory_id, world["context"], harness="codex")
    _undo(world, change.change_id)
    assert _state(world, memory_id) == (3, "v1", 0)


def test_one_changed_member_refuses_the_whole_change_and_writes_nothing(world):
    merge, merged, alpha, _ = _merge(world)
    world["memory"].apply([Update(merged, 1, body="edited")], changed_by="human")
    before = _counts(world)
    with pytest.raises(UndoRefused) as excinfo:
        _undo(world, merge.change_id)
    assert (excinfo.value.reason, excinfo.value.memory_ids) == ("changed", (merged,))
    assert _counts(world) == before
    assert _state(world, alpha) == (2, "alpha", 1)


def test_one_or_all_members_hard_deleted_refuses_while_an_unrelated_change_is_undone(world):
    merge, merged, alpha, beta = _merge(world)
    unrelated_id = world["create"]("unrelated")
    unrelated = world["memory"].apply([Update(unrelated_id, 1, body="edited")],
                                      changed_by="human")
    _remove_rows(world, merged)                       # one member: it cites, nothing cites it
    with pytest.raises(UndoRefused) as one:
        _undo(world, merge.change_id)
    assert (one.value.reason, one.value.memory_ids) == ("hard-deleted", ())
    _remove_rows(world, alpha, beta)                  # all members
    with pytest.raises(UndoRefused) as every:
        _undo(world, merge.change_id)
    assert every.value.reason == "hard-deleted"
    assert world["sql"]("SELECT step_count FROM changes WHERE change_id = ?",
                        merge.change_id) == [(3,)]    # the change row stays
    _undo(world, unrelated.change_id)
    assert _state(world, unrelated_id) == (3, "unrelated", 0)


def test_a_real_hard_delete_of_one_member_refuses_the_undo_and_keeps_the_change_row(world):
    maintenance = world["services"].maintenance
    merge, merged, _alpha, _beta = _merge(world)
    plan = maintenance.plan_hard_delete(merged)
    assert maintenance.hard_delete(merged, expected=plan.expected) == [merged]
    with pytest.raises(UndoRefused) as excinfo:
        _undo(world, merge.change_id)
    assert excinfo.value.reason == "hard-deleted"
    assert world["sql"]("SELECT step_count FROM changes WHERE change_id = ?",
                        merge.change_id) == [(3,)]        # the change row stays


def test_a_policy_hit_in_the_restored_content_refuses_the_undo(world):
    memory, memory_id = world["memory"], world["create"]("clean")
    world["sql"]("UPDATE memory_versions SET body = ? WHERE memory_id = ? AND version = 1",
                 SECRET, memory_id)
    change = memory.apply([Update(memory_id, 1, body="edited")], changed_by="human")
    before = _counts(world)
    with pytest.raises(ContentRejected) as by_undo:
        _undo(world, change.change_id)
    with pytest.raises(ContentRejected) as by_apply:           # item 3: identically
        memory.apply([Update(memory_id, 2, body=SECRET)], changed_by="human")
    assert (by_undo.value.rule_id, by_undo.value.memory_id) == \
        (by_apply.value.rule_id, memory_id)
    assert _counts(world) == before


def test_an_undo_back_to_a_deleted_version_whose_text_hits_the_policy_is_refused(world):
    """Spec §0.2: a resulting deleted state passes the policy too."""
    memory, memory_id = world["memory"], world["create"]("clean")
    memory.apply([SoftDelete(memory_id, 1)], changed_by="human")              # v2, deleted
    world["sql"]("UPDATE memory_versions SET body = ? WHERE memory_id = ? AND version = 2",
                 SECRET, memory_id)
    undelete = memory.restore(memory_id, 1, expected_version=2, changed_by="human")   # v3
    before = _counts(world)
    with pytest.raises(ContentRejected) as excinfo:
        _undo(world, undelete.change_id)
    assert excinfo.value.memory_id == memory_id and excinfo.value.rule_id
    assert _counts(world) == before
    assert _state(world, memory_id) == (3, "clean", 0)


def test_an_unknown_change_is_not_found(world):
    with pytest.raises(UndoRefused) as excinfo:
        _undo(world, "zzzzzzzzzz")
    assert excinfo.value.reason == "not-found"


def test_change_returns_the_change_with_its_stored_steps(world):
    merge, merged, alpha, beta = _merge(world)
    assert world["memory"].change(merge.change_id) == merge
    _remove_rows(world, merged)
    partial = world["memory"].change(merge.change_id)
    assert (partial.step_count, [s.memory_id for s in partial.steps]) == (3, [alpha, beta])
    assert world["memory"].change("zzzzzzzzzz") is None
