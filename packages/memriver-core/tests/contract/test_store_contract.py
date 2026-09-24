"""The MemoryStore/ProjectStore contract every backend must satisfy.

Nothing here knows how a store keeps anything. To add a backend, write a
`BackendHarness` and put a single entry in `BACKENDS`: no test changes.
"""

import shutil
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from memriver_core.application.service import MemoryService
from memriver_core.models import Memory, Project, ReadWriteSet, new_id, now
from memriver_core.models.errors import (
    GlobalReadOnly,
    IdCollision,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
    VersionConflict,
)
from memriver_core.repository.protocol import MemoryStore, ProjectStore
from memriver_core.repository.sqlite import SqliteMemoryStore, SqliteProjectStore

SOURCE = {"harness": "test", "method": "agent"}
MEMORY_COLUMNS = ("id, project_id, type, source_harness, source_method, trust, sync, "
                  "description, body, created, updated, version, deleted_at, last_read_at")


@dataclass(frozen=True)
class BackendHarness:
    """`make(root, home)` returns an independent (memory_store, project_store)
    pair over the same storage every time -- the seam the concurrency tests
    race against, standing in for two processes.

    `plant` stores a memory behind the stores' backs: agents can never write
    global, so a global memory is planted, as hand maintenance does; it can
    also plant an orphan. `remove_project` deletes a project behind the
    stores' backs (an outside writer that ignores foreign keys), while a
    long-lived read/write set still names it.
    """

    make: Callable[[Path, Path], tuple[MemoryStore, ProjectStore]]
    plant: Callable[[Path, Memory], None]
    remove_project: Callable[[Path, str], None]


def _make_sqlite(root: Path, home: Path) -> tuple[MemoryStore, ProjectStore]:
    return (SqliteMemoryStore(root, busy_timeout_ms=5000),
            SqliteProjectStore(root, home=home, busy_timeout_ms=5000))


def _plant_row(root: Path, memory: Memory) -> None:
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        conn.execute(
            f"INSERT INTO memories ({MEMORY_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory.id, memory.project_id, memory.type, memory.source["harness"],
             memory.source["method"], memory.trust, int(memory.sync), memory.description,
             memory.body, memory.created, memory.updated, memory.version, memory.deleted_at,
             memory.last_read_at))


def _remove_project_row(root: Path, project_id: str) -> None:
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:   # foreign keys off
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


BACKENDS = {"sqlite": BackendHarness(make=_make_sqlite, plant=_plant_row,
                                     remove_project=_remove_project_row)}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request) -> BackendHarness:
    return BACKENDS[request.param]


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def home(tmp_path) -> Path:
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def stores(backend, root, home):
    return backend.make(root, home)


def _init(project_store, directory: Path, name: str) -> Project:
    directory.mkdir(parents=True, exist_ok=True)
    project = Project.new(name, max_chars=120)
    project_store.create(project, project_store.plan_root(str(directory), None))
    return project


@pytest.fixture
def world(stores, tmp_path):
    """Two projects and global, with the three read/write sets a test needs."""
    memory_store, project_store = stores
    global_id = project_store.ensure_global()
    mine = _init(project_store, tmp_path / "mine", "mine")
    other = _init(project_store, tmp_path / "other", "other")
    return {
        "memory_store": memory_store, "project_store": project_store,
        "global": global_id, "mine": mine.id, "other": other.id,
        "read_write_set": ReadWriteSet(project_id=mine.id, global_project_id=global_id),
        "other_read_write_set": ReadWriteSet(project_id=other.id, global_project_id=global_id),
        "no_project": ReadWriteSet(project_id=None, global_project_id=global_id),
    }


def _m(project_id: str, body: str = "内容", description: str = "") -> Memory:
    return Memory.new(body=body, type="project", project_id=project_id, source=SOURCE,
                      description=description)


def _record(world, body="内容", description="") -> Memory:
    memory = _m(world["mine"], body=body, description=description)
    world["memory_store"].record(memory, world["read_write_set"])
    return memory


def _update(world, memory, body="x", description=None, version=None):
    return world["memory_store"].update(
        memory.id, world["read_write_set"], body=body, description=description,
        expected_version=memory.version if version is None else version)


def _delete(world, memory, *, hard=False, version=None, read_write_set=None):
    return world["memory_store"].delete(
        memory.id, read_write_set or world["read_write_set"], hard=hard,
        expected_version=memory.version if version is None else version)


