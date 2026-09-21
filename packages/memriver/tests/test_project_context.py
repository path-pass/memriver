from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest
from memriver import project_context
from memriver.project_context import (
    ProjectResolution,
    RegisteredProject,
    Registry,
    RegistryInvalid,
    bind,
    count_child_git_markers,
    covers,
    find_git_root,
    load_registry,
    new_project_id,
    project_exists,
    resolve,
    resolve_project,
    root_integrity,
    unbind,
)
from memriver_core.models import AccessContext, ProjectId

A = ProjectId("a-0123456789abcdef")
B = ProjectId("b-0123456789abcdef")


def _register(store: Path, project_id: str, roots: list[str] | None) -> None:
    d = store / "projects" / project_id
    d.mkdir(parents=True, exist_ok=True)
    if roots is not None:
        body = "roots = [" + ", ".join(f'"{r}"' for r in roots) + "]\n"
        (d / "project.toml").write_text(body, encoding="utf-8")


def _registry(*pairs: tuple[str, list[str]]) -> Registry:
    return Registry(tuple(RegisteredProject(ProjectId(i), tuple(r)) for i, r in pairs))


def _case_insensitive(monkeypatch):
    """Simulate APFS: two spellings differing only in case are one directory."""
    real = project_context.same_directory

    def fake(a: str, b: str):
        if a.lower() == b.lower() and a != b:
            return True
        return real(a, b)

    monkeypatch.setattr(project_context, "same_directory", fake)


def _unverifiable(monkeypatch):
    """Every samefile check fails (permission, I/O)."""
    monkeypatch.setattr(project_context, "same_directory", lambda a, b: None)


def test_find_git_root_is_kept_for_installers(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert find_git_root(tmp_path / "repo" / "src") == (tmp_path / "repo").resolve()
    assert find_git_root(tmp_path) is None


# --- load_registry ---

def test_missing_projects_dir_is_an_empty_registry(tmp_path):
    assert load_registry(tmp_path / "store") == Registry(())
    assert not (tmp_path / "store").exists()


def test_project_without_file_is_unbound(tmp_path):
    _register(tmp_path, "old-abc123", None)
    assert load_registry(tmp_path).projects == (RegisteredProject(ProjectId("old-abc123"), ()),)


def test_bound_project_keeps_roots_as_stored(tmp_path):
    _register(tmp_path, A, ["/x/work", "/y/work"])
    (project,) = load_registry(tmp_path).projects
    assert project.roots == ("/x/work", "/y/work")


@pytest.mark.parametrize("body, reason", [
    ("roots = [\n", "project file is not valid TOML"),
    ('roots = ["/x"]\nname = "x"\n', "project file has a key other than roots"),
    # a file with keys but no roots is the missing key, not the extra one
    ('name = "x"\n', "project file has no roots key"),
    ('roots = "/x"\n', "roots is not an array of strings"),
    ("roots = [1]\n", "roots is not an array of strings"),
    ('roots = ["relative/path"]\n', "root is not an absolute path"),
    ('roots = ["/x/\\u0000y"]\n', "root is not an addressable path"),
])
def test_invalid_project_file_names_location_and_fixed_reason(tmp_path, body, reason):
    d = tmp_path / "projects" / A
    d.mkdir(parents=True)
    (d / "project.toml").write_text(body, encoding="utf-8")
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert info.value.location == f"projects/{A}/project.toml"
    assert info.value.reason == reason


def test_invalid_file_error_never_echoes_the_offending_value(tmp_path):
    d = tmp_path / "projects" / A
    d.mkdir(parents=True)
    (d / "project.toml").write_text('roots = ["SENTINEL-relative/path"]\n', encoding="utf-8")
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert "SENTINEL" not in str(info.value)


def test_project_file_without_a_roots_key_names_the_missing_key(tmp_path):
    d = tmp_path / "projects" / A
    d.mkdir(parents=True)
    (d / "project.toml").write_text("", encoding="utf-8")
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert (info.value.location, info.value.reason) == (
        f"projects/{A}/project.toml", "project file has no roots key")


def test_project_file_that_is_a_symlink_is_never_followed(tmp_path):
    # a live link out of the store would let anything outside it decide which
    # directories the project owns
    outside = tmp_path / "outside.toml"
    outside.write_text('roots = ["/x/work"]\n', encoding="utf-8")
    d = tmp_path / "projects" / A
    d.mkdir(parents=True)
    (d / "project.toml").symlink_to(outside)
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert info.value.reason == "project file could not be read"


def test_dangling_project_file_symlink_is_unreadable_not_missing(tmp_path):
    d = tmp_path / "projects" / A
    d.mkdir(parents=True)
    (d / "project.toml").symlink_to(tmp_path / "nowhere.toml")
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert info.value.reason == "project file could not be read"


def test_projects_dir_that_is_a_symlink_is_invalid_not_empty(tmp_path):
    (tmp_path / "projects").symlink_to(tmp_path / "nowhere")
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert (info.value.location, info.value.reason) == ("projects", "project directory could not be read")
    (tmp_path / "projects").unlink()
    (tmp_path / "projects").write_text("not a directory")
    with pytest.raises(RegistryInvalid):
        load_registry(tmp_path)


def test_project_dir_that_is_a_symlink_is_invalid_not_skipped(tmp_path):
    real = tmp_path / "elsewhere" / A
    real.mkdir(parents=True)
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / A).symlink_to(real)
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert (info.value.location, info.value.reason) == (f"projects/{A}", "project directory could not be read")


