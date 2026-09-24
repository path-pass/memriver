"""The umbrella's display helpers and the installers' git-root finder.

Every directory rule and every registry behaviour moved to memriver_core
(repository.directories and the SQLite project store) with its tests.
"""

from __future__ import annotations

import errno
import os

import pytest
from memriver.project_context import count_child_git_markers, find_git_root, visible


def test_find_git_root_is_kept_for_installers(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert find_git_root(tmp_path / "repo" / "src") == (tmp_path / "repo").resolve()
    assert find_git_root(tmp_path) is None


def test_find_git_root_raises_when_a_marker_cannot_be_checked(tmp_path, monkeypatch):
    """A `.git` the OS cannot stat is not an absent one: climbing past it would
    place project files in the enclosing repository instead."""
    (tmp_path / "outer" / ".git").mkdir(parents=True)
    (tmp_path / "outer" / "inner" / ".git").mkdir(parents=True)
    marker = str((tmp_path / "outer" / "inner" / ".git").resolve())
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if str(path) == marker:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), marker)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    with pytest.raises(PermissionError):
        find_git_root(tmp_path / "outer" / "inner")


def test_find_git_root_climbs_past_a_looping_git_marker(tmp_path):
    """A `.git` symlink that loops names nothing, like a missing one: the climb
    goes on, as `Path.exists()` did on 3.12 and as git's own discovery does."""
    (tmp_path / "outer" / ".git").mkdir(parents=True)
    (tmp_path / "outer" / "inner").mkdir()
    (tmp_path / "outer" / "inner" / ".git").symlink_to(".git")
    assert find_git_root(tmp_path / "outer" / "inner") == (tmp_path / "outer").resolve()


def test_visible_replaces_controls_one_for_one_and_keeps_ordinary_spaces():
    # the management surfaces print registry-derived strings verbatim: every
    # control character becomes one space, ordinary spaces are left alone
    hostile = "two  spaces\x00\x1b[31m\n\x85\u2028\u2029end"
    rendered = visible(hostile)
    assert rendered == "two  spaces  [31m    end"   # NUL, ESC, LF, NEL, LS, PS: one space each
    assert len(rendered) == len(hostile)
    assert visible("  lead and  inner  and trail  ") == "  lead and  inner  and trail  "


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
