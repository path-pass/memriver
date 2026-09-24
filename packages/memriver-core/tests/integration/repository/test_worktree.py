"""Linked git worktrees map onto their main working tree (spec §5.1), against real git."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from memriver_core.repository.worktree import current_branch, main_tree_path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TIMEOUT_S = 10.0


def _git(*args: str, cwd: Path) -> None:
    """Run real git for a fixture, isolated from the user's and the system's config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, env=env, check=True, capture_output=True)


def _repository(directory: Path) -> Path:
    directory.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", str(directory), cwd=directory.parent)
    _git("commit", "--allow-empty", "-m", "init", cwd=directory)
    return directory


@pytest.fixture
def base(tmp_path) -> Path:
    return Path(os.path.realpath(tmp_path))


@pytest.fixture
def main(base) -> Path:
    return _repository(base / "main")


@pytest.fixture
def worktree(base, main) -> Path:
    _git("worktree", "add", "-b", "feature", str(base / "wt"), cwd=main)
    return base / "wt"


def _fake_git(monkeypatch, base: Path, script: str) -> Path:
    """Put a fake `git` first on PATH; returns the file it touches when run."""
    bin_dir = base / "fake-bin"
    bin_dir.mkdir()
    marker = base / "git-ran"
    fake = bin_dir / "git"
    fake.write_text(f"#!/bin/sh\ntouch '{marker}'\n{script}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return marker


def test_a_plain_directory_is_unchanged_and_git_is_never_run(base, monkeypatch):
    plain = base / "plain" / "deep"
    plain.mkdir(parents=True)
    marker = _fake_git(monkeypatch, base, "exit 1")
    assert main_tree_path(str(plain), timeout_s=TIMEOUT_S) == str(plain)
    assert current_branch(str(plain), timeout_s=TIMEOUT_S) is None
    assert not marker.exists()


def test_the_main_tree_root_and_its_sub_directories_are_unchanged(main):
    (main / "sub").mkdir()
    assert main_tree_path(str(main), timeout_s=TIMEOUT_S) == str(main)
    assert main_tree_path(str(main / "sub"), timeout_s=TIMEOUT_S) == str(main / "sub")


def test_a_linked_worktree_maps_to_the_main_root_keeping_the_sub_directory(main, worktree):
    (worktree / "newdir").mkdir()
    assert main_tree_path(str(worktree), timeout_s=TIMEOUT_S) == str(main)
    assert main_tree_path(str(worktree / "newdir"), timeout_s=TIMEOUT_S) == str(main / "newdir")
    assert not (main / "newdir").exists()


def test_a_worktree_with_relative_paths_maps_too(base, main):
    _git("worktree", "add", "--relative-paths", "-b", "rel", str(base / "rel"), cwd=main)
    assert main_tree_path(str(base / "rel"), timeout_s=TIMEOUT_S) == str(main)


def test_a_submodule_is_its_own_main_tree(base, main):
    library = _repository(base / "library")
    _git("-c", "protocol.file.allow=always", "submodule", "add", str(library), "sub", cwd=main)
    assert main_tree_path(str(main / "sub"), timeout_s=TIMEOUT_S) == str(main / "sub")


def test_a_separate_git_dir_repository_is_unchanged(base):
    _git("init", "--separate-git-dir", str(base / "meta"), str(base / "work"), cwd=base)
    assert main_tree_path(str(base / "work"), timeout_s=TIMEOUT_S) == str(base / "work")


def test_a_worktree_whose_admin_commondir_is_gone_is_degraded(main, worktree):
    (main / ".git" / "worktrees" / "wt" / "commondir").unlink()
    assert main_tree_path(str(worktree), timeout_s=TIMEOUT_S) is None


def test_a_git_file_pointing_nowhere_is_degraded(base):
    broken = base / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: ../gone\n")
    assert main_tree_path(str(broken), timeout_s=TIMEOUT_S) is None


def test_a_worktree_of_a_bare_repository_named_dot_git_is_degraded(base, main):
    bare = base / "x" / ".git"
    _git("init", "--bare", str(bare), cwd=base)
    _git("push", str(bare), "main", cwd=main)
    _git("worktree", "add", str(base / "bare-wt"), "main", cwd=bare)
    assert main_tree_path(str(base / "bare-wt"), timeout_s=TIMEOUT_S) is None


def test_a_worktree_of_a_separate_git_dir_repository_is_degraded(base):
    work = base / "work"
    _git("init", "-b", "main", "--separate-git-dir", str(base / "meta"), str(work), cwd=base)
    _git("commit", "--allow-empty", "-m", "init", cwd=work)
    _git("worktree", "add", "-b", "feature", str(base / "wt2"), cwd=work)
    assert main_tree_path(str(base / "wt2"), timeout_s=TIMEOUT_S) is None


def test_a_separate_git_dir_named_dot_git_maps_into_its_parent_known_limitation(base):
    # git lists <X> (the metadata directory minus "/.git") first, and asked in
    # <X> it answers as a working tree of the same repository: the check in
    # the main tree passes. Pinned so that changing this is a deliberate act.
    work = base / "work"
    (base / "x").mkdir()
    _git("init", "-b", "main", "--separate-git-dir", str(base / "x" / ".git"), str(work),
         cwd=base)
    _git("commit", "--allow-empty", "-m", "init", cwd=work)
    _git("worktree", "add", "-b", "feature", str(base / "wt2"), cwd=work)
    assert main_tree_path(str(base / "wt2"), timeout_s=TIMEOUT_S) == str(base / "x")


def test_a_main_tree_whose_git_names_another_toplevel_is_degraded(base, main, worktree):
    (base / "other").mkdir()
    _git("config", "core.worktree", str(base / "other"), cwd=main)
    assert main_tree_path(str(worktree), timeout_s=TIMEOUT_S) is None


def test_a_worktree_spelled_through_a_case_alias_is_degraded_never_mis_mapped(base, worktree):
    (worktree / "sub").mkdir()
    alias = base / "WT" / "sub"
    if not alias.exists():
        pytest.skip("case-sensitive filesystem")
    assert main_tree_path(str(alias), timeout_s=TIMEOUT_S) is None


def test_git_that_hangs_past_the_timeout_is_degraded(base, main, monkeypatch):
    _fake_git(monkeypatch, base, "exec sleep 30")
    started = time.monotonic()
    assert main_tree_path(str(main), timeout_s=0.5) is None
    assert current_branch(str(main), timeout_s=0.5) is None
    assert time.monotonic() - started < 10


def test_git_not_installed_is_degraded(base, main, monkeypatch):
    empty = base / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert main_tree_path(str(main), timeout_s=TIMEOUT_S) is None
    assert current_branch(str(main), timeout_s=TIMEOUT_S) is None


def test_inherited_git_variables_are_ignored(main, worktree, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(main / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(main))
    assert main_tree_path(str(worktree), timeout_s=TIMEOUT_S) == str(main)
    assert current_branch(str(worktree), timeout_s=TIMEOUT_S) == "feature"


def test_current_branch_of_main_worktree_and_detached_head(main, worktree):
    assert current_branch(str(main), timeout_s=TIMEOUT_S) == "main"
    assert current_branch(str(worktree), timeout_s=TIMEOUT_S) == "feature"
    _git("checkout", "--detach", cwd=main)
    assert current_branch(str(main), timeout_s=TIMEOUT_S) == "HEAD"