# --- ProjectStore: create / read / global ---------------------------------

def test_created_project_reads_back_with_its_directory(stores, tmp_path):
    _, project_store = stores
    project = _init(project_store, tmp_path / "demo", "demo")
    read = project_store.read(project.id)
    assert (read.id, read.name, read.root) == (project.id, "demo", str((tmp_path / "demo").resolve()))


@pytest.mark.parametrize("project_id", ["", "demo", "AAAAAAAAAA", "../x"])
def test_unknown_or_malformed_project_is_not_found(stores, project_id):
    _, project_store = stores
    with pytest.raises(ProjectNotFound):
        project_store.read(project_id)
    with pytest.raises(ProjectNotFound):
        project_store.read(new_id())


def test_an_uninitialized_store_has_no_global(stores):
    _, project_store = stores
    assert project_store.global_project_id() is None


def test_ensure_global_creates_a_named_unbound_project_once(stores):
    _, project_store = stores
    first = project_store.ensure_global()
    assert project_store.ensure_global() == first
    assert project_store.global_project_id() == first
    read = project_store.read(first)
    assert (read.name, read.root) == ("global", None)


def test_create_with_a_name_the_read_path_would_reject_writes_nothing(stores, tmp_path):
    _, project_store = stores
    directory = tmp_path / "demo"
    directory.mkdir()
    plan = project_store.plan_root(str(directory), None)
    project = Project(new_id(), "two\nlines")
    with pytest.raises(ValueError):
        project_store.create(project, plan)
    with pytest.raises(ProjectNotFound):
        project_store.read(project.id)


def test_ensure_global_keeps_existing_global_memories(backend, root, stores):
    _, project_store = stores
    global_id = project_store.ensure_global()
    kept = _m(global_id, body="kept")
    backend.plant(root, kept)
    assert project_store.ensure_global() == global_id
    read_write_set = ReadWriteSet(project_id=None, global_project_id=global_id)
    assert [m.id for m in project_store.search(global_id, read_write_set, query=None,
                                               limit=None)] == [kept.id]


# --- MemoryStore: record ----------------------------------------------------

def test_record_then_read_returns_the_same_memory(world):
    memory = _record(world, description="cue")
    assert world["memory_store"].read(memory.id, world["read_write_set"]) == memory


def test_record_into_global_is_refused(world):
    with pytest.raises(GlobalReadOnly):
        world["memory_store"].record(_m(world["global"]), world["read_write_set"])


def test_record_into_another_project_is_refused(world):
    with pytest.raises(ProjectUnavailable):
        world["memory_store"].record(_m(world["other"]), world["read_write_set"])


def test_record_without_a_project_in_the_read_write_set_is_refused(world):
    with pytest.raises(ProjectUnavailable):
        world["memory_store"].record(_m(world["mine"]), world["no_project"])


def test_record_into_a_missing_project_is_refused_even_with_a_matching_read_write_set(world):
    missing = new_id()
    read_write_set = ReadWriteSet(project_id=missing, global_project_id=world["global"])
    with pytest.raises(ProjectUnavailable):
        world["memory_store"].record(_m(missing), read_write_set)


def test_record_into_a_removed_store_is_refused_and_never_recreates_it(root, world):
    # a still-running server after `uninstall --purge-data`
    shutil.rmtree(root)
    with pytest.raises(ProjectUnavailable):
        world["memory_store"].record(_m(world["mine"]), world["read_write_set"])
    assert not root.exists()


def test_record_never_replaces_an_existing_id_even_a_deleted_one(world):
    memory = _record(world, body="first")
    clash = Memory(**{**memory.__dict__, "body": "second"})
    with pytest.raises(IdCollision):
        world["memory_store"].record(clash, world["read_write_set"])
    _delete(world, memory)
    with pytest.raises(IdCollision):
        world["memory_store"].record(clash, world["read_write_set"])


def test_recording_a_memory_the_read_path_would_reject_is_a_value_error_and_writes_nothing(world):
    bad = Memory(**{**_m(world["mine"]).__dict__, "type": "note"})
    with pytest.raises(ValueError):
        world["memory_store"].record(bad, world["read_write_set"])
    with pytest.raises(MemoryNotFound):
        world["memory_store"].read(bad.id, world["read_write_set"])


