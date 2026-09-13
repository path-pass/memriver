"""``memriver uninstall``: undo install's config edits, then optionally the rest.

``memriver.install.run_config_uninstall`` (the config-removal pipeline) never imports
``memriver_core`` -- it edits harness config files, it does not know where the
memory store lives. Purging that store, and clearing memriver's own packages
from the ``uv`` cache, both need capabilities the install package deliberately
does not have, so this module -- a sibling of ``doctor.py``, not a part of the
install package -- composes them: run the config removal first, and touch
nothing else unless that exits clean.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from .install import run_config_uninstall

# the two packages memriver's own install ever populates in uv's cache; `uv
# cache clean` takes one package name at a time
_UV_CACHE_PACKAGES = ("memriver", "memriver-core")

# a wedged `uv` must not hang uninstall after config removal already committed
_UV_CACHE_CLEAN_TIMEOUT_SECONDS = 30


def run_uninstall(harnesses: Sequence[str], *, yes: bool, dry_run: bool,
                  purge_data: bool, clean_uv_cache: bool,
                  home: Path, cwd: Path, env: Mapping[str, str],
                  input_fn: Callable[[str], str], stdout: TextIO,
                  replace_file: Callable[[Path, Path], None],
                  root: Path | None = None) -> int:
    """Config removal, then -- only once that succeeds -- the data root and cache.

    ``root``, when given, is the ``--purge-data`` target outright -- it wins
    over both the injected ``env``'s ``MEMRIVER_ROOT`` and the injected
    ``home``'s default, the same precedence ``doctor --root`` already gives
    its own root override.
    """
    exit_code = run_config_uninstall(
        harnesses, yes=yes, dry_run=dry_run, home=home, cwd=cwd, env=env,
        input_fn=input_fn, stdout=stdout, replace_file=replace_file,
    )
    if exit_code != 0:
        return exit_code

    if purge_data:
        exit_code = _purge_data(yes=yes, dry_run=dry_run, input_fn=input_fn,
                                stdout=stdout, env=env, home=home, cwd=cwd,
                                root_override=root)
        if exit_code != 0:
            return exit_code

    if clean_uv_cache and not dry_run:
        _clean_uv_cache(stdout)

    return 0


def _resolve_storage_root(root_override: Path | None, env: Mapping[str, str],
                          home: Path) -> Path:
    # imported here, not at module scope, so a plain uninstall with neither
    # flag never pays for importing memriver_core -- same convention doctor.py
    # already uses for its own memriver_core imports
    from memriver_core.config import storage_root

    if root_override is not None:
        return root_override
    return storage_root(env=env, home=home)


def _unresolvable(path: Path, error: Exception) -> str:
    """The refusal for a path canonicalization cannot answer for at all.

    A symlink loop, a component this user may not traverse: every one of them
    reaches ``_purge_data`` *after* the harness configuration has already been
    removed, so none of them may leave the process by way of a traceback.
    ``Path.resolve`` raises ``OSError`` for most of these and a bare
    ``RuntimeError`` for a loop, which is why both are caught wherever a purge
    path is resolved.
    """
    return (
        f"memriver uninstall: cannot resolve {path} ({error}); nothing was "
        "removed. Check what the path points at and run uninstall --purge-data "
        "again.\n"
    )


def _refuse_purge_target(given: Path, canonical: Path, *, home: Path,
                         cwd: Path) -> str | None:
    """The refusal text for a target too dangerous to delete, or ``None``.

    Both checks run against ``canonical`` -- the destination the deletion would
    actually walk -- because a symlinked component anywhere above the leaf
    redirects the deletion somewhere the given spelling never named, and a
    relative path or a ``..`` chain names it only after resolution.

    A leaf that is itself a symlink is still refused outright rather than
    followed: ``--root`` names the store, and a link standing in for it is an
    arrangement memriver will not delete through.
    """
    if given.is_symlink():
        return (
            f"memriver uninstall: {given} is a symlink; memriver will not purge "
            "through it. Replace it with a regular directory (or remove it) and "
            "run uninstall --purge-data again.\n"
        )
    # `home`/`cwd` being relative to the target covers the target *being* one of
    # them and the target being any ancestor of one -- the filesystem root
    # included, since every path is relative to it
    for base in (home, cwd):
        try:
            resolved = base.resolve()
        except (OSError, RuntimeError) as error:
            return _unresolvable(base, error)
        if resolved.is_relative_to(canonical):
            return (
                f"memriver uninstall: {canonical} is too broad a target to delete "
                "-- it holds the home directory, the current directory, or the "
                "whole filesystem. Point --root (or MEMRIVER_ROOT) at the memriver "
                "store itself and run uninstall --purge-data again.\n"
            )
    return None


def _open_directory_without_following_symlinks(path: Path) -> int:
    """Open ``path`` as a directory fd, refusing a symlink at *any* level.

    ``path`` must be absolute and already resolved. ``O_NOFOLLOW`` on a
    single full-path ``os.open`` refuses only a symlinked *leaf* -- an ancestor
    component swapped for a symlink between a guard and the open is still
    followed, redirecting the open onto a different real directory. So the path
    is walked component by component from the filesystem root, every component
    opened with ``O_NOFOLLOW``: a symlink anywhere along the way fails closed
    (``OSError``) rather than redirecting. The caller owns the returned fd.
    """
    fd = os.open(path.anchor or "/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component,
                              os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _purge_data(*, yes: bool, dry_run: bool, input_fn: Callable[[str], str],
                stdout: TextIO, env: Mapping[str, str], home: Path, cwd: Path,
                root_override: Path | None) -> int:
    """Delete the memory storage root, resolved from the same env/home this run
    was invoked with -- never the real process environment/home, which would
    silently purge a different store than the one ``uninstall`` just edited
    the harness configs for. ``--root`` (``root_override``), when given,
    outranks both.

    The target is canonicalized before anything else looks at it, and it is the
    canonical path -- not the spelling that produced it -- that is shown and
    that every guard runs against. What is finally deleted, though, is not a
    path at all: ``_remove_confirmed_directory`` binds the deletion to the
    directory *object* the user was shown, so the confirmation prompt is no
    longer a window in which the target can be swapped for another one.
    """
    given = _resolve_storage_root(root_override, env, home)
    if not given.is_absolute():
        given = cwd / given
    try:
        canonical = given.resolve()
    except (OSError, RuntimeError) as error:
        stdout.write("\n" + _unresolvable(given, error))
        return 1
    stdout.write(f"\nmemory storage root: {canonical}\n")
    refusal = _refuse_purge_target(given, canonical, home=home, cwd=cwd)
    if refusal is not None:
        stdout.write(refusal)
        return 1
    if not canonical.exists():
        stdout.write("no data to remove.\n")
        return 0
    if not canonical.is_dir():
        stdout.write(
            f"memriver uninstall: {canonical} is not a directory; nothing was "
            "removed.\n"
        )
        return 1
    if dry_run:
        stdout.write("dry run: the memory store was not removed.\n")
        return 0
    try:
        confirmed_fd = _open_directory_without_following_symlinks(canonical)
    except OSError as error:
        # the no-follow walk is what makes any component -- leaf or ancestor --
        # turned into a symlink between the guards above and here fail closed
        # rather than redirect the open onto a different real directory
        stdout.write(
            f"memriver uninstall: {canonical} could not be opened as a directory "
            f"({error.strerror or error}); nothing was removed.\n"
        )
        return 1
    try:
        confirmed = os.fstat(confirmed_fd)
        if not yes:
            try:
                answer = input_fn(
                    f"remove the entire memory store at {canonical}? [y/N] ")
            except EOFError:
                stdout.write(
                    "memriver uninstall: stdin is not interactive and no answer can "
                    "be read; re-run with --yes to purge the memory store shown "
                    "above.\n"
                )
                return 1
            if answer.strip().lower() not in ("y", "yes"):
                stdout.write(
                    "data purge declined; the memory store was left in place.\n")
                return 0
        return _remove_confirmed_directory(canonical, confirmed, stdout,
                                           given=given, home=home, cwd=cwd)
    finally:
        os.close(confirmed_fd)


def _remove_confirmed_directory(canonical: Path, confirmed: os.stat_result,
                                stdout: TextIO, *, given: Path, home: Path,
                                cwd: Path) -> int:
    """Delete the one directory ``confirmed`` identifies, or nothing at all.

    Comparing path strings a second time only proves that two moments resolved
    to the same spelling; it does not prove the directory standing there is
    still the object the user was shown. Neither does checking the object and
    then handing the *name* to a remover: whatever re-opens that name can be
    handed a different directory than the one that was checked.

    So the deletion is anchored to descriptors instead. The canonical parent is
    opened once; the leaf is opened through that fd and its ``(st_dev,
    st_ino)`` compared against the confirmed directory's; and the walk that
    empties it runs on *that* descriptor, never on a name. A leaf renamed away
    mid-walk takes its descriptor with it -- deleting its contents is still the
    action the user confirmed -- and a replacement standing at the old name is
    neither entered nor removed.

    Only the emptied root itself has to be detached by name, because there is
    no way to unlink a directory by descriptor; that one step re-checks the
    identity immediately beforehand and gives up if the name has changed hands.
    """
    try:
        parent_fd = _open_directory_without_following_symlinks(canonical.parent)
    except OSError as error:
        stdout.write(
            f"memriver uninstall: {canonical.parent} could not be opened as a "
            f"directory ({error.strerror or error}); nothing was removed.\n"
        )
        return 1
    try:
        try:
            leaf_fd = os.open(canonical.name,
                              os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                              dir_fd=parent_fd)
        except OSError as error:
            stdout.write(
                f"memriver uninstall: {canonical} could not be checked "
                f"({error.strerror or error}); nothing was removed.\n"
            )
            return 1
        try:
            if not _is_confirmed(os.fstat(leaf_fd), confirmed):
                stdout.write(
                    f"memriver uninstall: {canonical} is no longer the directory "
                    "shown above; nothing was removed. Check what the path points "
                    "at and run uninstall --purge-data again.\n"
                )
                return 1
            # the object is the confirmed one; the last question is whether the
            # protected bases moved into it while the prompt was open
            refusal = _refuse_purge_target(given, canonical, home=home, cwd=cwd)
            if refusal is not None:
                stdout.write(refusal)
                return 1
            replaced: list[Path] = []
            try:
                _empty_directory(leaf_fd, directory=canonical, replaced=replaced)
            except OSError as error:
                stdout.write(_partly_removed(canonical, error.strerror or str(error),
                                             replaced))
                return 1
            try:
                still_there = os.stat(canonical.name, dir_fd=parent_fd,
                                      follow_symlinks=False)
                if not _is_confirmed(still_there, confirmed):
                    stdout.write(
                        f"memriver uninstall: the memory store at {canonical} was "
                        "emptied, but a different object now stands at that name "
                        "and was left alone. Remove the empty directory by hand.\n"
                    )
                    return 1
                os.rmdir(canonical.name, dir_fd=parent_fd)
            except OSError as error:
                stdout.write(_partly_removed(canonical, error.strerror or str(error),
                                             replaced))
                return 1
        finally:
            os.close(leaf_fd)
    finally:
        os.close(parent_fd)
    stdout.write(f"removed {canonical}\n")
    return 0


def _is_confirmed(candidate: os.stat_result, confirmed: os.stat_result) -> bool:
    return (candidate.st_dev, candidate.st_ino) == (confirmed.st_dev,
                                                    confirmed.st_ino)


def _partly_removed(canonical: Path, reason: str,
                    replaced: Sequence[Path] = ()) -> str:
    return "".join(
        f"memriver uninstall: {path} was replaced while it was being removed "
        "and was left alone.\n" for path in replaced
    ) + (
        f"memriver uninstall: {canonical} was only partly removed ({reason}); "
        "the harness configuration above was removed successfully. Delete the "
        "remaining directory by hand.\n"
    )


def _empty_directory(fd: int, *, directory: Path, replaced: list[Path]) -> None:
    """Delete everything inside the open directory ``fd``, not the directory itself.

    Every step is relative to a descriptor this walk opened itself, so nothing
    a concurrent rename does to the path above can redirect a single unlink.
    Child directories are opened with ``O_NOFOLLOW``, which keeps a symlink a
    symlink: it is unlinked where it stands, never followed into.

    Detaching an emptied child is the one step no descriptor can carry out --
    there is no ``rmdir`` by fd -- so it names the child, and a name can change
    hands while the walk beneath it runs. The child's identity is captured when
    it is opened and re-checked immediately before the ``rmdir``: a stranger
    standing at that name is left alone and appended to ``replaced``, which the
    partial-removal report names. ``directory`` is carried only to spell those
    paths out for the user.
    """
    with os.scandir(fd) as entries:
        children = list(entries)
    for child in children:
        if not child.is_dir(follow_symlinks=False):
            os.unlink(child.name, dir_fd=fd)
            continue
        child_fd = os.open(child.name,
                           os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
        try:
            opened = os.fstat(child_fd)
            _empty_directory(child_fd, directory=directory / child.name,
                             replaced=replaced)
        finally:
            os.close(child_fd)
        still_there = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
        if not _is_confirmed(still_there, opened):
            replaced.append(directory / child.name)
            continue
        os.rmdir(child.name, dir_fd=fd)


def _clean_uv_cache(stdout: TextIO) -> None:
    """Best-effort ``uv cache clean`` for both memriver packages.

    Deleting the cache of the very interpreter this process runs under is
    safe on POSIX: unlink only removes the directory entry, and any file this
    process still has open (its own installed wheel included) stays valid
    until the last file descriptor referencing it closes. Neither a missing
    ``uv`` nor a nonzero exit is fatal here -- uninstall's own exit code
    already reflects whether the config removal it guards succeeded.
    """
    for package in _UV_CACHE_PACKAGES:
        try:
            result = subprocess.run(
                ["uv", "cache", "clean", package],
                capture_output=True, text=True, check=False,
                timeout=_UV_CACHE_CLEAN_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            stdout.write(
                "\nwarning: uv was not found on PATH; clean the cache manually:\n"
                f"  uv cache clean {package}\n"
            )
            continue
        except subprocess.TimeoutExpired:
            stdout.write(
                f"\nwarning: 'uv cache clean {package}' timed out; run it manually:\n"
                f"  uv cache clean {package}\n"
            )
            continue
        except OSError as error:
            # every other way starting the process can fail -- a `uv` this user
            # may not execute, a binary the kernel refuses -- lands here rather
            # than escaping past an uninstall that already removed the config.
            # `KeyboardInterrupt` and `SystemExit` are not `OSError`, so they
            # still travel.
            stdout.write(
                f"\nwarning: 'uv cache clean {package}' could not be started "
                f"({error.strerror or error}); run it manually:\n"
                f"  uv cache clean {package}\n"
            )
            continue
        if result.returncode != 0:
            stdout.write(
                f"\nwarning: 'uv cache clean {package}' failed; run it manually:\n"
                f"  uv cache clean {package}\n"
            )
