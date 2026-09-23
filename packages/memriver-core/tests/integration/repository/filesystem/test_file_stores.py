import logging
import os
import stat
import threading
import tomllib

import pytest
from memriver_core.models import Memory, Project, ReadWriteSet, new_id
from memriver_core.models.errors import IdCollision, MemoryNotFound, StorageFailure
from memriver_core.repository.filesystem import FileMemoryStore, FileProjectStore
from memriver_core.repository.filesystem.markdown_codec import encode

SOURCE = {"harness": "test", "method": "agent"}


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "store"
    project_store = FileProjectStore(root)
    memory_store = FileMemoryStore(root, project_store)
    global_id = project_store.ensure_global()
    project = Project.new("mine")
    project_store.create(project)
    read_write_set = ReadWriteSet(project_id=project.id, global_project_id=global_id)
    return root, memory_store, project_store, project.id, global_id, read_write_set


def _m(project_id, body="b"):
    return Memory.new(body=body, type="project", project_id=project_id, source=SOURCE)


def test_layout_is_flat_memories_project_files_and_a_manifest(world):
    root, memory_store, _, project_id, global_id, read_write_set = world
    memory = _m(project_id)
    memory_store.record(memory, read_write_set)
    assert (root / "memories" / f"{memory.id}.md").is_file()
    assert tomllib.loads((root / "projects" / f"{project_id}.toml").read_text()) == {"name": "mine"}
    assert tomllib.loads((root / "store.toml").read_text()) == {"global_project": global_id}
    assert not (root / "global").exists()


