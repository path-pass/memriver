"""Where a directory inside a linked git worktree belongs in its main working tree.

A session started in a linked worktree registers under the project of the
repository's main working tree, keeping the sub-directory it started in. Any
doubt answers None (degraded, never guessed). Git runs only when a `.git`
entry exists somewhere above the directory. Stdlib only.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

from memriver_core.repository.directories import canonical_directory, same_directory

_LOCATE = ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir",
           "--git-common-dir"]


def _git(args: list[str], cwd: str, timeout_s: float) -> str | None:
    """Stdout of `git -C cwd *args`; None on any failure.

    Every inherited `GIT_*` variable is dropped: a `GIT_DIR`/`GIT_WORK_TREE`
    left in the environment would make git answer for another repository.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    try:
        completed = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                                   timeout=timeout_s, env=env, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        # FileNotFoundError: git is not installed; ValueError: output that
        # does not decode in the locale's encoding
        return None
    return completed.stdout if completed.returncode == 0 else None


def _has_git_entry(path: str) -> bool | None:
    """A `.git` entry at `path` or above; None when lstat itself failed."""
    for ancestor in [Path(path), *Path(path).parents]:
        try:
            os.lstat(ancestor / ".git")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return None
        return True
    return False


def _locate(cwd: str, timeout_s: float) -> list[str] | None:
    """[toplevel, git dir, common dir]: exactly three non-empty absolute paths, else None."""
    output = _git(_LOCATE, cwd, timeout_s)
    if output is None:
        return None
    lines = output.splitlines()
    if len(lines) != 3 or not all(line and os.path.isabs(line) for line in lines):
        return None
    return lines


def main_tree_path(path: str, *, timeout_s: float) -> str | None:
    """`path`'s canonical directory outside git or in a main working tree; its twin
    in the main tree when inside a linked worktree; None when that cannot be told
    for sure, or when `path` is not an existing directory.

    Only the canonical spelling is ever used: an alias (a symlink) walks the
    ancestors of where it points, never of where it is written.
    """
    path = canonical_directory(path)
    if path is None:
        return None
    found = _has_git_entry(path)
    if found is None:
        return None
    if not found:
        return path
    located = _locate(path, timeout_s)
    if located is None:
        return None
    toplevel, git_dir, common_dir = located
    if git_dir == common_dir:
        # a main working tree, which includes --separate-git-dir and submodules
        return path
    listing = _git(["worktree", "list", "--porcelain", "-z"], path, timeout_s)
    if listing is None:
        return None
    # NUL-separated fields, an empty field ending each record: a path with a
    # newline cannot split a record
    first = list(itertools.takewhile(bool, listing.split("\0")))
    candidates = [field.removeprefix("worktree ") for field in first
                  if field.startswith("worktree ")]
    if len(candidates) != 1 or "bare" in first:
        return None
    main = candidates[0]
    # the first record of a --separate-git-dir repository is its metadata
    # directory, not a working tree: only the same query run there proves it.
    # Known limitation: a metadata directory itself named <X>/.git is listed
    # as <X>, git answers in <X> as a working tree of the same repository,
    # and a linked worktree then maps into <X>
    confirmed = _locate(main, timeout_s)
    if confirmed is None:
        return None
    if same_directory(confirmed[0], main) is not True \
            or same_directory(confirmed[2], common_dir) is not True:
        return None
    relative = os.path.relpath(path, toplevel)
    if relative.split(os.sep)[0] == os.pardir:
        # git spelled the toplevel differently from `path` (a symlink or a
        # case alias): no sub-directory can be read off the two strings
        return None
    return str(Path(main) / relative)


def current_branch(path: str, *, timeout_s: float) -> str | None:
    """The checked-out branch; "HEAD" when detached; None outside git or on any failure."""
    path = canonical_directory(path)
    if path is None or _has_git_entry(path) is not True:
        return None
    output = _git(["rev-parse", "--abbrev-ref", "HEAD"], path, timeout_s)
    if output is None:
        return None
    lines = output.splitlines()
    return lines[0] if len(lines) == 1 and lines[0] else None
