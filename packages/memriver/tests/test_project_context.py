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
    resolve,
    resolve_project,
    root_integrity,
    unbind,
    visible,
)
from memriver_core import StorageFailure
from memriver_core.bootstrap import build_service
from memriver_core.config import Settings
from memriver_core.models import new_id

A = "aaaaaaaaaa"
B = "bbbbbbbbbb"


def _register(store: Path, project_id: str, roots: list[str]) -> Path:
    d = store / "registry"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{project_id}.toml"
    path.write_text("roots = [" + ", ".join(f'"{r}"' for r in roots) + "]\n", encoding="utf-8")
    return path


def _registry(*pairs: tuple[str, list[str]]) -> Registry:
    return Registry(tuple(RegisteredProject(i, tuple(r)) for i, r in pairs))


def _service_with(store: Path, *names: str):
    service = build_service(Settings(root=store), root=store)
    return service, [service.create_project(name).id for name in names]


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

def test_missing_registry_dir_is_an_empty_registry(tmp_path):
    assert load_registry(tmp_path / "store") == Registry(())
    assert not (tmp_path / "store").exists()


def test_an_empty_roots_file_is_a_project_with_no_roots(tmp_path):
    _register(tmp_path, A, [])
    assert load_registry(tmp_path).projects == (RegisteredProject(A, ()),)


def test_bound_project_keeps_roots_as_stored(tmp_path):
    _register(tmp_path, A, ["/x/work", "/y/work"])
    (project,) = load_registry(tmp_path).projects
    assert project.roots == ("/x/work", "/y/work")


def test_the_pre_release_registry_is_not_read(tmp_path):
    old = tmp_path / "projects" / "demo-0123456789abcdef"
    old.mkdir(parents=True)
    (old / "project.toml").write_text('roots = ["/x/work"]\n')
    assert load_registry(tmp_path) == Registry(())


@pytest.mark.parametrize("body, reason", [
    ("roots = [\n", "registry file is not valid TOML"),
    ('roots = ["/x"]\nname = "x"\n', "registry file has a key other than roots"),
    ('name = "x"\n', "registry file has no roots key"),
    ("", "registry file has no roots key"),
    ('roots = "/x"\n', "roots is not an array of strings"),
    ("roots = [1]\n", "roots is not an array of strings"),
    ('roots = ["relative/path"]\n', "root is not an absolute path"),
    ('roots = ["/x/\\u0000y"]\n', "root is not an addressable path"),
])
def test_invalid_registry_file_names_location_and_fixed_reason(tmp_path, body, reason):
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / f"{A}.toml").write_text(body, encoding="utf-8")
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert (info.value.location, info.value.reason) == (f"registry/{A}.toml", reason)


def test_invalid_file_error_never_echoes_the_offending_value(tmp_path):
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / f"{A}.toml").write_text('roots = ["SENTINEL-relative/path"]\n')
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert "SENTINEL" not in str(info.value)


def test_a_symlinked_registry_file_is_never_followed_live_or_dangling(tmp_path):
    outside = tmp_path / "outside.toml"
    outside.write_text('roots = ["/x/work"]\n')
    (tmp_path / "registry").mkdir()
    for target in (outside, tmp_path / "nowhere.toml"):
        link = tmp_path / "registry" / f"{A}.toml"
        link.symlink_to(target)
        with pytest.raises(RegistryInvalid) as info:
            load_registry(tmp_path)
        assert (info.value.location, info.value.reason) == (
            f"registry/{A}.toml", "registry entry could not be read")
        link.unlink()


