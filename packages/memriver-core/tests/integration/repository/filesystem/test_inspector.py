import os
from pathlib import Path

import pytest
from memriver_core.application.errors import StorageFailure
from memriver_core.models import Memory, ProjectId, Scope, StoreReport
from memriver_core.repository.filesystem import FilesystemStoreInspector
from memriver_core.repository.filesystem.markdown_codec import encode

SOURCE = {"harness": "test", "session": "s", "method": "explicit"}

GLOBAL = Scope.global_()
PROJECT = Scope.project(ProjectId("mine-000000"))


def _entries_dir(root: Path, scope: Scope) -> Path:
    leaf = "global" if scope.project_id is None else f"projects/{scope.project_id}"
    return root / leaf / "entries"


def _write_raw(root: Path, directory_scope: Scope, name: str, text: str) -> Path:
    path = _entries_dir(root, directory_scope) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_memory(root: Path, *, id: str, scope: Scope,
                 directory_scope: Scope | None = None,
                 name: str | None = None) -> Path:
    """Seed one encoded memory; the directory and file name may be forced apart."""
    memory = Memory.new(body="内容", type="project", scope=scope, source=SOURCE, id=id)
    return _write_raw(root, directory_scope if directory_scope is not None else scope,
                      name if name is not None else f"{id}.md", encode(memory))


def build_pathological_file(root: Path, fixture_name: str, monkeypatch) -> Path:
    """One broken entry of the named shape; returns its absolute path."""
    if fixture_name == "unparsable":
        # no frontmatter at all: decode fails on the missing keys
        return _write_raw(root, GLOBAL, "broken.md", "just a hand-written note\n")
    if fixture_name == "bad-scope":
        # a stored scope outside the grammar -> UnparsableStoredScope
        text = encode(Memory.new(body="b", type="project", scope=GLOBAL,
                                 source=SOURCE, id="odd-scope"))
        return _write_raw(root, GLOBAL, "odd-scope.md",
                          text.replace("scope: global", "scope: neither-nor"))
    if fixture_name == "not-utf8":
        # readable bytes that are not text: nothing to decode, not an access problem
        path = _entries_dir(root, GLOBAL) / "binary.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe\x00garbage\x80\x81")
        return path
    if fixture_name == "wrong-directory":
        return write_memory(root, id="misplaced", scope=PROJECT, directory_scope=GLOBAL)
    if fixture_name == "wrong-stem":
        return write_memory(root, id="bar", scope=GLOBAL, name="foo.md")
    if fixture_name == "wrong-directory-and-stem":
        # wrong on both counts at once: scope must win (precedence 4 before 5)
        return write_memory(root, id="bar", scope=PROJECT, directory_scope=GLOBAL,
                            name="foo.md")
    if fixture_name == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root ignores file permissions")
        path = write_memory(root, id="locked", scope=GLOBAL)
        path.chmod(0o000)
        return path
    raise AssertionError(f"unknown fixture: {fixture_name}")


def raise_permission_error(*args, **kwargs):
    raise PermissionError(13, "Permission denied")


def test_missing_root_is_uninitialized(tmp_path):
    report = FilesystemStoreInspector(tmp_path / "missing").inspect()
    assert report == StoreReport(initialized=False, entries=(), findings=())


def test_existing_empty_root_is_initialized(tmp_path):
    report = FilesystemStoreInspector(tmp_path).inspect()
    assert report.initialized is True
    assert report.entries == () and report.findings == ()


def test_bad_name_stays_listed_and_is_unaddressable(tmp_path):
    path = write_memory(tmp_path, id="Bad_Name", scope=Scope.global_())
    report = FilesystemStoreInspector(tmp_path).inspect()
    assert [item.memory.id for item in report.entries] == ["Bad_Name"]
    assert [(f.kind, f.location_hint) for f in report.findings] == [
        ("unaddressable-id", path.relative_to(tmp_path).as_posix())
    ]


@pytest.mark.parametrize(
    ("fixture_name", "expected_kind"),
    [
        ("unparsable", "unparsable"),
        ("not-utf8", "unparsable"),
        ("bad-scope", "scope-directory-mismatch"),
        ("wrong-directory", "scope-directory-mismatch"),
        ("wrong-stem", "id-stem-mismatch"),
        ("unreadable", "unreadable-file"),
    ],
)
def test_pathological_file_becomes_a_relative_finding(
        tmp_path, fixture_name, expected_kind, monkeypatch):
    path = build_pathological_file(tmp_path, fixture_name, monkeypatch)
    report = FilesystemStoreInspector(tmp_path).inspect()
    finding = next(f for f in report.findings if f.kind == expected_kind)
    assert finding.location_hint == path.relative_to(tmp_path).as_posix()
    assert str(tmp_path) not in finding.reason
    # a broken file is never listed as a readable entry
    assert report.entries == ()


def test_unenumerable_root_raises_opaque_storage_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "iterdir", raise_permission_error)
    with pytest.raises(StorageFailure) as caught:
        FilesystemStoreInspector(tmp_path).inspect()
    assert str(tmp_path) not in str(caught.value)


def test_root_that_is_a_file_raises_opaque_storage_failure(tmp_path):
    path = tmp_path / "store"
    path.write_text("not a store", encoding="utf-8")
    with pytest.raises(StorageFailure) as caught:
        FilesystemStoreInspector(path).inspect()
    assert str(path) not in str(caught.value)


def test_every_scope_is_listed_in_relative_location_order(tmp_path):
    write_memory(tmp_path, id="zeta", scope=GLOBAL)
    write_memory(tmp_path, id="alpha", scope=GLOBAL)
    write_memory(tmp_path, id="mid", scope=PROJECT)
    report = FilesystemStoreInspector(tmp_path).inspect()
    assert [item.location_hint for item in report.entries] == [
        "global/entries/alpha.md",
        "global/entries/zeta.md",
        f"projects/{PROJECT.project_id}/entries/mid.md",
    ]
    assert report.findings == ()


def test_files_outside_the_entry_layout_are_ignored(tmp_path):
    write_memory(tmp_path, id="kept", scope=GLOBAL)
    _write_raw(tmp_path, GLOBAL, "notes.txt", "not an entry")
    (tmp_path / "global" / "README.md").write_text("layout note", encoding="utf-8")
    (tmp_path / "index.md").write_text("layout note", encoding="utf-8")
    (tmp_path / "projects").mkdir(exist_ok=True)
    (tmp_path / "projects" / "stray.md").write_text("layout note", encoding="utf-8")
    report = FilesystemStoreInspector(tmp_path).inspect()
    assert [item.memory.id for item in report.entries] == ["kept"]
    assert report.findings == ()


def test_scope_mismatch_outranks_stem_mismatch(tmp_path, monkeypatch):
    # both checks fail on one file: the store must report the physical
    # contradiction (a project memory sitting under global/) exactly once, not
    # the id-stem mismatch that a swapped precedence would surface instead
    path = build_pathological_file(tmp_path, "wrong-directory-and-stem", monkeypatch)
    report = FilesystemStoreInspector(tmp_path).inspect()
    assert [(f.kind, f.location_hint) for f in report.findings] == [
        ("scope-directory-mismatch", path.relative_to(tmp_path).as_posix())
    ]
    assert report.entries == ()
