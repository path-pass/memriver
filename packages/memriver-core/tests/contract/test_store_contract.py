"""The MemoryStore/ProjectStore contract every backend must satisfy.

Nothing here knows how a store keeps anything. To add a backend, write a
`BackendHarness` and put a single entry in `BACKENDS`: no test changes.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from memriver_core.models import Memory, Project, ReadWriteSet, new_id
from memriver_core.models.errors import (
    GlobalReadOnly,
    IdCollision,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
)
from memriver_core.repository.filesystem import FileMemoryStore, FileProjectStore
from memriver_core.repository.filesystem.markdown_codec import encode
from memriver_core.repository.protocol import MemoryStore, ProjectStore

SOURCE = {"harness": "test", "method": "agent"}


@dataclass(frozen=True)
class BackendHarness:
    """`make` returns an independent (memory_store, project_store) pair over
    the same storage every time it is called with the same directory -- the
    seam the concurrency test races against, standing in for two processes.

    `plant` stores a memory behind the stores' backs: agents can never write
    global, so a global memory on disk is planted, as hand maintenance does.
    `remove_project` deletes a project behind the stores' backs, standing in
    for a hand-deleted project file while a long-lived read/write set still names it.
    """

    make: Callable[[Path], tuple[MemoryStore, ProjectStore]]
    plant: Callable[[Path, Memory], None]
    remove_project: Callable[[Path, str], None]


def _make_filesystem(root: Path) -> tuple[MemoryStore, ProjectStore]:
    project_store = FileProjectStore(root)
    return FileMemoryStore(root, project_store), project_store


def _plant_file(root: Path, memory: Memory) -> None:
    directory = root / "memories"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{memory.id}.md").write_text(encode(memory), encoding="utf-8")


def _remove_project_file(root: Path, project_id: str) -> None:
    (root / "projects" / f"{project_id}.toml").unlink()


BACKENDS = {"filesystem": BackendHarness(make=_make_filesystem, plant=_plant_file,
                                         remove_project=_remove_project_file)}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request) -> BackendHarness:
    return BACKENDS[request.param]


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def stores(backend, root):
    return backend.make(root)


@pytest.fixture
def world(stores):
    """Two projects and global, with the three read/write sets a test needs."""
    memory_store, project_store = stores
    global_id = project_store.ensure_global()
    mine, other = Project.new("mine"), Project.new("other")
    project_store.create(mine)
    project_store.create(other)
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


# --- ProjectStore: create / read / global ---------------------------------

def test_created_project_reads_back(stores):
    _, project_store = stores
    project = Project.new("demo")
    project_store.create(project)
    assert project_store.read(project.id) == project


@pytest.mark.parametrize("project_id", ["", "demo", "AAAAAAAAAA", "../x"])
def test_unknown_or_malformed_project_is_not_found(stores, project_id):
    _, project_store = stores
    with pytest.raises(ProjectNotFound):
        project_store.read(project_id)
    with pytest.raises(ProjectNotFound):
        project_store.read(new_id())


def test_create_never_overwrites_an_existing_project(stores):
    _, project_store = stores
    project = Project.new("first")
    project_store.create(project)
    with pytest.raises(IdCollision):
        project_store.create(Project(id=project.id, name="second"))
    assert project_store.read(project.id).name == "first"


def test_an_uninitialized_store_has_no_global(stores):
    _, project_store = stores
    assert project_store.global_project_id() is None


def test_ensure_global_creates_a_named_project_once(stores):
    _, project_store = stores
    first = project_store.ensure_global()
    assert project_store.ensure_global() == first
    assert project_store.global_project_id() == first
    assert project_store.read(first).name == "global"


def test_ensure_global_keeps_existing_global_memories(backend, root, stores):
    _, project_store = stores
    global_id = project_store.ensure_global()
    kept = _m(global_id, body="kept")
    backend.plant(root, kept)
    assert project_store.ensure_global() == global_id
    read_write_set = ReadWriteSet(project_id=None, global_project_id=global_id)
    assert [m.id for m in project_store.search(global_id, read_write_set, query=None, limit=None)] == [kept.id]


# --- MemoryStore: record ----------------------------------------------------

def test_record_then_read_returns_the_same_memory(world):
    memory = _m(world["mine"], description="cue")
    world["memory_store"].record(memory, world["read_write_set"])
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


def test_record_never_replaces_an_existing_id(world):
    memory = _m(world["mine"], body="first")
    world["memory_store"].record(memory, world["read_write_set"])
    clash = Memory(**{**memory.__dict__, "body": "second"})
    with pytest.raises(IdCollision):
        world["memory_store"].record(clash, world["read_write_set"])
    assert world["memory_store"].read(memory.id, world["read_write_set"]).body == "first"


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


@pytest.mark.parametrize("action", ["read", "update", "delete"])
def test_a_project_removed_after_the_read_write_set_was_built_hides_its_memories(
        backend, root, world, action):
    memory = _m(world["mine"])
    world["memory_store"].record(memory, world["read_write_set"])
    backend.remove_project(root, world["mine"])
    memory_store = world["memory_store"]
    with pytest.raises(MemoryNotFound):
        if action == "read":
            memory_store.read(memory.id, world["read_write_set"])
        elif action == "update":
            memory_store.update(memory.id, world["read_write_set"], body="x", description=None)
        else:
            memory_store.delete(memory.id, world["read_write_set"])
    assert world["project_store"].search(world["mine"], world["read_write_set"], query=None, limit=None) == []


def test_update_replaces_content_advances_updated_and_keeps_the_rest(world):
    memory = _m(world["mine"], body="old", description="cue")
    world["memory_store"].record(memory, world["read_write_set"])
    updated = world["memory_store"].update(memory.id, world["read_write_set"], body=" new ", description=None)
    assert (updated.body, updated.description) == ("new", "cue")
    assert updated.updated > memory.updated
    for field in ("id", "project_id", "type", "source", "trust", "sync", "created"):
        assert getattr(updated, field) == getattr(memory, field)
    assert world["memory_store"].read(memory.id, world["read_write_set"]) == updated


def test_update_with_an_unchanged_body_still_advances_updated(world):
    memory = _m(world["mine"], body="same")
    world["memory_store"].record(memory, world["read_write_set"])
    updated = world["memory_store"].update(memory.id, world["read_write_set"], body="same", description=None)
    assert updated.updated > memory.updated


def test_update_description_empty_string_clears_it(world):
    memory = _m(world["mine"], description="cue")
    world["memory_store"].record(memory, world["read_write_set"])
    assert world["memory_store"].update(memory.id, world["read_write_set"], body="b",
                                        description="").description == ""


@pytest.mark.parametrize("action", ["update", "delete"])
def test_global_memories_are_read_only(backend, root, world, action):
    fact = _m(world["global"])
    backend.plant(root, fact)
    memory_store = world["memory_store"]
    with pytest.raises(GlobalReadOnly):
        if action == "update":
            memory_store.update(fact.id, world["read_write_set"], body="x", description=None)
        else:
            memory_store.delete(fact.id, world["read_write_set"])
    assert memory_store.read(fact.id, world["read_write_set"]) == fact


@pytest.mark.parametrize("action", ["update", "delete"])
def test_a_foreign_memory_cannot_be_changed_and_is_not_revealed(world, action):
    foreign = _m(world["other"])
    world["memory_store"].record(foreign, world["other_read_write_set"])
    with pytest.raises(MemoryNotFound):
        if action == "update":
            world["memory_store"].update(foreign.id, world["read_write_set"], body="x", description=None)
        else:
            world["memory_store"].delete(foreign.id, world["read_write_set"])
    assert world["memory_store"].read(foreign.id, world["other_read_write_set"]) == foreign


def test_delete_removes_the_memory_but_not_the_project(world):
    memory = _m(world["mine"])
    world["memory_store"].record(memory, world["read_write_set"])
    world["memory_store"].delete(memory.id, world["read_write_set"])
    with pytest.raises(MemoryNotFound):
        world["memory_store"].read(memory.id, world["read_write_set"])
    assert world["project_store"].read(world["mine"]).id == world["mine"]
    assert world["project_store"].search(world["mine"], world["read_write_set"], query=None, limit=None) == []


# --- ProjectStore: search ---------------------------------------------------

def test_search_sees_what_the_memory_store_wrote_with_no_second_membership_write(world):
    memory = _m(world["mine"], body="uv manages python")
    world["memory_store"].record(memory, world["read_write_set"])
    hits = world["project_store"].search(world["mine"], world["read_write_set"], query="UV", limit=None)
    assert hits == [memory]
    world["memory_store"].update(memory.id, world["read_write_set"], body="pip now", description=None)
    assert world["project_store"].search(world["mine"], world["read_write_set"], query="uv", limit=None) == []
    assert world["project_store"].search(world["mine"], world["read_write_set"], query="pip", limit=None)[0].body == "pip now"


def test_search_is_one_project_only(backend, root, world):
    world["memory_store"].record(_m(world["mine"], body="shared word"), world["read_write_set"])
    backend.plant(root, _m(world["global"], body="shared word"))
    hits = world["project_store"].search(world["mine"], world["read_write_set"], query="shared", limit=None)
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


def test_search_query_none_lists_all_and_empty_query_matches_nothing(world):
    for body in ("a", "b"):
        world["memory_store"].record(_m(world["mine"], body=body), world["read_write_set"])
    project_store = world["project_store"]
    assert len(project_store.search(world["mine"], world["read_write_set"], query=None, limit=None)) == 2
    assert project_store.search(world["mine"], world["read_write_set"], query="", limit=None) == []
    assert project_store.search(world["mine"], world["read_write_set"], query="\x00", limit=None) == []


def test_search_matches_description_and_body_newest_first_with_limit(world):
    older = _m(world["mine"], body="alpha", description="x")
    world["memory_store"].record(older, world["read_write_set"])
    newer = _m(world["mine"], body="y", description="ALPHA cue")
    world["memory_store"].record(newer, world["read_write_set"])
    project_store = world["project_store"]
    assert [m.id for m in project_store.search(world["mine"], world["read_write_set"], query="alpha",
                                               limit=None)] == [newer.id, older.id]
    assert [m.id for m in project_store.search(world["mine"], world["read_write_set"], query="alpha",
                                               limit=1)] == [newer.id]


def test_search_does_not_match_on_the_id(world):
    memory = _m(world["mine"], body="b")
    world["memory_store"].record(memory, world["read_write_set"])
    assert world["project_store"].search(world["mine"], world["read_write_set"], query=memory.id,
                                         limit=None) == []


# --- concurrency --------------------------------------------------------------

def test_concurrent_records_from_two_instances_all_land(backend, root, world):
    other_memory_store, _ = backend.make(root)
    stores = [world["memory_store"], other_memory_store]
    memories = [_m(world["mine"], body=f"fact {i}") for i in range(20)]

    def write(i: int) -> None:
        stores[i % 2].record(memories[i], world["read_write_set"])

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    hits = world["project_store"].search(world["mine"], world["read_write_set"], query=None, limit=None)
    assert {m.id for m in hits} == {m.id for m in memories}
