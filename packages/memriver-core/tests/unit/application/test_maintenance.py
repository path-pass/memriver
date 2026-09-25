"""MaintenanceService over a real SQLite store: the maintenance run's reads and writes."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from memriver_core import bootstrap
from memriver_core.models import Memory, new_id, now, timestamp_shift
from memriver_core.repository.sqlite.database import DATABASE_FILENAME
from memriver_core.settings import Settings

SECRET = "token ghp_" + "a" * 36          # trips the github-pat rule
SNAPSHOT = '{"type":"project","description":"cue","body":"x"}'


@pytest.fixture
def world(tmp_path):
    store, home = tmp_path / "store", tmp_path / "home"
    work, other = tmp_path / "work", tmp_path / "other"
    for directory in (home, work, other):
        directory.mkdir()
    settings = Settings(root=store)
    service = bootstrap.build_service(settings, root=store, home=home)
    global_id = service.ensure_global()
    project = service.init_project("demo", service.plan_root(str(work)))
    other_project = service.init_project("other", service.plan_root(str(other)))
    return SimpleNamespace(
        store=store, service=service, global_id=global_id, project=project,
        other=other_project, context=service.open_project_context(str(work)),
        other_context=service.open_project_context(str(other)),
        maintenance=bootstrap.build_maintenance_service(settings, root=store))


def _write(world, body="uv manages python", *, description="cue", context=None) -> Memory:
    return world.service.record(content=body, type="project", sync=True, harness="test",
                                description=description, context=context or world.context)


def _sql(world, statement: str, *params) -> list[tuple]:
    with closing(sqlite3.connect(world.store / DATABASE_FILENAME)) as conn, conn:
        return conn.execute(statement, params).fetchall()


def _plant(world, project_id: str, body: str, *, description: str = "cue",
           created: str | None = None, last_read_at: str | None = None, trust: str = "agent",
           sync: int = 1) -> str:
    """A row written behind the service's back: global rows, old rows, secrets."""
    memory_id, stamp = new_id(), created or now()
    _sql(world, "INSERT INTO memories (id, project_id, type, source_harness, source_method, "
                "trust, sync, description, body, created, updated, version, last_read_at) "
                "VALUES (?, ?, 'project', 'test', 'agent', ?, ?, ?, ?, ?, ?, 1, ?)",
         memory_id, project_id, trust, sync, description, body, stamp, stamp, last_read_at)
    return memory_id


def _days_ago(days: float) -> str:
    return timestamp_shift(now(), days=-days)


def test_projects_list_global_last_and_memories_list_active_rows_oldest_first(world):
    first, second, gone = _write(world, "first"), _write(world, "second"), _write(world, "gone")
    world.service.delete(gone.id, world.context, expected_version=1)
    assert world.maintenance.projects()[-1].id == world.global_id
    assert world.maintenance.global_project_id() == world.global_id
    assert [m.id for m in world.maintenance.memories(world.project.id)] == [first.id, second.id]


def test_no_maintenance_read_touches_last_read_at(world):
    memory = _write(world)
    world.maintenance.memories(world.project.id)
    world.maintenance.sources_of(memory.id)
    world.maintenance.derived_from(memory.id)
    world.maintenance.ttl_candidates(now(), 1, 5, 10)
    assert world.service.show(memory.id).last_read_at is None


def _set(world, derived: str, version: int, *sources: Memory) -> None:
    """A source set recorded for `derived` at `version` (no sources: an empty set)."""
    _sql(world, "INSERT INTO memory_source_sets VALUES (?, ?)", derived, version)
    for source in sources:
        _sql(world, "INSERT INTO memory_sources VALUES (?, ?, ?, 1, ?, ?)", derived, version,
             source.id, world.project.id, SNAPSHOT)


def test_sources_of_is_the_effective_set_and_derived_from_the_active_citers(world):
    a, b = _write(world, "a"), _write(world, "b")
    derived = _plant(world, world.global_id, "a and b")
    gone = _plant(world, world.global_id, "old")
    _set(world, derived, 1, a)
    _set(world, derived, 2, a, b)
    _set(world, gone, 1, a)
    # version 3 was written without a set (a human edit): version 2's set carries forward
    _sql(world, "UPDATE memories SET version = 3 WHERE id = ?", derived)
    _sql(world, "UPDATE memories SET deleted_at = ?, version = 2 WHERE id = ?", now(), gone)
    assert sorted((s.source_id, s.source_version, s.snapshot["body"])
                  for s in world.maintenance.sources_of(derived)) == sorted(
        [(a.id, 1, "x"), (b.id, 1, "x")])
    assert world.maintenance.derived_from(a.id) == [derived]