def test_unverifiable_comparison_makes_the_registry_invalid(tmp_path, monkeypatch):
    _register(tmp_path, A, ["/x/work"])
    _register(tmp_path, B, ["/y/work"])
    _unverifiable(monkeypatch)
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert info.value.reason == "root could not be checked"


def test_bad_directory_name_is_invalid(tmp_path):
    (tmp_path / "projects" / "Bad_Name").mkdir(parents=True)
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert (info.value.location, info.value.reason) == (
        "projects/Bad_Name", "project directory name is not an addressable project id")


def test_same_root_under_two_ids_is_invalid(tmp_path):
    _register(tmp_path, A, ["/x/work"])
    _register(tmp_path, B, ["/x/work"])
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert info.value.reason == "root is already bound to another project"
    assert info.value.location == f"projects/{B}/project.toml"


def test_alias_roots_under_two_ids_are_invalid(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    real = tmp_path / "Work"
    real.mkdir()
    _register(tmp_path / "store", A, [str(real)])
    _register(tmp_path / "store", B, [str(tmp_path / "work")])
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path / "store")
    assert info.value.reason == "root is already bound to another project"


def test_alias_roots_under_one_id_are_legal(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    real = tmp_path / "Work"
    real.mkdir()
    _register(tmp_path / "store", A, [str(real), str(tmp_path / "work")])
    (project,) = load_registry(tmp_path / "store").projects
    assert len(project.roots) == 2


def test_stray_file_under_projects_is_ignored(tmp_path):
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "notes.txt").write_text("x")
    assert load_registry(tmp_path) == Registry(())


# --- resolve_project ---

def test_nearest_registered_ancestor_wins(tmp_path):
    parent = (tmp_path / "work").resolve()
    child = parent / "frontend"
    (child / "src").mkdir(parents=True)
    (parent / "backend").mkdir()
    registry = _registry((A, [str(parent)]), (B, [str(child)]))
    res = resolve_project(registry, child / "src")
    assert (res.state, res.project_id, res.root) == ("registered", B, str(child))
    assert resolve_project(registry, parent / "backend").project_id == A
    assert resolve_project(registry, parent).project_id == A


