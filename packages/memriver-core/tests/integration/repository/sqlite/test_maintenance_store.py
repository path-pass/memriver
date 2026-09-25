"""`SqliteMaintenanceStore` directly: cross-project reads, and the id/row validation
a read must apply to a row planted -- or corrupted -- outside the store."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from memriver_core.models import Project, new_id, now
from memriver_core.repository.sqlite import SqliteMaintenanceStore, SqliteProjectStore
from memriver_core.repository.sqlite.database import DATABASE_FILENAME

SNAPSHOT = '{"type":"project","description":"cue","body":"x"}'


@pytest.fixture
def world(tmp_path):
    store, home, work = tmp_path / "store", tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    project_store = SqliteProjectStore(store, home=home, busy_timeout_ms=2000)
    global_id = project_store.ensure_global()
    project = Project.new("demo", max_chars=120)
    project_store.create(project, project_store.plan_root(str(work), None))
    return {"store": store, "maintenance": SqliteMaintenanceStore(store, busy_timeout_ms=2000),
            "global": global_id, "project": project.id}


def _sql(world, statement: str, *params) -> list[tuple]:
    with closing(sqlite3.connect(world["store"] / DATABASE_FILENAME)) as conn, conn:
        return conn.execute(statement, params).fetchall()


def _plant(world, project_id: str, body: str, *, created: str | None = None,
          last_read_at: str | None = None, trust: str = "agent") -> str:
    memory_id, stamp = new_id(), created or now()
    _sql(world, "INSERT INTO memories (id, project_id, type, source_harness, source_method, "
                "trust, sync, description, body, created, updated, version, last_read_at) "
                "VALUES (?, ?, 'project', 'test', 'agent', ?, 1, 'cue', ?, ?, ?, 1, ?)",
         memory_id, project_id, trust, body, stamp, stamp, last_read_at)
    return memory_id


def _cite(world, derived: str, source: str) -> None:
    """A version-1 source set for `derived`, citing `source` at version 1."""
    _sql(world, "INSERT INTO memory_source_sets VALUES (?, 1)", derived)
    _sql(world, "INSERT INTO memory_sources VALUES (?, 1, ?, 1, ?, ?)", derived, source,
         world["project"], SNAPSHOT)


# --- N3: active_memories spans every project -------------------------------

def test_active_memories_spans_every_project_oldest_first_and_touches_nothing(world):
    first = _plant(world, world["project"], "first", created="2026-01-01T00:00:00.000000Z")
    glob = _plant(world, world["global"], "global one", created="2026-01-02T00:00:00.000000Z")
    second = _plant(world, world["project"], "second", created="2026-01-03T00:00:00.000000Z")
    deleted = _plant(world, world["project"], "gone", created="2026-01-04T00:00:00.000000Z")
    _sql(world, "UPDATE memories SET deleted_at = ? WHERE id = ?", now(), deleted)
    orphan = _plant(world, "zzzzzzzzzz", "orphan", created="2026-01-05T00:00:00.000000Z")
    bad = _plant(world, world["project"], "bad", created="2026-01-06T00:00:00.000000Z")
    with closing(sqlite3.connect(world["store"] / DATABASE_FILENAME)) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET trust = 'odd' WHERE id = ?", (bad,))

    ids = [m.id for m in world["maintenance"].active_memories()]
    assert ids == [first, glob, second]         # deleted, orphan and bad rows excluded
    assert orphan not in ids and bad not in ids and deleted not in ids
    assert _sql(world, "SELECT count(*) FROM memory_reads") == [(0,)]
    assert _sql(world, "SELECT last_read_at FROM memories WHERE id = ?", first) == [(None,)]


# --- I1: derived_from must not echo a corrupted id --------------------------

def test_derived_from_skips_a_corrupted_derived_id_and_stays_json_encodable(world):
    source = _plant(world, world["project"], "fact")
    good = _plant(world, world["project"], "good derived")
    bad = _plant(world, world["project"], "bad derived")
    _cite(world, good, source)
    _cite(world, bad, source)
    with closing(sqlite3.connect(world["store"] / DATABASE_FILENAME)) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET id = CAST(X'80' AS TEXT) WHERE id = ?", (bad,))
        conn.execute("UPDATE memory_source_sets SET derived_id = CAST(X'80' AS TEXT) "
                    "WHERE derived_id = ?", (bad,))
        conn.execute("UPDATE memory_sources SET derived_id = CAST(X'80' AS TEXT) "
                    "WHERE derived_id = ?", (bad,))

    result = world["maintenance"].derived_from(source)
    assert result == [good]
    assert all(isinstance(item, str) for item in result)
    json.dumps({"derived": result})              # must not raise TypeError on bytes


# --- N1: fingerprint_of must apply the same row validation as doctor -------

def test_fingerprint_of_returns_the_stored_value_for_a_valid_row(world):
    _sql(world, "INSERT INTO dream_state VALUES (?, ?, ?)", "consolidate:p1", "fp-1", now())
    assert world["maintenance"].fingerprint_of("consolidate:p1") == "fp-1"


def test_fingerprint_of_refuses_a_row_with_a_malformed_processed_at(world):
    _sql(world, "INSERT INTO dream_state VALUES (?, ?, ?)", "consolidate:p2", "fp-2",
         "not-a-timestamp")
    assert world["maintenance"].fingerprint_of("consolidate:p2") is None


def test_fingerprint_of_refuses_a_row_with_an_empty_fingerprint(world):
    _sql(world, "INSERT INTO dream_state VALUES (?, ?, ?)", "consolidate:p3", "", now())
    assert world["maintenance"].fingerprint_of("consolidate:p3") is None
