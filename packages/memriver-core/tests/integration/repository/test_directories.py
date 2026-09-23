"""Directory rules moved from the umbrella: same-directory, covers, integrity, nearest root."""

from __future__ import annotations

from pathlib import Path

from memriver_core.repository import directories
from memriver_core.repository.directories import (
    Match,
    canonical_directory,
    covers,
    integrity_diagnostic,
    nearest_bound,
    root_state,
)

A = "aaaaaaaaaa"
B = "bbbbbbbbbb"


def _case_insensitive(monkeypatch):
    """Simulate APFS: two spellings differing only in case are one directory."""
    real = directories.same_directory

    def fake(a: str, b: str):
        if a.lower() == b.lower() and a != b:
            return True
        return real(a, b)

    monkeypatch.setattr(directories, "same_directory", fake)


def _unverifiable(monkeypatch):
    """Every samefile check fails (permission, I/O)."""
    monkeypatch.setattr(directories, "same_directory", lambda a, b: None)


def test_canonical_directory_is_strict_and_never_raises(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "d")
    assert canonical_directory(str(tmp_path / "link")) == str((tmp_path / "d").resolve())
    for bad in (tmp_path / "f", tmp_path / "missing", Path("/x\x00y")):
        assert canonical_directory(str(bad)) is None


def test_nearest_registered_ancestor_wins(tmp_path):
    parent = (tmp_path / "work").resolve()
    child = parent / "frontend"
    (child / "src").mkdir(parents=True)
    (parent / "backend").mkdir()
    bound = [(A, str(parent)), (B, str(child))]
    assert nearest_bound(str(child / "src"), bound) == Match("registered", B)
    assert nearest_bound(str(parent / "backend"), bound).project_id == A
    assert nearest_bound(str(parent), bound).project_id == A


def test_unregistered_directory_is_none(tmp_path):
    assert nearest_bound(str(tmp_path), []) == Match("none")


def test_case_alias_matches_by_samefile(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    real = (tmp_path / "Work").resolve()
    real.mkdir()
    assert nearest_bound(str(real), [(A, str(tmp_path.resolve() / "work"))]).project_id == A


def test_nearer_alias_beats_farther_exact_match(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    parent = (tmp_path / "work").resolve()
    child = parent / "Child"
    child.mkdir(parents=True)
    assert nearest_bound(str(child), [(A, str(parent)), (B, str(parent / "child"))]).project_id == B


def test_exact_and_alias_of_different_ids_at_one_level_is_degraded(tmp_path, monkeypatch):
    _case_insensitive(monkeypatch)
    real = (tmp_path / "Work").resolve()
    real.mkdir()
    match = nearest_bound(str(real), [(A, str(real)), (B, str(tmp_path.resolve() / "work"))])
    assert match.state == "degraded"
    assert match.diagnostic.endswith("matched by more than one project")


def test_offline_root_is_matched_by_string_only(tmp_path):
    gone = str((tmp_path / "gone").resolve())
    assert nearest_bound(str(tmp_path), [(A, gone)]).state == "none"
    (tmp_path / "gone").mkdir()
    assert nearest_bound(str(tmp_path / "gone"), [(A, gone)]).project_id == A


def test_root_replaced_by_symlink_is_degraded_and_never_authorizes_target(tmp_path):
    old = (tmp_path / "old").resolve()
    new = (tmp_path / "new").resolve()
    old.mkdir()
    assert nearest_bound(str(old), [(A, str(old))]).project_id == A
    old.rmdir()
    new.mkdir()
    old.symlink_to(new)
    match = nearest_bound(str(new), [(A, str(old))])
    assert match.state == "degraded"
    assert match.diagnostic == f"{old}: registered root is no longer a canonical path"
    assert nearest_bound(str(old), [(A, str(old))]).state == "degraded"


def test_ancestor_component_replaced_by_symlink_is_degraded(tmp_path):
    x = (tmp_path / "x").resolve()
    (x / "work").mkdir(parents=True)
    y = tmp_path / "y"
    x.rename(y)
    x.symlink_to(y)
    assert nearest_bound(str(y / "work"), [(A, str(x / "work"))]).state == "degraded"
    assert integrity_diagnostic([str(x / "work")]) == \
        f"{x / 'work'}: registered root is no longer a canonical path"


def test_redirected_ancestor_with_missing_leaf_is_a_redirect_not_offline(tmp_path):
    x = (tmp_path / "x").resolve()
    x.mkdir()
    y = tmp_path / "y"
    y.mkdir()
    x.rmdir()
    x.symlink_to(y)
    assert integrity_diagnostic([str(x / "work")]) == \
        f"{x / 'work'}: registered root is no longer a canonical path"
    assert root_state(str(x / "work")) == "not-canonical"


def test_fully_absent_root_is_offline(tmp_path):
    root = str((tmp_path / "gone" / "deeper").resolve())
    assert integrity_diagnostic([root]) is None
    assert root_state(root) == "missing"


def test_unverifiable_same_directory_is_degraded_never_a_non_match(tmp_path, monkeypatch):
    real = (tmp_path / "work").resolve()
    real.mkdir()
    _unverifiable(monkeypatch)
    match = nearest_bound(str(tmp_path), [(A, str(real))])
    assert match.state == "degraded"
    assert match.diagnostic == f"{real}: registered root could not be checked"


def test_nul_bearing_root_is_unverifiable_not_raising():
    assert integrity_diagnostic(["/x\x00y"]) == "/x\x00y: registered root could not be checked"
    assert root_state("/x\x00y") == "unverifiable"


def test_root_that_cannot_be_lstatted_is_degraded(tmp_path, monkeypatch):
    real = (tmp_path / "work").resolve()
    real.mkdir()
    true_lstat = directories.os.lstat

    def fake(path, *args, **kwargs):
        if str(path) == str(real):
            raise PermissionError(13, "denied")
        return true_lstat(path, *args, **kwargs)

    monkeypatch.setattr(directories.os, "lstat", fake)
    assert integrity_diagnostic([str(real)]) == f"{real}: registered root could not be checked"
    assert nearest_bound(str(tmp_path), [(A, str(real))]).state == "degraded"


def test_root_state_of_a_healthy_and_of_a_file_root(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_text("x")
    assert root_state(str((tmp_path / "d").resolve())) == "ok"
    assert root_state(str((tmp_path / "f").resolve())) == "missing"


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


def test_start_that_is_a_file_missing_or_unaddressable_is_degraded(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    for start in (f, tmp_path / "does-not-exist", Path("/SENTINEL\x00cwd")):
        match = nearest_bound(str(start), [])
        assert match == Match("degraded", diagnostic="working directory could not be resolved")
