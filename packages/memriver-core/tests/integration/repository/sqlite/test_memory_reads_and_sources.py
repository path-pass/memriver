"""The SQLite memory store's dream-era duties: read records, and the provenance
rules a hard delete has to respect."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from memriver_core.models import Memory, Project, ReadWriteSet, now
from memriver_core.models.errors import (
    MemoryNotFound,
    MemoryReferenced,
    ProjectUnavailable,
    StorageFailure,
    VersionConflict,
)
from memriver_core.repository.sqlite import SqliteMemoryStore, SqliteProjectStore

SOURCE = {"harness": "test", "method": "agent"}


@pytest.fixture
def world(tmp_path):
    store, home, work = tmp_path / "store", tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    project_store = SqliteProjectStore(store, home=home, busy_timeout_ms=2000)
    global_id = project_store.ensure_global()
    project = Project.new("demo", max_chars=120)
    project_store.create(project, project_store.plan_root(str(work), None))
    memory_store = SqliteMemoryStore(store, busy_timeout_ms=2000)
    read_write_set = ReadWriteSet(project_id=project.id, global_project_id=global_id)
    return {"store": store, "memory_store": memory_store, "global": global_id,
            "project": project.id, "read_write_set": read_write_set}


def _record(world, body: str = "fact") -> Memory:
    memory = Memory.new(body=body, type="project", project_id=world["project"], source=SOURCE,
                        description="cue")
    world["memory_store"].record(memory, world["read_write_set"])
    return memory


def _sql(world, statement: str, *params) -> list[tuple]:
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        return conn.execute(statement, params).fetchall()


def test_touch_read_records_one_read_with_the_version_harness_and_session(world):
    memory = _record(world)
    world["memory_store"].touch_read(memory.id, "2026-09-25T00:00:01.000000Z",
                                     memory_version=1, harness="claude-code", session_id="s-1")
    assert _sql(world, "SELECT * FROM memory_reads") == [
        (memory.id, 1, "2026-09-25T00:00:01.000000Z", "claude-code", "s-1")]
    seen = world["memory_store"].read(memory.id, world["read_write_set"])
    assert (seen.last_read_at, seen.version) == ("2026-09-25T00:00:01.000000Z", 1)


def test_touch_read_of_a_deleted_or_unknown_memory_records_nothing(world):
    memory = _record(world)
    world["memory_store"].delete(memory.id, world["read_write_set"], expected_version=1,
                                 hard=False)
    world["memory_store"].touch_read(memory.id, now(), memory_version=1, harness="codex")
    assert _sql(world, "SELECT count(*) FROM memory_reads") == [(0,)]


def test_touch_read_records_the_version_it_is_given_not_the_rows_current_one(world):
    memory = _record(world)
    world["memory_store"].update(memory.id, world["read_write_set"], expected_version=1,
                                 body="moved on", description=None)
    world["memory_store"].touch_read(memory.id, now(), memory_version=1, harness="codex")
    assert _sql(world, "SELECT memory_version FROM memory_reads") == [(1,)]


def test_touch_read_of_a_deleted_memory_does_not_run_the_prune(world):
    """The retention prune must ride on an actual insert: a no-op touch_read (an
    unknown or deleted id) must not trim another, still-active memory's history."""
    memory = _record(world)
    other = _record(world, "kept")
    world["memory_store"].touch_read(other.id, "2020-01-01T00:00:00.000000Z", memory_version=1,
                                     harness="codex")
    world["memory_store"].delete(memory.id, world["read_write_set"], expected_version=1,
                                 hard=False)
    world["memory_store"].touch_read(memory.id, now(), memory_version=1, harness="codex",
                                     prune_before="2026-01-01T00:00:00.000000Z")
    assert _sql(world, "SELECT count(*) FROM memory_reads") == [(1,)]


def test_prune_before_drops_older_reads_in_the_same_write(world):
    memory = _record(world)
    world["memory_store"].touch_read(memory.id, "2026-01-01T00:00:00.000000Z", memory_version=1,
                                     harness="codex")
    world["memory_store"].touch_read(memory.id, "2026-09-25T00:00:00.000000Z", memory_version=1,
                                     harness="codex", prune_before="2026-06-01T00:00:00.000000Z")
    assert _sql(world, "SELECT read_at FROM memory_reads") == [("2026-09-25T00:00:00.000000Z",)]


