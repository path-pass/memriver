import os
import stat
import time

import pytest
from memriver_core.models import Memory, Project, ReadWriteSet, new_id
from memriver_core.models.errors import StorageFailure
from memriver_core.repository.filesystem import (
    FileMemoryStore,
    FileProjectStore,
    FilesystemStoreInspector,
)
from memriver_core.repository.filesystem.markdown_codec import encode

SOURCE = {"harness": "test", "method": "agent"}


def _initialized(tmp_path):
    root = tmp_path / "store"
    project_store = FileProjectStore(root)
    global_id = project_store.ensure_global()
    project = Project.new("mine")
    project_store.create(project)
    return root, project_store, global_id, project.id


def _kinds(report):
    return [(f.kind, f.location_hint) for f in report.findings]


def test_missing_root_is_uninitialized_with_nothing_else(tmp_path):
    report = FilesystemStoreInspector(tmp_path / "absent").inspect()
    assert (report.initialized, report.entries, report.projects, report.findings) == \
        (False, (), (), ())


def test_a_root_without_manifest_is_uninitialized(tmp_path):
    (tmp_path / "store").mkdir()
    assert FilesystemStoreInspector(tmp_path / "store").inspect().initialized is False


def test_a_healthy_store_lists_projects_and_memories(tmp_path):
    root, project_store, global_id, project_id = _initialized(tmp_path)
    memory = Memory.new(body="b", type="user", project_id=project_id, source=SOURCE)
    FileMemoryStore(root, project_store).record(
        memory, ReadWriteSet(project_id=project_id, global_project_id=global_id))
    report = FilesystemStoreInspector(root).inspect()
    assert report.initialized is True
    assert set(report.projects) == {global_id, project_id}
    assert [(e.memory.id, e.location_hint) for e in report.entries] == \
        [(memory.id, f"memories/{memory.id}.md")]
    assert report.findings == ()


def _write_memory_file(root, name, text):
    (root / "memories").mkdir(exist_ok=True)
    path = root / "memories" / name
    path.write_text(text, encoding="utf-8")
    return path


def test_each_bad_memory_file_is_one_relative_finding(tmp_path):
    root, _, _, project_id = _initialized(tmp_path)
    good = Memory.new(body="b", type="user", project_id=project_id, source=SOURCE)
    other_id = new_id()
    broken_id = new_id()
    _write_memory_file(root, "not-an-id.md", encode(good))
    _write_memory_file(root, f"{broken_id}.md", "hand notes\n")
    _write_memory_file(root, f"{other_id}.md", encode(good))            # id mismatch
    orphan = Memory.new(body="b", type="user", project_id=new_id(), source=SOURCE)
    _write_memory_file(root, f"{orphan.id}.md", encode(orphan))
    report = FilesystemStoreInspector(root).inspect()
    assert sorted(_kinds(report)) == sorted([
        ("unaddressable-id", "memories/not-an-id.md"),
        ("unparsable", f"memories/{broken_id}.md"),
        ("id-stem-mismatch", f"memories/{other_id}.md"),
        ("unknown-project", f"memories/{orphan.id}.md"),
    ])
    assert [e.memory.id for e in report.entries] == [orphan.id]   # listed, and flagged
    assert all(not f.location_hint.startswith("/") for f in report.findings)


# 8 nested alias levels: ~600 bytes of YAML, 10**8 leaves if every alias were expanded
_ALIAS_BOMB = "\n".join(
    ["  l0: &l0 [" + ", ".join(["a"] * 10) + "]"]
    + [f"  l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]" for i in range(1, 8)])
# one 20k-char scalar aliased 2000 times: a ~26 KB file, a 40 MB value if every alias were expanded
_SCALAR_ALIAS = '  s: &s "' + "a" * 20_000 + '"\n  r: [' + ", ".join(["*s"] * 2000) + "]"


