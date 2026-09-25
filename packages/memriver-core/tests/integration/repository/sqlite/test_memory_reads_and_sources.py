"""The SQLite memory store's dream-era duties: read records, and the provenance
rules a hard delete has to respect."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from memriver_core.models import Memory, Project, ReadWriteSet, now
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