# --- MemoryStore: read / update / delete ------------------------------------

def test_one_id_means_the_same_memory_in_every_read_write_set_that_may_read_it(backend, root, world):
    fact = _m(world["global"], body="global fact")
    backend.plant(root, fact)
    assert world["memory_store"].read(fact.id, world["read_write_set"]) == \
        world["memory_store"].read(fact.id, world["other_read_write_set"])


def test_a_foreign_id_reads_exactly_like_a_missing_one(world):
    foreign = _m(world["other"])
    world["memory_store"].record(foreign, world["other_read_write_set"])
    for memory_id in (foreign.id, new_id(), "not-an-id"):
        with pytest.raises(MemoryNotFound) as excinfo:
            world["memory_store"].read(memory_id, world["read_write_set"])
        assert excinfo.value.memory_id == memory_id


def test_an_orphan_memory_is_never_readable(backend, root, world):
    orphan = _m(new_id())
    backend.plant(root, orphan)
    with pytest.raises(MemoryNotFound):
        world["memory_store"].read(orphan.id, world["read_write_set"])


def test_touch_read_sets_last_read_at_and_nothing_else(root, world):
    memory_store, read_write_set = world["memory_store"], world["read_write_set"]
    memory = _record(world)
    memory_store.touch_read(memory.id, "2026-09-24T00:00:01.000000Z")
    seen = memory_store.read(memory.id, read_write_set)
    assert seen.last_read_at == "2026-09-24T00:00:01.000000Z"
    assert (seen.version, seen.updated, seen.body) == (memory.version, memory.updated, memory.body)
    # an earlier timestamp never moves it back
    memory_store.touch_read(memory.id, "2020-01-01T00:00:00.000000Z")
    assert memory_store.read(memory.id, read_write_set).last_read_at == \
        "2026-09-24T00:00:01.000000Z"
    memory_store.touch_read(new_id(), "2026-09-24T00:00:01.000000Z")   # unknown id: a no-op
    shutil.rmtree(root)
    memory_store.touch_read(memory.id, "2026-09-24T00:00:01.000000Z")  # absent store: a no-op


def test_touch_read_with_a_malformed_at_is_a_no_op(world):
    memory_store, read_write_set = world["memory_store"], world["read_write_set"]
    memory = _record(world)
    memory_store.touch_read(memory.id, "not-a-timestamp")
    assert memory_store.read(memory.id, read_write_set).last_read_at is None
    memory_store.touch_read(memory.id, "2026-09-24T00:00:01.000000Z")
    memory_store.touch_read(memory.id, "not-a-timestamp")   # a bad value never overwrites a good one
    assert memory_store.read(memory.id, read_write_set).last_read_at == \
        "2026-09-24T00:00:01.000000Z"


@pytest.mark.parametrize("action", ["read", "update", "delete"])
def test_a_project_removed_after_the_read_write_set_was_built_hides_its_memories(
        backend, root, world, action):
    memory = _record(world)
    backend.remove_project(root, world["mine"])
    with pytest.raises(MemoryNotFound):
        if action == "read":
            world["memory_store"].read(memory.id, world["read_write_set"])
        elif action == "update":
            _update(world, memory)
        else:
            _delete(world, memory)
    assert world["project_store"].search(world["mine"], world["read_write_set"], query=None,
                                         limit=None) == []


def test_update_replaces_content_advances_updated_and_version_and_keeps_the_rest(world):
    memory = _record(world, body="old", description="cue")
    updated = _update(world, memory, body=" new ")
    assert (updated.body, updated.description, updated.version) == ("new", "cue", 2)
    assert updated.updated > memory.updated
    for field in ("id", "project_id", "type", "source", "trust", "sync", "created"):
        assert getattr(updated, field) == getattr(memory, field)
    assert world["memory_store"].read(memory.id, world["read_write_set"]) == updated


def test_update_with_an_unchanged_body_still_advances_updated_and_version(world):
    memory = _record(world, body="same")
    updated = _update(world, memory, body="same")
    assert updated.updated > memory.updated and updated.version == memory.version + 1


def test_update_description_empty_string_clears_it(world):
    memory = _record(world, description="cue")
    assert _update(world, memory, body="b", description="").description == ""