def test_an_empty_set_is_no_sources_not_a_carried_one(world):
    a = _write(world, "a")
    derived = _plant(world, world.project.id, "rewritten")
    _set(world, derived, 1, a)
    _set(world, derived, 2)                        # an explicitly empty set
    _sql(world, "UPDATE memories SET version = 2 WHERE id = ?", derived)
    assert world.maintenance.sources_of(derived) == []
    assert world.maintenance.derived_from(a.id) == []
    _sql(world, "UPDATE memories SET version = 1 WHERE id = ?", derived)
    assert [s.source_id for s in world.maintenance.sources_of(derived)] == [a.id]


@pytest.mark.parametrize(("reads", "age_days", "due"), [
    (0, 91, True), (0, 89, False),          # the base TTL: 90 days
    (1, 181, True), (1, 179, False),        # one read doubles it
    (9, 451, True), (9, 449, False),        # capped at 5 x 90 days
])
def test_the_effective_ttl_grows_with_reads_up_to_the_cap(world, reads, age_days, due):
    stamp = _days_ago(age_days)
    memory_id = _plant(world, world.project.id, "fact", created=stamp, last_read_at=stamp)
    for _ in range(reads):
        _sql(world, "INSERT INTO memory_reads VALUES (?, 1, ?, 'codex', NULL)", memory_id, stamp)
    candidates = world.maintenance.ttl_candidates(now(), 90, 5, 10)
    assert (memory_id in [c.memory.id for c in candidates]) is due
    if due:
        assert candidates[0].reads == reads


def test_a_review_not_yet_due_suppresses_a_candidate_and_the_oldest_come_first(world):
    old = _plant(world, world.project.id, "old", created=_days_ago(300),
                 last_read_at=_days_ago(300))
    older = _plant(world, world.global_id, "older", created=_days_ago(400),
                   last_read_at=_days_ago(400))
    reviewed = _plant(world, world.project.id, "kept", created=_days_ago(500),
                      last_read_at=_days_ago(500))
    _sql(world, "INSERT INTO dream_reviews VALUES (?, 1, ?, 'keep', 'still true', 0, ?, 'r', "
                "'claude', 'dream-1')", reviewed, now(), timestamp_shift(now(), days=30))
    assert [c.memory.id for c in world.maintenance.ttl_candidates(now(), 90, 5, 10)] == [
        older, old]
    assert [c.memory.id for c in world.maintenance.ttl_candidates(now(), 90, 5, 1)] == [older]


def test_passes_policy_refuses_a_memory_whose_body_or_description_breaks_a_rule(world):
    clean = _plant(world, world.project.id, "fact")
    secret_body = _plant(world, world.project.id, SECRET)
    secret_cue = _plant(world, world.project.id, "fact", description=SECRET)
    by_id = {m.id: m for m in world.maintenance.memories(world.project.id)}
    assert world.maintenance.passes_policy(by_id[clean])
    assert not world.maintenance.passes_policy(by_id[secret_body])
    assert not world.maintenance.passes_policy(by_id[secret_cue])
    assert world.maintenance.text_passes_policy("plain words")
    assert not world.maintenance.text_passes_policy(SECRET)
    assert world.maintenance.text_passes_policy("")          # nothing there to leak


def test_an_untouched_store_has_no_fingerprints_and_no_changes(world):
    assert world.maintenance.fingerprint_of(f"consolidate:{world.project.id}") is None
    assert world.maintenance.changes(10) == []


def test_a_description_of_only_control_characters_is_treated_as_empty_not_rejected(world):
    control_only = chr(1) + chr(2) + chr(3)
    memory_id = _plant(world, world.project.id, "fact", description=control_only)
    by_id = {m.id: m for m in world.maintenance.memories(world.project.id)}
    assert world.maintenance.text_passes_policy(control_only)
    assert world.maintenance.passes_policy(by_id[memory_id])
