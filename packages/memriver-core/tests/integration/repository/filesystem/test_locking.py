import threading
import time

import pytest
from memriver_core.models import AccessContext, Memory, Project
from memriver_core.models.errors import StorageFailure
from memriver_core.repository.filesystem import FileMemoryStore, FileProjectStore
from memriver_core.repository.filesystem import memory_store as memory_store_module
from memriver_core.repository.filesystem.locking import store_lock


def _world(root):
    project_store = FileProjectStore(root)
    global_id = project_store.ensure_global()
    project = Project.new("mine")
    project_store.create(project)
    memory_store = FileMemoryStore(root, project_store)
    return memory_store, project_store, AccessContext(project_id=project.id,
                                                      global_project_id=global_id)


def _race(target, args_list):
    barrier = threading.Barrier(len(args_list))
    errors: list[Exception] = []

    def attempt(*args):
        try:
            barrier.wait(timeout=5)
            target(*args)
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=attempt, args=args) for args in args_list]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    return errors


def test_store_lock_creates_the_root_and_its_lock_file(tmp_path):
    root = tmp_path / "store"
    with store_lock(root):
        pass
    assert (root / ".lock").exists()


def test_store_lock_wraps_an_oserror_from_inside_the_block_as_storage_failure(tmp_path):
    root = tmp_path / "store"
    cause = OSError("disk full")
    with pytest.raises(StorageFailure) as excinfo, store_lock(root):
        raise cause
    assert excinfo.value.__cause__ is cause


def test_store_lock_lets_a_non_oserror_from_inside_the_block_propagate(tmp_path):
    root = tmp_path / "store"
    with pytest.raises(ValueError), store_lock(root):
        raise ValueError("not a storage problem")


def test_record_holds_the_lock_from_the_project_check_to_the_write(tmp_path, monkeypatch):
    # the project check opens the critical section and the write closes it; a peer entering the check while another thread is still
    # writing means the lock does not span the sequence
    memory_store, project_store, ctx = _world(tmp_path / "store")
    active = max_active = 0
    counter = threading.Lock()
    real_read, real_write = project_store.read, memory_store_module.write_new

    def observed_read(project_id):
        nonlocal active, max_active
        with counter:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        return real_read(project_id)

    def observed_write(root, path, text):
        nonlocal active
        try:
            real_write(root, path, text)
        finally:
            with counter:
                active -= 1

    monkeypatch.setattr(project_store, "read", observed_read)
    monkeypatch.setattr(memory_store_module, "write_new", observed_write)
    memories = [Memory.new(body=n, type="user", project_id=ctx.project_id, source={})
                for n in ("a", "b")]
    assert _race(memory_store.record, [(m, ctx) for m in memories]) == []
    assert max_active == 1


def test_update_serializes_concurrent_writers(tmp_path, monkeypatch):
    memory_store, _, ctx = _world(tmp_path / "store")
    memory = Memory.new(body="base", type="user", project_id=ctx.project_id, source={})
    memory_store.record(memory, ctx)
    active = max_active = 0
    counter = threading.Lock()
    real_replace = memory_store_module.replace_file

    def observed_replace(root, path, text):
        nonlocal active, max_active
        with counter:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        try:
            real_replace(root, path, text)
        finally:
            with counter:
                active -= 1

    monkeypatch.setattr(memory_store_module, "replace_file", observed_replace)
    assert _race(lambda body: memory_store.update(memory.id, ctx, body=body, description=None),
                 [("a",), ("b",)]) == []
    assert max_active == 1
    assert memory_store.read(memory.id, ctx).body in {"a", "b"}