def test_unregistered_directory_is_none(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    res = resolve_project(_registry(), tmp_path / "repo")
    assert res == ProjectResolution(state="none", project_id=None, root=None, diagnostic=None)
    assert res.context() == AccessContext(project_id=None)


def test_case_alias_matches_by_samefile(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    real = (tmp_path / "Work").resolve()
    real.mkdir()
    registry = _registry((A, [str(tmp_path.resolve() / "work")]))
    res = resolve_project(registry, real)
    assert res.state == "registered" and res.root == str(tmp_path.resolve() / "work")


def test_nearer_alias_beats_farther_exact_match(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    parent = (tmp_path / "work").resolve()
    child = parent / "Child"
    child.mkdir(parents=True)
    registry = _registry((A, [str(parent)]), (B, [str(parent / "child")]))
    assert resolve_project(registry, child).project_id == B


def test_exact_and_alias_of_different_ids_at_one_level_is_degraded(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    real = (tmp_path / "Work").resolve()
    real.mkdir()
    registry = _registry((A, [str(real)]), (B, [str(tmp_path.resolve() / "work")]))
    res = resolve_project(registry, real)
    assert res.state == "degraded" and res.diagnostic.endswith("matched by more than one project")


def test_offline_root_is_matched_by_string_only(tmp_path):
    gone = str((tmp_path / "gone").resolve())
    registry = _registry((A, [gone]))
    assert resolve_project(registry, tmp_path).state == "none"
    (tmp_path / "gone").mkdir()
    assert resolve_project(registry, tmp_path / "gone").project_id == A


def test_root_replaced_by_symlink_is_degraded_and_never_authorizes_target(tmp_path):
    old = (tmp_path / "old").resolve()
    new = (tmp_path / "new").resolve()
    old.mkdir()
    registry = _registry((A, [str(old)]))
    assert resolve_project(registry, old).project_id == A
    old.rmdir()
    new.mkdir()
    old.symlink_to(new)
    res = resolve_project(registry, new)
    assert res.state == "degraded"
    assert res.diagnostic == f"{old}: registered root is no longer a canonical path"
    assert resolve_project(registry, old).state == "degraded"


def test_ancestor_component_replaced_by_symlink_is_degraded(tmp_path):
    x = (tmp_path / "x").resolve()
    (x / "work").mkdir(parents=True)
    registry = _registry((A, [str(x / "work")]))
    y = tmp_path / "y"
    x.rename(y)
    x.symlink_to(y)
    assert resolve_project(registry, y / "work").state == "degraded"
    assert root_integrity(registry) == f"{x / 'work'}: registered root is no longer a canonical path"


def test_redirected_ancestor_with_missing_leaf_is_a_redirect_not_offline(tmp_path):
    x = (tmp_path / "x").resolve()
    x.mkdir()
    registry = _registry((A, [str(x / "work")]))          # leaf never existed under the new target
    y = tmp_path / "y"
    y.mkdir()
    x.rmdir()
    x.symlink_to(y)
    assert root_integrity(registry) == f"{x / 'work'}: registered root is no longer a canonical path"
    assert resolve_project(registry, y).state == "degraded"


def test_fully_absent_root_is_offline(tmp_path):
    registry = _registry((A, [str((tmp_path / "gone" / "deeper").resolve())]))
    assert root_integrity(registry) is None


def test_unverifiable_root_check_is_degraded(tmp_path, monkeypatch):
    real = (tmp_path / "work").resolve()
    real.mkdir()
    registry = _registry((A, [str(real)]))
    _unverifiable(monkeypatch)
    res = resolve_project(registry, tmp_path)
    assert res.state == "degraded" and res.diagnostic == f"{real}: registered root could not be checked"


def test_nul_bearing_root_is_unverifiable_not_raising():
    # os.lstat on a path the OS cannot even address raises ValueError, not
    # OSError; _nearest_existing must treat that the same as "could not be
    # checked" rather than letting it escape root_integrity
    registry = _registry((A, ["/x\x00y"]))
    assert root_integrity(registry) == "/x\x00y: registered root could not be checked"


def test_root_that_cannot_be_lstatted_is_degraded(tmp_path, monkeypatch):
    real = (tmp_path / "work").resolve()
    real.mkdir()
    registry = _registry((A, [str(real)]))
    true_lstat = project_context.os.lstat

    def fake(path, *args, **kwargs):
        if str(path) == str(real):
            raise PermissionError(13, "denied")
        return true_lstat(path, *args, **kwargs)

    monkeypatch.setattr(project_context.os, "lstat", fake)
    assert root_integrity(registry) == f"{real}: registered root could not be checked"
    res = resolve_project(registry, tmp_path)
    assert res.state == "degraded"
    assert res.diagnostic == f"{real}: registered root could not be checked"


def test_covers_refuses_a_path_the_os_cannot_address(tmp_path):
    assert covers(Path("/x\x00y"), tmp_path) is False


def test_covers_is_physical_and_ancestor_aware(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    (tmp_path / "Home" / "x").mkdir(parents=True)
    assert covers(tmp_path / "Home", tmp_path / "Home" / "x") is True
    assert covers(tmp_path / "home", tmp_path / "Home" / "x") is True
    assert covers(tmp_path / "Home" / "x", tmp_path / "Home") is False
    _unverifiable(monkeypatch)
    assert covers(tmp_path / "Other", tmp_path / "Home" / "x") is None


def test_start_that_is_a_file_or_missing_is_degraded(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    for start in (f, tmp_path / "does-not-exist"):
        res = resolve_project(_registry(), start)
        assert res.state == "degraded"
        assert res.diagnostic == "working directory could not be resolved"
        assert res.context() == AccessContext(project_id=None)


# --- resolve (never raises) ---

def test_resolve_survives_a_start_path_the_os_cannot_even_address(tmp_path):
    # the hooks take cwd from the harness payload, and JSON can carry a NUL:
    # resolving it raises ValueError, which must still come back as degraded
    res = resolve(tmp_path / "store", Path("/SENTINEL\x00cwd"))
    assert res.state == "degraded"
    assert res.diagnostic == "working directory could not be resolved"
    assert res.context().project_id is None
    assert "SENTINEL" not in res.diagnostic


def test_resolve_turns_invalid_registry_into_degraded(tmp_path):
    d = tmp_path / "store" / "projects" / A
    d.mkdir(parents=True)
    (d / "project.toml").write_text("roots = [\n")
    res = resolve(tmp_path / "store", tmp_path)
    assert res.state == "degraded"
    assert res.diagnostic == f"projects/{A}/project.toml: project file is not valid TOML"


# --- header ---

def test_header_per_state():
    assert ProjectResolution("registered", A, "/x/work", None).header() == f"project: {A} (root /x/work)"
    assert ProjectResolution("none", None, None, None).header() \
        == "project: none — global is read-only; ask the user to run memriver project init"
    assert ProjectResolution("degraded", None, None, "projects/x/project.toml: bad").header() \
        == ("project: unavailable — registry invalid (projects/x/project.toml: bad); "
            "ask the user to run memriver project explain")


def test_header_neutralizes_control_characters_and_truncates():
    res = ProjectResolution("registered", A, "/x/\nevil " + "a" * 200, None)
    line = res.header()
    assert "\n" not in line and " " not in line
    assert line.startswith(f"project: {A} (root /x/ evil a")
    assert len(line) <= len(f"project: {A} (root ") + 120 + 1


# --- registry writes ---

def test_new_project_id_shape():
    pid = new_project_id("My Work.Dir")
    assert pid.startswith("my-work-dir-") and len(pid) == len("my-work-dir-") + 16
    assert new_project_id("").startswith("project-")
    pid = new_project_id("é" * 300)
    assert len(pid.encode()) <= 255 and pid.startswith("project-")


@pytest.fixture
def dirs(tmp_path):
    """Real, canonical directories to bind: bind refuses paths that do not exist."""
    out = {}
    for name in ("x-work", "y-work", "z-old", "w0", "w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8"):
        (tmp_path / "roots" / name).mkdir(parents=True)
        out[name] = str((tmp_path / "roots" / name).resolve())
    return out


def test_bind_create_makes_private_dir_and_exact_document(tmp_path, dirs):
    store = tmp_path / "store"
    bind(store, A, dirs["x-work"], create=True)
    d = store / "projects" / A
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "project.toml").stat().st_mode) == 0o600
    assert (d / "project.toml").read_text() == f'roots = ["{dirs["x-work"]}"]\n'
    bind(store, A, dirs["y-work"], create=False)
    assert (d / "project.toml").read_text() == f'roots = ["{dirs["x-work"]}", "{dirs["y-work"]}"]\n'
    assert not (d / "entries").exists()


def test_bind_create_and_adopt_existence_rules(tmp_path, dirs):
    with pytest.raises(ValueError, match="no such project"):
        bind(tmp_path, A, dirs["x-work"], create=False)
    bind(tmp_path, A, dirs["x-work"], create=True)
    with pytest.raises(ValueError, match="project id already exists"):
        bind(tmp_path, A, dirs["y-work"], create=True)
    (tmp_path / "projects" / "old-abc123" / "entries").mkdir(parents=True)   # entries-only
    bind(tmp_path, ProjectId("old-abc123"), dirs["z-old"], create=False)
    assert (tmp_path / "projects" / "old-abc123" / "project.toml").read_text() \
        == f'roots = ["{dirs["z-old"]}"]\n'


def test_bind_refuses_a_root_that_vanished_or_was_redirected(tmp_path, dirs):
    with pytest.raises(ValueError, match="not a canonical existing directory"):
        bind(tmp_path, A, str(tmp_path / "never"), create=True)
    link = tmp_path / "link"
    link.symlink_to(dirs["x-work"])
    with pytest.raises(ValueError, match="not a canonical existing directory"):
        bind(tmp_path, A, str(link), create=True)
    assert not (tmp_path / "projects").exists()


def test_bind_refuses_when_a_comparison_cannot_be_checked(tmp_path, dirs, monkeypatch):
    bind(tmp_path, A, dirs["x-work"], create=True)
    _unverifiable(monkeypatch)
    with pytest.raises(ValueError, match="could not be checked"):
        bind(tmp_path, B, dirs["y-work"], create=True)
    assert not (tmp_path / "projects" / B).exists()


def test_bind_same_id_alias_is_a_no_op_and_cross_id_is_refused(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    real = tmp_path / "Work"
    real.mkdir()
    # on a case-sensitive volume the alias spelling must exist too (bind requires an
    # existing canonical directory); on APFS this mkdir is a no-op on the same directory
    (tmp_path / "work").mkdir(exist_ok=True)
    bind(tmp_path / "store", A, str(real), create=True)
    doc = tmp_path / "store" / "projects" / A / "project.toml"
    before = doc.stat().st_mtime_ns
    bind(tmp_path / "store", A, str(real), create=False)
    bind(tmp_path / "store", A, str(tmp_path / "work"), create=False)
    assert doc.read_text() == f'roots = ["{real}"]\n' and doc.stat().st_mtime_ns == before
    with pytest.raises(ValueError, match="already bound to another project"):
        bind(tmp_path / "store", B, str(tmp_path / "work"), create=True)
    assert not (tmp_path / "store" / "projects" / B).exists()


def test_unbind_removes_one_root_even_if_path_is_gone(tmp_path, dirs):
    bind(tmp_path, A, dirs["x-work"], create=True)
    bind(tmp_path, A, dirs["y-work"], create=False)
    import shutil
    shutil.rmtree(dirs["x-work"])                          # moved away: the path is gone
    unbind(tmp_path, A, dirs["x-work"])
    doc = tmp_path / "projects" / A / "project.toml"
    assert doc.read_text() == f'roots = ["{dirs["y-work"]}"]\n'
    unbind(tmp_path, A, dirs["y-work"])
    assert doc.read_text() == "roots = []\n"
    with pytest.raises(ValueError, match="not bound to this project"):
        unbind(tmp_path, A, dirs["y-work"])


def test_bind_refuses_to_write_over_an_invalid_registry(tmp_path, dirs):
    d = tmp_path / "projects" / B
    d.mkdir(parents=True)
    (d / "project.toml").write_text("roots = [\n")
    with pytest.raises(RegistryInvalid):
        bind(tmp_path, A, dirs["x-work"], create=True)
    assert not (tmp_path / "projects" / A).exists()


def test_failed_replace_keeps_old_document_and_no_temp_file(tmp_path, dirs, monkeypatch):
    from memriver_core import StorageFailure

    bind(tmp_path, A, dirs["x-work"], create=True)
    doc = tmp_path / "projects" / A / "project.toml"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(project_context.os, "replace", boom)
    # store_lock wraps every OSError raised inside its block as StorageFailure
    with pytest.raises(StorageFailure):
        bind(tmp_path, A, dirs["y-work"], create=False)
    assert doc.read_text() == f'roots = ["{dirs["x-work"]}"]\n'
    assert [p.name for p in doc.parent.iterdir()] == ["project.toml"]


def test_concurrent_binds_do_not_lose_updates(tmp_path, dirs):
    bind(tmp_path, A, dirs["w0"], create=True)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            bind(tmp_path, A, dirs[f"w{i}"], create=False)
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert set(load_registry(tmp_path).projects[0].roots) == {dirs[f"w{i}"] for i in range(9)}


def test_project_exists_counts_unbound_directories(tmp_path):
    (tmp_path / "projects" / "old-abc123" / "entries").mkdir(parents=True)
    assert project_exists(tmp_path, ProjectId("old-abc123"))
    assert not project_exists(tmp_path, A)


def test_count_child_git_markers(tmp_path):
    (tmp_path / "a" / ".git").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / ".git").write_text("gitdir: elsewhere")
    (tmp_path / "c" / "deep" / ".git").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    outside = tmp_path.parent / "outside-repo"
    (outside / ".git").mkdir(parents=True)
    (tmp_path / "linked").symlink_to(outside)
    assert count_child_git_markers(tmp_path) == 2
    assert count_child_git_markers(tmp_path / "missing") is None


def test_failed_chmod_closes_the_descriptor_and_leaves_nothing_behind(tmp_path, dirs, monkeypatch):
    from memriver_core import StorageFailure

    bind(tmp_path, A, dirs["x-work"], create=True)
    doc = tmp_path / "projects" / A / "project.toml"
    opened: list[int] = []
    true_mkstemp = project_context.tempfile.mkstemp

    def watched(*args, **kwargs):
        fd, path = true_mkstemp(*args, **kwargs)
        opened.append(fd)
        return fd, path

    def boom(*args, **kwargs):
        raise OSError("no chmod here")

    monkeypatch.setattr(project_context.tempfile, "mkstemp", watched)
    monkeypatch.setattr(project_context.os, "fchmod", boom)
    with pytest.raises(StorageFailure):
        bind(tmp_path, A, dirs["y-work"], create=False)
    assert doc.read_text() == f'roots = ["{dirs["x-work"]}"]\n'
    assert [p.name for p in doc.parent.iterdir()] == ["project.toml"]
    with pytest.raises(OSError):                           # the descriptor did not leak
        os.fstat(opened[0])


def test_unbind_removes_every_copy_of_the_same_root_string(tmp_path, dirs):
    # a hand-edited file can hold one spelling twice; other spellings that alias
    # the same directory are left alone
    _register(tmp_path, A, [dirs["x-work"], dirs["x-work"], dirs["y-work"]])
    unbind(tmp_path, A, dirs["x-work"])
    assert (tmp_path / "projects" / A / "project.toml").read_text() \
        == f'roots = ["{dirs["y-work"]}"]\n'


def test_bind_create_refuses_an_id_that_exists_as_anything(tmp_path, dirs):
    (tmp_path / "projects").mkdir()
    squatter = tmp_path / "projects" / A
    squatter.write_text("not a project directory")         # load_registry ignores stray files
    with pytest.raises(ValueError, match="project id already exists"):
        bind(tmp_path, A, dirs["x-work"], create=True)
    assert squatter.read_text() == "not a project directory"