def test_created_files_are_private_and_created_directories_are_0700(world):
    root, memory_store, _, project_id, _, read_write_set = world
    memory = _m(project_id)
    memory_store.record(memory, read_write_set)
    for path in (root / "memories", root / "projects"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (root / "memories" / f"{memory.id}.md", root / "store.toml",
                 root / "projects" / f"{project_id}.toml"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("name", ['quote " and \\ backslash', "中文 名称 😀", "tab\there",
                                  r"a literal \n stays two characters"])
def test_project_names_roundtrip_through_toml(tmp_path, name):
    project_store = FileProjectStore(tmp_path / "store")
    project = Project.new(name)
    project_store.create(project)
    assert project_store.read(project.id) == project


def test_a_symlinked_memory_file_is_never_followed_and_reads_as_damage(world, tmp_path):
    root, memory_store, _, project_id, _, read_write_set = world
    target = tmp_path / "outside.md"
    memory = _m(project_id)
    target.write_text(encode(memory), encoding="utf-8")
    (root / "memories").mkdir(exist_ok=True)
    os.symlink(target, root / "memories" / f"{memory.id}.md")
    with pytest.raises(StorageFailure):
        memory_store.read(memory.id, read_write_set)


def _call_with_timeout(fn, *args, seconds: float = 5.0, unblock=None):
    """Run ``fn(*args)`` on a thread; a call that does not return in ``seconds`` fails.

    A regression that opens a FIFO would block the caller forever and hang
    pytest instead of failing one test. On timeout the write end of
    ``unblock`` is opened and closed so the stuck reader sees EOF and the
    thread can finish; the test then fails with a timeout, not a hang.
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the test thread
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        if unblock is not None:
            os.close(os.open(unblock, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(seconds)
        pytest.fail(f"{fn.__name__} did not return within {seconds}s: it blocked on the file")
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["value"]


def test_a_fifo_at_a_memory_name_is_refused_without_blocking(world):
    root, memory_store, project_store, project_id, _, read_write_set = world
    memory_id = new_id()
    (root / "memories").mkdir(exist_ok=True)
    fifo = root / "memories" / f"{memory_id}.md"
    os.mkfifo(fifo)
    with pytest.raises(StorageFailure):
        _call_with_timeout(memory_store.read, memory_id, read_write_set, unblock=fifo)
    # a scan skips the one damaged file rather than failing
    assert _call_with_timeout(lambda: project_store.search(project_id, read_write_set, query=None,
                                                           limit=None), unblock=fifo) == []


def test_an_undecodable_memory_file_is_damage_not_absence(world):
    root, memory_store, _, _, _, read_write_set = world
    memory_id = new_id()
    (root / "memories").mkdir(exist_ok=True)
    (root / "memories" / f"{memory_id}.md").write_text("hand notes\n", encoding="utf-8")
    for action in (lambda: memory_store.read(memory_id, read_write_set),
                   lambda: memory_store.update(memory_id, read_write_set, body="x", description=None),
                   lambda: memory_store.delete(memory_id, read_write_set)):
        with pytest.raises(StorageFailure):
            action()
    assert (root / "memories" / f"{memory_id}.md").read_text() == "hand notes\n"


def test_an_unreadable_memory_file_is_a_storage_failure(world):
    root, memory_store, _, project_id, _, read_write_set = world
    memory = _m(project_id)
    memory_store.record(memory, read_write_set)
    path = root / "memories" / f"{memory.id}.md"
    path.chmod(0)
    try:
        with pytest.raises(StorageFailure):
            memory_store.read(memory.id, read_write_set)
    finally:
        path.chmod(0o600)


def test_a_frontmatter_id_that_disagrees_with_the_file_name_is_a_storage_failure(world):
    root, memory_store, _, project_id, _, read_write_set = world
    memory = _m(project_id)
    other_id = new_id()
    (root / "memories").mkdir(exist_ok=True)
    (root / "memories" / f"{other_id}.md").write_text(encode(memory), encoding="utf-8")
    with pytest.raises(StorageFailure):
        memory_store.read(other_id, read_write_set)


def test_absent_and_malformed_ids_are_not_found(world):
    _, memory_store, _, _, _, read_write_set = world
    for memory_id in (new_id(), "not-an-id", "AAAAAAAAAA"):
        with pytest.raises(MemoryNotFound):
            memory_store.read(memory_id, read_write_set)


def test_a_damaged_project_file_is_a_storage_failure_for_record_read_and_search(world):
    root, memory_store, project_store, project_id, _, read_write_set = world
    memory = _m(project_id)
    memory_store.record(memory, read_write_set)
    (root / "projects" / f"{project_id}.toml").write_text("name = 5\n")
    with pytest.raises(StorageFailure):
        memory_store.read(memory.id, read_write_set)
    with pytest.raises(StorageFailure):
        memory_store.record(_m(project_id), read_write_set)
    with pytest.raises(StorageFailure):
        project_store.search(project_id, read_write_set, query=None, limit=None)


def test_an_unlistable_memories_directory_fails_the_search(world):
    root, memory_store, project_store, project_id, _, read_write_set = world
    memory_store.record(_m(project_id), read_write_set)
    (root / "memories").chmod(0)
    try:
        with pytest.raises(StorageFailure):
            project_store.search(project_id, read_write_set, query=None, limit=None)
    finally:
        (root / "memories").chmod(0o700)


def test_a_write_failure_on_a_located_memory_is_a_storage_failure(world, monkeypatch):
    _, memory_store, _, project_id, _, read_write_set = world
    memory = _m(project_id)
    memory_store.record(memory, read_write_set)
    from memriver_core.repository.filesystem import memory_store as module

    def fail(*_args, **_kwargs):
        raise OSError("disk full at /secret/path")

    monkeypatch.setattr(module, "replace_file", fail)
    with pytest.raises(StorageFailure):
        memory_store.update(memory.id, read_write_set, body="x", description=None)


def _shared_source_memory(project_id):
    shared = ["a", "b"]
    return Memory.new(body="b", type="project", project_id=project_id,
                      source={"first": shared, "second": shared})


def test_a_memory_the_reader_would_refuse_is_never_recorded(world):
    root, memory_store, _, project_id, _, read_write_set = world
    with pytest.raises(ValueError):
        memory_store.record(_shared_source_memory(project_id), read_write_set)
    assert not (root / "memories").exists() or not any((root / "memories").iterdir())


def test_an_update_the_reader_would_refuse_leaves_the_file_byte_for_byte(world, monkeypatch):
    root, memory_store, _, project_id, _, read_write_set = world
    memory = _m(project_id)
    memory_store.record(memory, read_write_set)
    path = root / "memories" / f"{memory.id}.md"
    before = path.read_bytes()
    # not reachable through update's parameters today (a decoded source is a
    # tree); stands in for any future field that could share a container
    unstorable = _shared_source_memory(project_id)
    unstorable.id = memory.id
    monkeypatch.setattr(memory_store, "read", lambda *_args: unstorable)
    with pytest.raises(ValueError):
        memory_store.update(memory.id, read_write_set, body="x", description=None)
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", ["name = 'x'\n", "global_project = 3\n",
                                     "global_project = 'nope'\n", "not toml [\n"])
def test_an_invalid_manifest_is_a_storage_failure_and_ensure_global_writes_nothing(tmp_path, content):
    root = tmp_path / "store"
    root.mkdir()
    (root / "store.toml").write_text(content)
    project_store = FileProjectStore(root)
    with pytest.raises(StorageFailure):
        project_store.global_project_id()
    with pytest.raises(StorageFailure):
        project_store.ensure_global()
    assert (root / "store.toml").read_text() == content
    assert not (root / "projects").exists()


def test_a_manifest_naming_a_missing_project_is_a_storage_failure(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    (root / "store.toml").write_text(f"global_project = '{new_id()}'\n")
    with pytest.raises(StorageFailure):
        FileProjectStore(root).global_project_id()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_non_regular_project_files_are_never_opened(tmp_path, kind):
    root = tmp_path / "store"
    (root / "projects").mkdir(parents=True)
    project_id = new_id()
    path = root / "projects" / f"{project_id}.toml"
    if kind == "symlink":
        target = tmp_path / "t.toml"
        target.write_text("name = 'x'\n")
        os.symlink(target, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    with pytest.raises(StorageFailure):
        _call_with_timeout(FileProjectStore(root).read, project_id,
                           unblock=path if kind == "fifo" else None)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_a_non_regular_manifest_is_never_opened(tmp_path, kind):
    root = tmp_path / "store"
    root.mkdir()
    path = root / "store.toml"
    if kind == "symlink":
        target = tmp_path / "m.toml"
        target.write_text(f"global_project = '{new_id()}'\n")
        os.symlink(target, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    project_store = FileProjectStore(root)
    unblock = path if kind == "fifo" else None
    with pytest.raises(StorageFailure):
        _call_with_timeout(project_store.global_project_id, unblock=unblock)
    with pytest.raises(StorageFailure):
        _call_with_timeout(project_store.ensure_global, unblock=unblock)
    assert not (root / "projects").exists()


def test_a_forced_id_collision_leaves_the_existing_file_byte_for_byte(world):
    root, memory_store, _, project_id, _, read_write_set = world
    memory = _m(project_id, body="first")
    memory_store.record(memory, read_write_set)
    path = root / "memories" / f"{memory.id}.md"
    before = path.read_bytes()
    clash = Memory(**{**memory.__dict__, "body": "second", "description": "other"})
    with pytest.raises(IdCollision):
        memory_store.record(clash, read_write_set)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_a_taken_project_id_is_an_id_collision_for_create_and_ensure_global(tmp_path, monkeypatch):
    root = tmp_path / "store"
    project_store = FileProjectStore(root)
    taken = Project.new("taken")
    project_store.create(taken)
    before = (root / "projects" / f"{taken.id}.toml").read_bytes()
    from memriver_core.repository.filesystem import project_store as module
    monkeypatch.setattr(module.Project, "new", classmethod(lambda cls, name: cls(id=taken.id, name=name)))
    with pytest.raises(IdCollision):
        project_store.ensure_global()
    assert (root / "projects" / f"{taken.id}.toml").read_bytes() == before
    assert not (root / "store.toml").exists()


@pytest.mark.parametrize("error", [FileExistsError, OSError])
def test_a_manifest_write_failure_is_final_and_leaves_one_candidate(tmp_path, monkeypatch, error):
    """Only a taken *project* id is a collision. Once the global project file
    is written, a failing manifest write -- even FileExistsError -- is
    StorageFailure, no second candidate project."""
    from memriver_core.bootstrap import build_service
    from memriver_core.config import Settings
    from memriver_core.repository.filesystem import project_store as module

    root = tmp_path / "store"
    real_write_new = module.write_new

    def manifest_fails(store_root, path, text):
        if path.name == "store.toml":
            raise error("manifest refused")
        real_write_new(store_root, path, text)

    monkeypatch.setattr(module, "write_new", manifest_fails)
    service = build_service(Settings(root=root), root=root)
    with pytest.raises(StorageFailure):
        service.ensure_global()
    assert len(list((root / "projects").iterdir())) == 1
    assert not (root / "store.toml").exists()


def test_two_instances_racing_ensure_global_make_one_global(tmp_path):
    root = tmp_path / "store"
    stores = [FileProjectStore(root), FileProjectStore(root)]
    barrier = threading.Barrier(2)
    results: list[str] = []

    def attempt(project_store):
        barrier.wait(timeout=5)
        results.append(project_store.ensure_global())

    threads = [threading.Thread(target=attempt, args=(s,)) for s in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(results) == 2 and results[0] == results[1]
    assert [p.stem for p in (root / "projects").iterdir()] == [results[0]]


@pytest.mark.parametrize("dirname", ["projects", "memories"])
def test_a_symlinked_data_directory_is_never_followed_for_writes(tmp_path, dirname):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / dirname).symlink_to(outside)
    project_store = FileProjectStore(root)
    memory_store = FileMemoryStore(root, project_store)
    if dirname == "projects":
        with pytest.raises(StorageFailure):
            project_store.ensure_global()
        with pytest.raises(StorageFailure):
            project_store.create(Project.new("x"))
    else:
        (root / "memories").unlink()
        global_id = project_store.ensure_global()
        project = Project.new("x")
        project_store.create(project)
        (root / "memories").symlink_to(outside)
        read_write_set = ReadWriteSet(project_id=project.id, global_project_id=global_id)
        with pytest.raises(StorageFailure):
            memory_store.record(_m(project.id), read_write_set)
    assert list(outside.iterdir()) == []


def test_a_symlinked_data_directory_is_never_followed_for_reads(tmp_path):
    root = tmp_path / "store"
    project_store = FileProjectStore(root)
    global_id = project_store.ensure_global()
    project = Project.new("x")
    project_store.create(project)
    memory = _m(project.id)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{memory.id}.md").write_text(encode(memory), encoding="utf-8")
    (outside / f"{project.id}.toml").write_text("name = 'x'\n")
    (root / "memories").symlink_to(outside)
    read_write_set = ReadWriteSet(project_id=project.id, global_project_id=global_id)
    memory_store = FileMemoryStore(root, project_store)
    with pytest.raises(StorageFailure):
        memory_store.read(memory.id, read_write_set)
    with pytest.raises(StorageFailure):
        project_store.search(project.id, read_write_set, query=None, limit=None)
    projects_dir = root / "projects"
    for child in projects_dir.iterdir():
        child.unlink()
    projects_dir.rmdir()
    projects_dir.symlink_to(outside)
    with pytest.raises(StorageFailure):
        project_store.read(project.id)


def test_a_symlinked_store_root_is_honoured_as_configured(tmp_path):
    real = tmp_path / "real-store"
    real.mkdir()
    (tmp_path / "link-store").symlink_to(real)
    project_store = FileProjectStore(tmp_path / "link-store")
    global_id = project_store.ensure_global()
    assert (real / "projects" / f"{global_id}.toml").is_file()


# 'name = "line\\nbreak"' is a TOML basic string: the escape decodes to a real
# newline, which a project name may not hold. (A single-quoted TOML literal
# 'line\nbreak' would keep backslash-n as two characters -- a legal name.)
@pytest.mark.parametrize("content", ["", "name = ''\n", "name = 'a'\nextra = 1\n",
                                     'name = "line\\nbreak"\n', 'name = "tab\\u0007bell"\n',
                                     "name = 5\n"])
def test_an_invalid_project_document_is_a_storage_failure(tmp_path, content):
    root = tmp_path / "store"
    (root / "projects").mkdir(parents=True)
    project_id = new_id()
    (root / "projects" / f"{project_id}.toml").write_text(content)
    with pytest.raises(StorageFailure):
        FileProjectStore(root).read(project_id)


def test_search_skips_undecodable_and_unaddressable_files(world):
    root, memory_store, project_store, project_id, _, read_write_set = world
    good = _m(project_id, body="good")
    memory_store.record(good, read_write_set)
    (root / "memories" / f"{new_id()}.md").write_text("broken\n", encoding="utf-8")
    (root / "memories" / "not-an-id.md").write_text(encode(_m(project_id)), encoding="utf-8")
    assert project_store.search(project_id, read_write_set, query=None, limit=None) == [good]


def test_a_scan_checks_the_memories_container_exactly_once(world, monkeypatch):
    _root, memory_store, project_store, project_id, _, read_write_set = world
    memory_store.record(_m(project_id), read_write_set)
    memory_store.record(_m(project_id), read_write_set)
    from memriver_core.repository.filesystem import memory_files as module

    real_check = module.data_dir_exists
    calls = []

    def counting_check(*args, **kwargs):
        calls.append(1)
        return real_check(*args, **kwargs)

    monkeypatch.setattr(module, "data_dir_exists", counting_check)
    project_store.search(project_id, read_write_set, query=None, limit=None)
    assert len(calls) == 1


def test_a_file_deleted_between_listing_and_read_is_skipped(world, monkeypatch, caplog):
    _root, memory_store, project_store, project_id, _, read_write_set = world
    first = _m(project_id)
    second = _m(project_id)
    memory_store.record(first, read_write_set)
    memory_store.record(second, read_write_set)
    from memriver_core.repository.filesystem import memory_files as module

    real_reader = module._read_memory_file
    order = sorted(m.id for m in (first, second))
    doomed = order[1]

    def delete_then_read(root_arg, memory_id):
        if memory_id == doomed:
            (root_arg / "memories" / f"{doomed}.md").unlink()
        return real_reader(root_arg, memory_id)

    monkeypatch.setattr(module, "_read_memory_file", delete_then_read)
    survivor = first if first.id != doomed else second
    with caplog.at_level(logging.DEBUG, logger="memriver_core"):
        result = project_store.search(project_id, read_write_set, query=None, limit=None)
    assert result == [survivor]
    # absent (deleted mid-scan) is not damaged: no "skipping unusable" warning
    assert not any("skipping unusable memory file" in r.getMessage() for r in caplog.records)


def test_a_failed_create_leaves_no_temp_file(world, monkeypatch):
    root, memory_store, _, project_id, _, read_write_set = world
    from memriver_core.repository.filesystem import files

    def fail(*_args):
        raise OSError("link failed")

    monkeypatch.setattr(files.os, "link", fail)
    with pytest.raises(StorageFailure):
        memory_store.record(_m(project_id), read_write_set)
    assert [p.name for p in (root / "memories").iterdir()] == []


def test_a_cross_project_scan_never_logs_another_projects_raw_fields(world, caplog):
    root, _, project_store, project_id, _, read_write_set = world
    other = Project.new("other")
    project_store.create(other)
    sentinel = "B_PRIVATE_VALUE_DO_NOT_DISCLOSE"
    text = encode(_m(other.id)).replace("type: project", f"type: {sentinel}")
    (root / "memories").mkdir(exist_ok=True)
    (root / "memories" / f"{new_id()}.md").write_text(text, encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="memriver_core"):
        result = project_store.search(project_id, read_write_set, query=None, limit=None)
    assert sentinel not in repr(result)
    assert caplog.records                     # the coercion is still diagnosed
    assert all(sentinel not in r.getMessage() for r in caplog.records)
    assert sentinel not in caplog.text


def test_a_committed_create_is_success_even_if_the_temp_cleanup_fails(tmp_path, monkeypatch):
    from memriver_core.repository.filesystem import files

    def fail(*_args, **_kwargs):
        raise PermissionError(13, "cleanup denied")

    monkeypatch.setattr(files.os, "unlink", fail)
    path = tmp_path / "store" / "memories" / "x.md"
    files.write_new(tmp_path / "store", path, "text")
    assert path.read_text(encoding="utf-8") == "text"


def test_a_link_collision_is_not_masked_by_a_failed_cleanup(world, monkeypatch):
    root, memory_store, _, project_id, _, read_write_set = world
    from memriver_core.repository.filesystem import files

    def collide(*_args, **_kwargs):
        raise FileExistsError(17, "taken")

    def fail(*_args, **_kwargs):
        raise PermissionError(13, "cleanup denied")

    monkeypatch.setattr(files.os, "link", collide)
    monkeypatch.setattr(files.os, "unlink", fail)
    with pytest.raises(FileExistsError):
        files.write_new(root, root / "memories" / "x.md", "text")
    with pytest.raises(IdCollision):
        memory_store.record(_m(project_id), read_write_set)


def test_a_failed_replace_is_not_masked_by_a_failed_cleanup(tmp_path, monkeypatch):
    from memriver_core.repository.filesystem import files

    class ReplaceFailed(Exception):
        pass

    def replace_fails(*_args, **_kwargs):
        raise ReplaceFailed

    def fail(*_args, **_kwargs):
        raise PermissionError(13, "cleanup denied")

    monkeypatch.setattr(files.os, "replace", replace_fails)
    monkeypatch.setattr(files.os, "unlink", fail)
    with pytest.raises(ReplaceFailed):
        files.replace_file(tmp_path, tmp_path / "x.md", "text")


def test_a_failed_temp_write_is_not_masked_by_a_failed_cleanup(tmp_path, monkeypatch):
    from memriver_core.repository.filesystem import files

    def fail(*_args, **_kwargs):
        raise PermissionError(13, "cleanup denied")

    monkeypatch.setattr(files.os, "unlink", fail)
    with pytest.raises(UnicodeEncodeError):       # a lone surrogate cannot be written as UTF-8
        files.write_new(tmp_path, tmp_path / "x.md", "a\udc80b")