def _call_with_timeout(fn, *args, seconds: float = 5.0, unblock: Path | None = None):
    """Run ``fn(*args)`` on a thread; a call that does not return in ``seconds`` fails.

    A regression that opens a FIFO would block the caller forever and hang
    pytest instead of failing one test. On timeout the write end of
    ``unblock`` is opened and closed so the stuck reader sees EOF.
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


@pytest.mark.parametrize("kind", ["fifo", "directory", "socket"])
def test_a_correctly_named_non_regular_registry_file_is_refused_never_skipped(
        tmp_path, monkeypatch, kind):
    # skipping it would let resolution fall back to a parent project's root
    registry = tmp_path / "registry"
    registry.mkdir()
    path = registry / f"{A}.toml"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        import socket
        monkeypatch.chdir(registry)             # AF_UNIX paths are short; bind relative
        server = socket.socket(socket.AF_UNIX)
        server.bind(f"{A}.toml")
        server.close()
    unblock = path if kind == "fifo" else None
    with pytest.raises(RegistryInvalid) as info:
        _call_with_timeout(load_registry, tmp_path, unblock=unblock)
    assert (info.value.location, info.value.reason) == (
        f"registry/{A}.toml", "registry file could not be read")
    res = _call_with_timeout(resolve, tmp_path, tmp_path, unblock=unblock)
    assert res.state == "degraded"
    assert res.diagnostic == f"registry/{A}.toml: registry file could not be read"


def test_registry_dir_that_is_a_symlink_or_a_file_is_invalid_not_empty(tmp_path):
    (tmp_path / "registry").symlink_to(tmp_path / "nowhere")
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert (info.value.location, info.value.reason) == ("registry", "registry entry could not be read")
    (tmp_path / "registry").unlink()
    (tmp_path / "registry").write_text("not a directory")
    with pytest.raises(RegistryInvalid):
        load_registry(tmp_path)


@pytest.mark.parametrize("name", ["Bad_Name.toml", "demo-0123456789abcdef.toml",
                                  "AAAAAAAAAA.toml"])
def test_a_registry_file_name_that_is_not_an_id_is_invalid(tmp_path, name):
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / name).write_text("roots = []\n")
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert (info.value.location, info.value.reason) == (
        f"registry/{name}", "registry file name is not an addressable project id")


def test_unverifiable_comparison_makes_the_registry_invalid(tmp_path, monkeypatch):
    _register(tmp_path, A, ["/x/work"])
    _register(tmp_path, B, ["/y/work"])
    _unverifiable(monkeypatch)
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert info.value.reason == "root could not be checked"


def test_same_root_under_two_ids_is_invalid(tmp_path):
    _register(tmp_path, A, ["/x/work"])
    _register(tmp_path, B, ["/x/work"])
    with pytest.raises(RegistryInvalid) as info:
        load_registry(tmp_path)
    assert (info.value.location, info.value.reason) == (
        f"registry/{B}.toml", "root is already bound to another project")


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


def test_strays_in_the_registry_dir_are_ignored(tmp_path):
    (tmp_path / "registry" / "subdir").mkdir(parents=True)
    (tmp_path / "registry" / "notes.txt").write_text("x")
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


# --- resolve (never raises) ---

def test_resolve_survives_a_start_path_the_os_cannot_even_address(tmp_path):
    # the hooks take cwd from the harness payload, and JSON can carry a NUL:
    # resolving it raises ValueError, which must still come back as degraded
    res = resolve(tmp_path / "store", Path("/SENTINEL\x00cwd"))
    assert res.state == "degraded"
    assert res.diagnostic == "working directory could not be resolved"
    assert "SENTINEL" not in res.diagnostic


def test_resolve_turns_invalid_registry_into_degraded(tmp_path):
    (tmp_path / "store" / "registry").mkdir(parents=True)
    (tmp_path / "store" / "registry" / f"{A}.toml").write_text("roots = [\n")
    res = resolve(tmp_path / "store", tmp_path)
    assert res.state == "degraded"
    assert res.diagnostic == f"registry/{A}.toml: registry file is not valid TOML"


def test_visible_replaces_controls_one_for_one_and_keeps_ordinary_spaces():
    # the management surfaces print registry-derived strings verbatim: every
    # control character becomes one space, ordinary spaces are left alone
    hostile = "two  spaces\x00\x1b[31m\n\x85\u2028\u2029end"
    rendered = visible(hostile)
    assert rendered == "two  spaces  [31m    end"   # NUL, ESC, LF, NEL, LS, PS: one space each
    assert len(rendered) == len(hostile)
    assert visible("  lead and  inner  and trail  ") == "  lead and  inner  and trail  "


# --- registry writes ---

@pytest.fixture
def dirs(tmp_path):
    """Real, canonical directories to bind: bind refuses paths that do not exist."""
    out = {}
    for name in ("x-work", "y-work", "z-old", "w0", "w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8"):
        (tmp_path / "roots" / name).mkdir(parents=True)
        out[name] = str((tmp_path / "roots" / name).resolve())
    return out


def test_bind_writes_a_private_registry_file_with_the_exact_document(tmp_path, dirs):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    bind(store, service, pid, dirs["x-work"])
    doc = store / "registry" / f"{pid}.toml"
    assert stat.S_IMODE(doc.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(doc.stat().st_mode) == 0o600
    assert doc.read_text() == f'roots = ["{dirs["x-work"]}"]\n'
    bind(store, service, pid, dirs["y-work"])
    assert doc.read_text() == f'roots = ["{dirs["x-work"]}", "{dirs["y-work"]}"]\n'
    assert not (store / "memories").exists()


def test_bind_refuses_a_missing_project_and_the_global_project(tmp_path, dirs):
    store = tmp_path / "store"
    service, _ = _service_with(store)
    global_id = service.ensure_global()
    with pytest.raises(ValueError, match="no such project"):
        bind(store, service, new_id(), dirs["x-work"])
    with pytest.raises(ValueError, match="the global project cannot be bound"):
        bind(store, service, global_id, dirs["x-work"])
    assert not (store / "registry").exists()


def test_bind_refuses_when_the_manifest_is_invalid(tmp_path, dirs):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    (store / "store.toml").write_text("global_project = 'nope'\n")
    with pytest.raises(StorageFailure):
        bind(store, service, pid, dirs["x-work"])
    assert not (store / "registry").exists()


def test_bind_refuses_a_root_that_vanished_or_was_redirected(tmp_path, dirs):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    with pytest.raises(ValueError, match="not a canonical existing directory"):
        bind(store, service, pid, str(tmp_path / "never"))
    link = tmp_path / "link"
    link.symlink_to(dirs["x-work"])
    with pytest.raises(ValueError, match="not a canonical existing directory"):
        bind(store, service, pid, str(link))
    assert not (store / "registry").exists()


def test_bind_refuses_when_a_comparison_cannot_be_checked(tmp_path, dirs, monkeypatch):
    store = tmp_path / "store"
    service, (a, b) = _service_with(store, "a", "b")
    bind(store, service, a, dirs["x-work"])
    _unverifiable(monkeypatch)
    with pytest.raises(ValueError, match="could not be checked"):
        bind(store, service, b, dirs["y-work"])
    assert not (store / "registry" / f"{b}.toml").exists()


def test_bind_same_id_alias_is_a_no_op_and_cross_id_is_refused(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    real = tmp_path / "Work"
    real.mkdir()
    (tmp_path / "work").mkdir(exist_ok=True)
    store = tmp_path / "store"
    service, (a, b) = _service_with(store, "a", "b")
    bind(store, service, a, str(real))
    doc = store / "registry" / f"{a}.toml"
    before = doc.stat().st_mtime_ns
    bind(store, service, a, str(real))
    bind(store, service, a, str(tmp_path / "work"))
    assert doc.read_text() == f'roots = ["{real}"]\n' and doc.stat().st_mtime_ns == before
    with pytest.raises(ValueError, match="already bound to another project"):
        bind(store, service, b, str(tmp_path / "work"))
    assert not (store / "registry" / f"{b}.toml").exists()


def test_unbind_removes_one_root_even_if_path_is_gone(tmp_path, dirs):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    bind(store, service, pid, dirs["x-work"])
    bind(store, service, pid, dirs["y-work"])
    import shutil
    shutil.rmtree(dirs["x-work"])
    unbind(store, pid, dirs["x-work"])
    doc = store / "registry" / f"{pid}.toml"
    assert doc.read_text() == f'roots = ["{dirs["y-work"]}"]\n'
    unbind(store, pid, dirs["y-work"])
    assert doc.read_text() == "roots = []\n"
    with pytest.raises(ValueError, match="not bound to this project"):
        unbind(store, pid, dirs["y-work"])


def test_unbind_cleans_up_a_registry_file_whose_project_is_missing(tmp_path, dirs):
    orphan = new_id()
    doc = _register(tmp_path, orphan, [dirs["x-work"]])
    unbind(tmp_path, orphan, dirs["x-work"])
    assert doc.read_text() == "roots = []\n"


def test_unbind_removes_every_copy_of_the_same_root_string(tmp_path, dirs):
    doc = _register(tmp_path, A, [dirs["x-work"], dirs["x-work"], dirs["y-work"]])
    unbind(tmp_path, A, dirs["x-work"])
    assert doc.read_text() == f'roots = ["{dirs["y-work"]}"]\n'


def test_bind_refuses_to_write_over_an_invalid_registry(tmp_path, dirs):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    (store / "registry").mkdir()
    (store / "registry" / f"{B}.toml").write_text("roots = [\n")
    with pytest.raises(RegistryInvalid):
        bind(store, service, pid, dirs["x-work"])
    assert not (store / "registry" / f"{pid}.toml").exists()


def test_bind_refuses_a_symlinked_registry_directory(tmp_path, dirs):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "registry").symlink_to(outside)
    with pytest.raises(RegistryInvalid):
        bind(store, service, pid, dirs["x-work"])
    assert list(outside.iterdir()) == []


def _swap_registry_for_a_link_after_the_read(store: Path, outside: Path, monkeypatch) -> None:
    """The registry dir becomes a link between the registry read and the write."""
    true_load = project_context.load_registry

    def load_then_swap(root: Path) -> Registry:
        registry = true_load(root)
        (store / "registry").rename(store / "registry-moved")
        (store / "registry").symlink_to(outside)
        return registry

    monkeypatch.setattr(project_context, "load_registry", load_then_swap)


def test_bind_never_writes_through_a_registry_dir_swapped_for_a_symlink(
        tmp_path, dirs, monkeypatch):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    bind(store, service, pid, dirs["x-work"])
    outside = tmp_path / "outside"
    outside.mkdir()
    _swap_registry_for_a_link_after_the_read(store, outside, monkeypatch)
    with pytest.raises(StorageFailure):              # store_lock wraps the core writer's OSError
        bind(store, service, pid, dirs["y-work"])
    assert list(outside.iterdir()) == []


def test_unbind_never_writes_through_a_registry_dir_swapped_for_a_symlink(
        tmp_path, dirs, monkeypatch):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    bind(store, service, pid, dirs["x-work"])
    outside = tmp_path / "outside"
    outside.mkdir()
    _swap_registry_for_a_link_after_the_read(store, outside, monkeypatch)
    with pytest.raises(StorageFailure):
        unbind(store, pid, dirs["x-work"])
    assert list(outside.iterdir()) == []


def test_failed_replace_keeps_old_document_and_no_temp_file(tmp_path, dirs, monkeypatch):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    bind(store, service, pid, dirs["x-work"])
    doc = store / "registry" / f"{pid}.toml"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)       # the core writer's replace
    with pytest.raises(StorageFailure):              # store_lock wraps every OSError
        bind(store, service, pid, dirs["y-work"])
    assert doc.read_text() == f'roots = ["{dirs["x-work"]}"]\n'
    assert [p.name for p in doc.parent.iterdir()] == [doc.name]


def test_failed_temp_write_closes_the_descriptor_and_leaves_nothing_behind(
        tmp_path, dirs, monkeypatch):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    bind(store, service, pid, dirs["x-work"])
    doc = store / "registry" / f"{pid}.toml"
    opened: list[int] = []
    true_fdopen = os.fdopen

    class FailingWrite:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, text):
            raise OSError("disk full")

    def failing_fdopen(fd, mode="r", *args, **kwargs):
        handle = true_fdopen(fd, mode, *args, **kwargs)
        if "w" not in mode:                     # the service's reads go through here too
            return handle
        opened.append(fd)
        return FailingWrite(handle)

    monkeypatch.setattr(os, "fdopen", failing_fdopen)   # the core writer's temp file
    with pytest.raises(StorageFailure):
        bind(store, service, pid, dirs["y-work"])
    assert doc.read_text() == f'roots = ["{dirs["x-work"]}"]\n'
    assert [p.name for p in doc.parent.iterdir()] == [doc.name]
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_concurrent_binds_do_not_lose_updates(tmp_path, dirs):
    store = tmp_path / "store"
    service, (pid,) = _service_with(store, "x")
    bind(store, service, pid, dirs["w0"])
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            bind(store, service, pid, dirs[f"w{i}"])
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert set(load_registry(store).projects[0].roots) == {dirs[f"w{i}"] for i in range(9)}


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