@pytest.mark.parametrize("action", ["update", "soft-delete", "hard-delete"])
def test_a_stale_version_is_a_conflict_and_changes_nothing(world, action):
    memory = _record(world, body="v1")
    current = _update(world, memory, body="v2")                  # now at version 2
    with pytest.raises(VersionConflict) as excinfo:
        if action == "update":
            _update(world, memory, body="lost", version=1)
        else:
            _delete(world, memory, hard=action == "hard-delete", version=1)
    assert excinfo.value.memory_id == memory.id
    assert world["memory_store"].read(memory.id, world["read_write_set"]) == current


def test_two_writers_with_the_same_version_exactly_one_wins(backend, root, home, world):
    memory = _record(world, body="v1")
    other_memory_store, _ = backend.make(root, home)
    results: list[object] = []
    barrier = threading.Barrier(2)

    def write(memory_store, body):
        barrier.wait()
        try:
            results.append(memory_store.update(memory.id, world["read_write_set"], body=body,
                                               description=None, expected_version=1))
        except VersionConflict as err:
            results.append(err)

    threads = [threading.Thread(target=write, args=(s, b))
               for s, b in ((world["memory_store"], "a"), (other_memory_store, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(type(r).__name__ for r in results) == ["Memory", "VersionConflict"]
    winner = next(r for r in results if isinstance(r, Memory))
    assert world["memory_store"].read(memory.id, world["read_write_set"]).body == winner.body


@pytest.mark.parametrize("action", ["update", "delete"])
def test_global_memories_are_read_only(backend, root, world, action):
    fact = _m(world["global"])
    backend.plant(root, fact)
    with pytest.raises(GlobalReadOnly):
        if action == "update":
            _update(world, fact)
        else:
            _delete(world, fact)
    assert world["memory_store"].read(fact.id, world["read_write_set"]) == fact


def test_hard_delete_of_global_is_refused_too(backend, root, world):
    fact = _m(world["global"])
    backend.plant(root, fact)
    with pytest.raises(GlobalReadOnly):
        _delete(world, fact, hard=True)


def _swap_global_role(root: Path, old_global: str, new_global: str) -> None:
    """Make `new_global` the sole is_global row and `old_global` an ordinary
    project, mimicking a database replaced or restored under a running
    server: a read/write set built before the swap still names `old_global`
    as global, so only a row read inside the write transaction knows better.
    """
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        conn.execute("UPDATE projects SET root = NULL WHERE id = ?", (new_global,))
        conn.execute("UPDATE projects SET is_global = 0 WHERE id = ?", (old_global,))
        conn.execute("UPDATE projects SET is_global = 1 WHERE id = ?", (new_global,))


def test_record_is_refused_once_the_target_project_becomes_global(root, world):
    _swap_global_role(root, world["global"], world["mine"])
    memory = _m(world["mine"])
    with pytest.raises(GlobalReadOnly):
        world["memory_store"].record(memory, world["read_write_set"])
    with pytest.raises(MemoryNotFound):
        world["memory_store"].read_any(memory.id, include_deleted=True)


@pytest.mark.parametrize("action", ["update", "soft-delete", "hard-delete"])
def test_update_or_delete_is_refused_once_the_target_project_becomes_global(root, world, action):
    memory = _record(world)
    _swap_global_role(root, world["global"], world["mine"])
    with pytest.raises(GlobalReadOnly):
        if action == "update":
            _update(world, memory)
        else:
            _delete(world, memory, hard=action == "hard-delete")
    assert world["memory_store"].read_any(memory.id, include_deleted=True) == memory


@pytest.mark.parametrize("action", ["update", "delete"])
def test_a_foreign_memory_cannot_be_changed_and_is_not_revealed(world, action):
    foreign = _m(world["other"])
    world["memory_store"].record(foreign, world["other_read_write_set"])
    with pytest.raises(MemoryNotFound):
        if action == "update":
            _update(world, foreign)
        else:
            _delete(world, foreign)
    assert world["memory_store"].read(foreign.id, world["other_read_write_set"]) == foreign


def test_a_soft_delete_hides_the_memory_like_an_absent_id_and_keeps_the_project(world):
    memory = _record(world)
    assert _delete(world, memory) == memory.version + 1
    for call in (lambda: world["memory_store"].read(memory.id, world["read_write_set"]),
                 lambda: _update(world, memory, version=memory.version + 1),
                 lambda: _delete(world, memory, version=memory.version + 1)):
        with pytest.raises(MemoryNotFound):
            call()
    assert world["project_store"].read(world["mine"]).id == world["mine"]
    assert world["project_store"].search(world["mine"], world["read_write_set"], query=None,
                                         limit=None) == []


def test_read_any_sees_a_soft_deleted_memory_only_when_asked(world):
    memory = _record(world)
    _delete(world, memory)
    with pytest.raises(MemoryNotFound):
        world["memory_store"].read_any(memory.id, include_deleted=False)
    seen = world["memory_store"].read_any(memory.id, include_deleted=True)
    assert seen.deleted_at is not None and seen.version == memory.version + 1


def test_read_any_reads_every_project_including_global(backend, root, world):
    fact = _m(world["global"])
    backend.plant(root, fact)
    foreign = _m(world["other"])
    world["memory_store"].record(foreign, world["other_read_write_set"])
    assert world["memory_store"].read_any(fact.id, include_deleted=False) == fact
    assert world["memory_store"].read_any(foreign.id, include_deleted=False) == foreign


def test_hard_delete_removes_an_active_and_a_soft_deleted_row(world):
    active = _record(world, body="a")
    assert _delete(world, active, hard=True) == 0
    soft = _record(world, body="b")
    _delete(world, soft)
    assert _delete(world, soft, hard=True, version=soft.version + 1) == 0
    for memory in (active, soft):
        with pytest.raises(MemoryNotFound):
            world["memory_store"].read_any(memory.id, include_deleted=True)


def test_an_invalid_row_is_damage_on_a_direct_read_and_skipped_by_search(backend, root, world):
    good = _record(world, body="good")
    bad = _m(world["mine"], body="bad")
    backend.plant(root, bad)
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        # an outside writer can switch CHECK constraints off; reads must not trust them
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET trust = 'unheard-of' WHERE id = ?", (bad.id,))
    with pytest.raises(StorageFailure):
        world["memory_store"].read(bad.id, world["read_write_set"])
    assert world["project_store"].search(world["mine"], world["read_write_set"], query=None,
                                         limit=None) == [good]


def test_a_damaged_deleted_row_answers_exactly_like_an_absent_id(root, world):
    memory = _record(world)
    _delete(world, memory)
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET trust = 'odd' WHERE id = ?", (memory.id,))
    for call in (lambda: world["memory_store"].read(memory.id, world["read_write_set"]),
                 lambda: _update(world, memory, version=memory.version + 1),
                 lambda: _delete(world, memory, version=memory.version + 1)):
        with pytest.raises(MemoryNotFound):
            call()
    with pytest.raises(StorageFailure):                    # only the management read sees it
        world["memory_store"].read_any(memory.id, include_deleted=True)


def test_a_damaged_row_of_another_project_answers_exactly_like_an_absent_id(backend, root, world):
    foreign = _m(world["other"])
    backend.plant(root, foreign)
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET trust = 'odd' WHERE id = ?", (foreign.id,))
    for call in (lambda: world["memory_store"].read(foreign.id, world["read_write_set"]),
                 lambda: _update(world, foreign),
                 lambda: _delete(world, foreign)):
        with pytest.raises(MemoryNotFound):               # knowing an id grants nothing
            call()
    with pytest.raises(StorageFailure):                    # only the management read sees it
        world["memory_store"].read_any(foreign.id, include_deleted=False)


def test_an_undecodable_row_of_another_project_answers_exactly_like_an_absent_id(
        backend, root, world):
    foreign = _m(world["other"])
    backend.plant(root, foreign)
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        # STRICT accepts invalid UTF-8 in a TEXT column; the driver fails to decode it
        conn.execute("UPDATE memories SET body = CAST(X'80' AS TEXT) WHERE id = ?", (foreign.id,))
    for call in (lambda: world["memory_store"].read(foreign.id, world["read_write_set"]),
                 lambda: _update(world, foreign),
                 lambda: _delete(world, foreign),
                 lambda: _delete(world, foreign, hard=True)):
        with pytest.raises(MemoryNotFound):
            call()
    with pytest.raises(StorageFailure):                    # only the management read sees it
        world["memory_store"].read_any(foreign.id, include_deleted=False)


def test_an_undecodable_row_in_the_same_project_is_skipped_by_search_and_index_not_a_crash(
        root, world):
    good = _record(world, body="good")
    bad = _record(world, body="will be corrupted")
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        # STRICT accepts invalid UTF-8 in a TEXT column; sqlite3's default text_factory
        # would raise OperationalError decoding it and fail the whole fetchall()
        conn.execute("UPDATE memories SET body = CAST(X'80' AS TEXT) WHERE id = ?", (bad.id,))
    assert world["project_store"].search(world["mine"], world["read_write_set"], query=None,
                                         limit=None) == [good]
    service = MemoryService(
        world["memory_store"], world["project_store"], content_policy_factory=lambda: None,
        diagnostics=None, max_body_chars=10_000, metadata_max_chars=1_000,
        search_limit_default=20, search_limit_max=100, index_budget_lines=50,
        index_cue_chars=80, header_field_chars=80, project_name_max_chars=120)
    assert good.id in service.index(world["read_write_set"])
    assert world["memory_store"].read(good.id, world["read_write_set"]) == good
    with pytest.raises(StorageFailure):
        world["memory_store"].read(bad.id, world["read_write_set"])


def test_a_soft_delete_leaves_updated_unchanged(world):
    memory = _record(world)
    _delete(world, memory)
    assert world["memory_store"].read_any(memory.id, include_deleted=True).updated == memory.updated


def test_hard_delete_of_another_projects_memory_is_not_found_active_or_soft_deleted(world):
    active = _m(world["other"], body="a")
    soft = _m(world["other"], body="b")
    for memory in (active, soft):
        world["memory_store"].record(memory, world["other_read_write_set"])
    world["memory_store"].delete(soft.id, world["other_read_write_set"],
                                 expected_version=soft.version, hard=False)
    for memory, version in ((active, active.version), (soft, soft.version + 1)):
        with pytest.raises(MemoryNotFound):
            _delete(world, memory, hard=True, version=version)
        assert world["memory_store"].read_any(memory.id, include_deleted=True).id == memory.id


def test_hard_delete_of_a_soft_deleted_global_memory_is_refused(backend, root, world):
    fact = Memory(**{**_m(world["global"]).__dict__, "version": 2, "deleted_at": now()})
    backend.plant(root, fact)
    with pytest.raises(GlobalReadOnly):
        _delete(world, fact, hard=True)
    assert world["memory_store"].read_any(fact.id, include_deleted=True) == fact


def test_hard_delete_of_a_soft_deleted_row_with_a_stale_version_is_a_conflict(world):
    memory = _record(world)
    _delete(world, memory)                                 # now at version 2, deleted
    with pytest.raises(VersionConflict) as excinfo:
        _delete(world, memory, hard=True, version=memory.version)
    assert excinfo.value.memory_id == memory.id
    assert world["memory_store"].read_any(memory.id, include_deleted=True).version == \
        memory.version + 1


# --- ProjectStore: search ---------------------------------------------------

def test_search_sees_what_the_memory_store_wrote_with_no_second_membership_write(world):
    memory = _record(world, body="uv manages python")
    assert world["project_store"].search(world["mine"], world["read_write_set"], query="UV",
                                         limit=None) == [memory]
    _update(world, memory, body="pip now")
    project_store = world["project_store"]
    assert project_store.search(world["mine"], world["read_write_set"], query="uv", limit=None) == []
    assert project_store.search(world["mine"], world["read_write_set"], query="pip",
                                limit=None)[0].body == "pip now"


def test_search_is_one_project_only(backend, root, world):
    _record(world, body="shared word")
    backend.plant(root, _m(world["global"], body="shared word"))
    hits = world["project_store"].search(world["mine"], world["read_write_set"], query="shared",
                                         limit=None)
    assert {m.project_id for m in hits} == {world["mine"]}


def test_search_of_an_unauthorized_or_missing_project_is_empty(backend, root, world):
    world["memory_store"].record(_m(world["other"], body="secret"), world["other_read_write_set"])
    orphan_project = new_id()
    backend.plant(root, _m(orphan_project, body="secret"))
    hand_built = ReadWriteSet(project_id=orphan_project, global_project_id=world["global"])
    project_store = world["project_store"]
    assert project_store.search(world["other"], world["read_write_set"], query=None, limit=None) == []
    assert project_store.search(orphan_project, hand_built, query=None, limit=None) == []
    assert project_store.search(new_id(), world["read_write_set"], query=None, limit=None) == []


def test_the_management_view_searches_any_project(world):
    foreign = _m(world["other"], body="theirs")
    world["memory_store"].record(foreign, world["other_read_write_set"])
    assert world["project_store"].search(world["other"], None, query="theirs",
                                         limit=None) == [foreign]


def test_search_query_none_lists_all_and_empty_query_matches_nothing(world):
    for body in ("a", "b"):
        _record(world, body=body)
    project_store = world["project_store"]
    assert len(project_store.search(world["mine"], world["read_write_set"], query=None, limit=None)) == 2
    assert project_store.search(world["mine"], world["read_write_set"], query="", limit=None) == []
    assert project_store.search(world["mine"], world["read_write_set"], query="\x00", limit=None) == []


def test_search_matches_description_and_body_newest_first_with_limit(world):
    older = _record(world, body="alpha", description="x")
    newer = _record(world, body="y", description="ALPHA cue")
    project_store = world["project_store"]
    assert [m.id for m in project_store.search(world["mine"], world["read_write_set"],
                                               query="alpha", limit=None)] == [newer.id, older.id]
    assert [m.id for m in project_store.search(world["mine"], world["read_write_set"],
                                               query="alpha", limit=1)] == [newer.id]


def test_search_folds_case_beyond_ascii(world):
    memory = _record(world, body="ÄRGER mit Umlauten")
    assert world["project_store"].search(world["mine"], world["read_write_set"], query="ärger",
                                         limit=None) == [memory]


def test_search_does_not_match_on_the_id(world):
    memory = _record(world, body="b")
    assert world["project_store"].search(world["mine"], world["read_write_set"], query=memory.id,
                                         limit=None) == []


def test_every_write_method_reads_back_what_it_wrote(stores, tmp_path):
    memory_store, project_store = stores
    directory = tmp_path / "proj"
    directory.mkdir()
    project = Project.new("proj", max_chars=120)
    project_store.create(project, project_store.plan_root(str(directory), None))     # create
    assert project_store.read(project.id).root == str(directory.resolve())

    unbind_plan, _ = project_store.plan_unbind(project.id, str(directory.resolve()),
                                               str(tmp_path))
    project_store.unbind(unbind_plan)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    project_store.bind(project.id, project_store.plan_root(str(elsewhere), project.id))  # bind
    assert project_store.read(project.id).root == str(elsewhere.resolve())

    global_id = project_store.ensure_global()
    read_write_set = ReadWriteSet(project_id=project.id, global_project_id=global_id)
    memory = _m(project.id, body="first")
    memory_store.record(memory, read_write_set)                                     # record
    assert memory_store.read(memory.id, read_write_set) == memory

    updated = memory_store.update(memory.id, read_write_set, body="second",
                                  description=None, expected_version=memory.version)  # update
    assert memory_store.read(memory.id, read_write_set) == updated

    version = memory_store.delete(memory.id, read_write_set,                        # soft delete
                                  expected_version=updated.version, hard=False)
    seen = memory_store.read_any(memory.id, include_deleted=True)
    assert seen.deleted_at is not None and seen.version == version


# --- concurrency --------------------------------------------------------------

def test_concurrent_records_from_two_instances_all_land(backend, root, home, world):
    other_memory_store, _ = backend.make(root, home)
    memory_stores = [world["memory_store"], other_memory_store]
    memories = [_m(world["mine"], body=f"fact {i}") for i in range(20)]

    def write(i: int) -> None:
        memory_stores[i % 2].record(memories[i], world["read_write_set"])

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    hits = world["project_store"].search(world["mine"], world["read_write_set"], query=None,
                                         limit=None)
    assert {m.id for m in hits} == {m.id for m in memories}


def test_a_missing_store_answers_absence_and_is_never_created_by_reads_or_updates(tmp_path, home):
    memory_store, project_store = _make_sqlite(tmp_path / "none", home)
    read_write_set = ReadWriteSet(project_id=new_id(), global_project_id=None)
    with pytest.raises(MemoryNotFound):
        memory_store.read(new_id(), read_write_set)
    with pytest.raises(MemoryNotFound):
        memory_store.update(new_id(), read_write_set, body="x", description=None,
                            expected_version=1)
    with pytest.raises(MemoryNotFound):
        memory_store.delete(new_id(), read_write_set, expected_version=1, hard=False)
    assert project_store.search(new_id(), None, query=None, limit=None) == []
    assert not (tmp_path / "none").exists()
