import threading
import time

from memriver_core.models import AccessContext, Memory, Scope
from memriver_core.repository.filesystem import FileMemoryRepository
from memriver_core.repository.filesystem.locking import store_lock

CTX = AccessContext(project_id=None)


def test_store_lock_creates_the_root_and_its_lock_file(tmp_path):
    root = tmp_path / "store"
    with store_lock(root):
        pass
    assert (root / ".lock").exists()


def test_update_body_serializes_concurrent_writers(tmp_path):
    # instrument the critical section directly: end-state assertions alone
    # can't discriminate a missing lock, because _atomic_write's mkstemp +
    # os.replace already guarantees a single clean winner either way. Count
    # concurrent entries into _atomic_write instead -- with the store-wide
    # flock held for the whole read-modify-write, only one thread can ever
    # be inside it at a time.
    repo = FileMemoryRepository(tmp_path)
    repo.create(Memory.new(body="base", type="user", scope=Scope.global_(),
                           source={}, id="n"), CTX)
    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    real_write = repo._atomic_write

    def observed_write(path, text):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)  # widen the window so an unserialized peer overlaps
        try:
            real_write(path, text)
        finally:
            with counter_lock:
                active -= 1

    repo._atomic_write = observed_write
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def attempt(marker: str) -> None:
        try:
            barrier.wait(timeout=5)
            repo.update_body("n", marker, CTX)
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=attempt, args=(m,)) for m in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert errors == []
    assert max_active == 1  # flock serialized the two critical sections
    assert repo.get("n", CTX).body in {"a", "b"}