@pytest.mark.parametrize(("old", "new"), [
    ("description: cue", 'description: "\\udc80"'),
    ("harness: test", 'harness: "\\udc80"'),
    ("harness: test", 'harness: test\n  tags: !!set {"\\udc80": null}'),
    ("harness: test", 'harness: test\n  blob: !!binary gA=='),
    ("trust: agent", 'trust: !!binary gA=='),
    ("harness: test", 'harness: test\n  r: &x [*x]'),  # a cycle has no JSON form
    ("harness: test", 'harness: test\n  r: &x {k: *x}'),
    ("harness: test", 'harness: test\n  a: &x [1, 2]\n  b: *x'),  # memriver never writes aliases
    pytest.param("harness: test", "harness: test\n" + _ALIAS_BOMB, id="alias-bomb"),
    pytest.param("harness: test", "harness: test\n" + _SCALAR_ALIAS, id="scalar-alias"),
])
def test_an_unservable_stored_value_is_unparsable(tmp_path, old, new):
    root, _, _, project_id = _initialized(tmp_path)
    memory = Memory.new(body="b", type="user", project_id=project_id, source=SOURCE,
                        description="cue")
    text = encode(memory).replace(old, new)
    assert new in text
    _write_memory_file(root, f"{memory.id}.md", text)
    start = time.monotonic()
    report = FilesystemStoreInspector(root).inspect()
    assert time.monotonic() - start < 1.0
    assert _kinds(report) == [("unparsable", f"memories/{memory.id}.md")]
    assert report.entries == ()


def test_an_unreadable_memory_file_is_reported_not_raised(tmp_path):
    root, _, _, project_id = _initialized(tmp_path)
    memory = Memory.new(body="b", type="user", project_id=project_id, source=SOURCE)
    path = _write_memory_file(root, f"{memory.id}.md", encode(memory))
    path.chmod(0)
    try:
        assert _kinds(FilesystemStoreInspector(root).inspect()) == \
            [("unreadable-file", f"memories/{memory.id}.md")]
    finally:
        path.chmod(0o600)


def _call_with_timeout(fn, *args, seconds: float = 5.0, unblock=None):
    """Run ``fn(*args)`` on a thread; a call that blocks on a FIFO fails instead of hanging."""
    import threading

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


def test_invalid_project_files_and_manifest_are_reported(tmp_path):
    root, _, _, _ = _initialized(tmp_path)
    bad_id = new_id()
    (root / "projects" / f"{bad_id}.toml").write_text("name = 5\n")
    (root / "projects" / "not-an-id.toml").write_text("name = 'x'\n")
    fifo = root / "projects" / f"{new_id()}.toml"
    os.mkfifo(fifo)
    (root / "store.toml").write_text("global_project = 'nope'\n")
    report = _call_with_timeout(FilesystemStoreInspector(root).inspect, unblock=fifo)
    kinds = [k for k, _ in _kinds(report)]
    assert kinds.count("invalid-project") == 2
    assert ("unreadable-file", f"projects/{fifo.name}") in _kinds(report)
    assert ("invalid-manifest", "store.toml") in _kinds(report)
    assert report.initialized is True
    assert bad_id not in report.projects


def test_the_pre_release_layout_is_reported_and_never_modified(tmp_path):
    root = tmp_path / "store"
    (root / "global" / "entries").mkdir(parents=True)
    (root / "global" / "entries" / "tea.md").write_text("old")
    (root / "projects" / "demo-0123456789abcdef" / "entries").mkdir(parents=True)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = FilesystemStoreInspector(root).inspect()
    assert sorted(_kinds(report)) == [("legacy-layout", "global"),
                                      ("legacy-layout", "projects/demo-0123456789abcdef")]
    assert report.initialized is False
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_findings_are_in_stable_location_order(tmp_path):
    root, _, _, _ = _initialized(tmp_path)
    for _ in range(3):
        _write_memory_file(root, f"{new_id()}.md", "x\n")
    first = FilesystemStoreInspector(root).inspect().findings
    assert [f.location_hint for f in first] == sorted(f.location_hint for f in first)
    assert FilesystemStoreInspector(root).inspect().findings == first