def test_a_read_record_that_cannot_be_written_never_raises(world):
    memory = _record(world)
    _sql(world, "DROP TABLE memory_reads")
    world["memory_store"].touch_read(memory.id, now(), memory_version=1,
                                     harness="codex")                     # best effort
    seen = world["memory_store"].read(memory.id, world["read_write_set"])
    assert seen.id == memory.id


def _plant_global(world, body: str = "shared") -> Memory:
    memory = Memory.new(body=body, type="project", project_id=world["global"], source=SOURCE,
                        description="global cue")
    _sql(world, "INSERT INTO memories (id, project_id, type, source_harness, source_method, "
                "trust, sync, description, body, created, updated, version) "
                "VALUES (?, ?, 'project', 'test', 'agent', 'agent', 1, ?, ?, ?, ?, 1)",
         memory.id, memory.project_id, memory.description, memory.body, memory.created,
         memory.updated)
    return memory


def _cite(world, derived: Memory, source: Memory) -> None:
    """A source set for the derived entry's version 1, citing `source` at version 1."""
    _sql(world, "INSERT INTO memory_source_sets VALUES (?, 1)", derived.id)
    _sql(world, "INSERT INTO memory_sources VALUES (?, 1, ?, 1, ?, ?)", derived.id, source.id,
         source.project_id, '{"type":"project","description":"cue","body":"fact"}')


def test_hard_delete_of_a_referenced_source_is_refused_and_deletes_nothing(world):
    source, derived = _record(world, "fact"), _plant_global(world)
    _cite(world, derived, source)
    with pytest.raises(MemoryReferenced) as caught:
        world["memory_store"].delete(source.id, world["read_write_set"], expected_version=1,
                                     hard=True)
    assert (caught.value.memory_id, caught.value.derived_ids) == (source.id, (derived.id,))
    assert world["memory_store"].read(source.id, world["read_write_set"]).version == 1


def test_a_soft_delete_of_a_source_keeps_its_provenance_readable(world):
    source, derived = _record(world, "fact"), _plant_global(world)
    _cite(world, derived, source)
    world["memory_store"].delete(source.id, world["read_write_set"], expected_version=1,
                                 hard=False)
    assert _sql(world, "SELECT source_id, snapshot FROM memory_sources") == [
        (source.id, '{"type":"project","description":"cue","body":"fact"}')]
    # a soft-deleted source is still referenced: its row cannot be purged either
    with pytest.raises(MemoryReferenced):
        world["memory_store"].delete(source.id, world["read_write_set"], expected_version=2,
                                     hard=True)


def test_delete_global_is_the_management_delete_of_a_global_entry(world):
    source, derived = _record(world, "fact"), _plant_global(world)
    _cite(world, derived, source)
    memory_store = world["memory_store"]
    assert memory_store.delete_global(derived.id, expected_version=1, hard=False) == 2
    with pytest.raises(VersionConflict):
        memory_store.delete_global(derived.id, expected_version=1, hard=True)
    assert memory_store.delete_global(derived.id, expected_version=2, hard=True) == 0
    # purging the derived entry dropped its source rows, so the source can go now
    assert _sql(world, "SELECT count(*) FROM memory_sources") == [(0,)]
    assert memory_store.delete(source.id, world["read_write_set"], expected_version=1,
                               hard=True) == 0


def test_delete_global_refuses_a_project_entry_and_an_unknown_id(world):
    memory = _record(world)
    with pytest.raises(ProjectUnavailable) as caught:
        world["memory_store"].delete_global(memory.id, expected_version=1, hard=False)
    assert caught.value.reason == "not-global"
    with pytest.raises(MemoryNotFound):
        world["memory_store"].delete_global("zzzzzzzzzz", expected_version=1, hard=False)


def test_hard_delete_refuses_when_a_derived_id_is_corrupted(world):
    """A row inserted outside the store (foreign keys off) can carry a derived_id that is
    not a valid memory id; a hard delete must refuse it as damage, not echo it."""
    source = _record(world, "fact")
    bad_id = "not-an-id\nFORGED LINE " + chr(27) + "[2J"
    _sql(world, "INSERT INTO memory_source_sets VALUES (?, 1)", bad_id)
    _sql(world, "INSERT INTO memory_sources VALUES (?, 1, ?, 1, ?, ?)", bad_id, source.id,
         source.project_id, '{"type":"project","description":"cue","body":"fact"}')
    with pytest.raises(StorageFailure):
        world["memory_store"].delete(source.id, world["read_write_set"], expected_version=1,
                                     hard=True)
    assert world["memory_store"].read(source.id, world["read_write_set"]).version == 1