def test_root_that_is_a_file_raises_opaque_storage_failure(tmp_path):
    path = tmp_path / "store"
    path.write_text("x")
    with pytest.raises(StorageFailure):
        FilesystemStoreInspector(path).inspect()


@pytest.mark.parametrize("occupant", ["file", "symlink"])
def test_a_data_directory_that_is_not_a_real_directory_is_a_finding_and_never_followed(
        tmp_path, occupant):
    root, _, _, project_id = _initialized(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    orphan = Memory.new(body="b", type="user", project_id=project_id, source=SOURCE)
    (outside / f"{orphan.id}.md").write_text(encode(orphan), encoding="utf-8")
    if occupant == "file":
        (root / "memories").write_text("not a directory")
    else:
        (root / "memories").symlink_to(outside)
    report = FilesystemStoreInspector(root).inspect()
    assert ("unsafe-container", "memories") in _kinds(report)
    assert report.entries == ()


def test_a_stray_that_vanishes_after_the_listing_is_skipped(tmp_path, monkeypatch):
    root, _, _, _ = _initialized(tmp_path)
    temp = root / "projects" / "tmpabcd.tmp"
    temp.write_text("half-written")
    real_lstat = type(temp).lstat

    def vanishing(self, *args, **kwargs):
        if self.name == "tmpabcd.tmp":
            raise FileNotFoundError(2, "gone")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(type(temp), "lstat", vanishing)
    assert FilesystemStoreInspector(root).inspect().findings == ()


def test_an_unstattable_stray_is_an_opaque_storage_failure(tmp_path, monkeypatch):
    root, _, _, _ = _initialized(tmp_path)
    stray = root / "projects" / "stray"
    stray.write_text("x")
    real_lstat = type(stray).lstat

    def denied(self, *args, **kwargs):
        if self.name == "stray":
            raise PermissionError(13, "denied /secret/path")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(type(stray), "lstat", denied)
    with pytest.raises(StorageFailure):
        FilesystemStoreInspector(root).inspect()


def test_an_unenumerable_memories_directory_raises_storage_failure(tmp_path):
    root, _, _, _ = _initialized(tmp_path)
    (root / "memories").mkdir()
    (root / "memories").chmod(0)
    try:
        with pytest.raises(StorageFailure):
            FilesystemStoreInspector(root).inspect()
    finally:
        (root / "memories").chmod(stat.S_IRWXU)


def test_an_unreadable_project_file_is_an_unreadable_file_finding(tmp_path):
    root, _, _, project_id = _initialized(tmp_path)
    path = root / "projects" / f"{project_id}.toml"
    path.chmod(0)
    try:
        report = FilesystemStoreInspector(root).inspect()
    finally:
        path.chmod(0o600)
    assert [(f.kind, f.location_hint, f.project_id) for f in report.findings] == \
        [("unreadable-file", f"projects/{project_id}.toml", project_id)]
    assert project_id not in report.projects


def test_an_unreadable_store_root_raises_storage_failure_not_findings(tmp_path):
    root, _, _, _ = _initialized(tmp_path)
    root.chmod(0)
    try:
        with pytest.raises(StorageFailure):
            FilesystemStoreInspector(root).inspect()
    finally:
        root.chmod(stat.S_IRWXU)


def test_a_failed_container_lstat_raises_storage_failure(tmp_path, monkeypatch):
    root, _, _, _ = _initialized(tmp_path)
    real_lstat = os.lstat

    def denied(path, *args, **kwargs):
        if os.fspath(path).endswith("/memories"):
            raise PermissionError(13, "denied /secret/path")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    with pytest.raises(StorageFailure):
        FilesystemStoreInspector(root).inspect()
