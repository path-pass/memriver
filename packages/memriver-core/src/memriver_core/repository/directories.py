"""Directory facts and rules shared by every store backend. Stdlib only.

The application layer may not touch the filesystem, and none of these rules
belongs to one backend: which bound directory a path lies under, whether a
directory would swallow the home directory or the store, and deleting a whole
store directory without ever following a symlink. Moved from the umbrella
(project_context, uninstall) with their behaviour unchanged.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self


def same_directory(a: str, b: str) -> bool | None:
    """``os.path.samefile`` with "absent" and "could not check" told apart.

    False: both exist and differ, one of them does not exist (an offline
    root is not a match), or one cannot be addressed at all. None: the check
    itself failed (permission, I/O) -- no caller may read that as "no match".
    Every comparison in this package goes through this one name, so a test
    can swap it for a whole module.
    """
    try:
        return os.path.samefile(a, b)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        # ValueError: a path the OS cannot even address (an embedded NUL) --
        # definitely not this directory, and no amount of retrying changes it
        return False
    except OSError:
        return None


def covers(outer: Path | str, inner: Path | str) -> bool | None:
    """``outer`` is ``inner`` or one of its ancestors, by string or by ``same_directory``.

    None as soon as one comparison could not be checked.
    """
    inner_path = Path(inner)
    for candidate in [inner_path, *inner_path.parents]:
        if str(outer) == str(candidate):
            return True
        same = same_directory(str(outer), str(candidate))
        if same is None:
            return None
        if same:
            return True
    return False


UNRESOLVABLE_START = "working directory could not be resolved"


def canonical_directory(path: str) -> str | None:
    """The strict realpath of an existing directory; None for anything else.

    Never raises: the hooks take a start directory from the harness payload,
    and a NUL in it makes resolve() raise ValueError rather than OSError.
    """
    try:
        canonical = Path(path).resolve(strict=True)
        if not canonical.is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return str(canonical)


RootState = Literal["ok", "missing", "not-canonical", "unverifiable"]


class _Unverifiable(Exception):
    """lstat itself failed somewhere on the way up: not offline, not checkable."""


def _nearest_existing(root: str) -> str | None:
    """The root or its closest ancestor that lstat finds; None when none exists."""
    for candidate in [Path(root), *Path(root).parents]:
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as err:
            # ValueError: a path the OS cannot even address (an embedded NUL)
            raise _Unverifiable from err
        return str(candidate)
    return None


def root_state(root: str) -> RootState:
    """How a bound root stands on disk now.

    The nearest existing path among the root and its ancestors is what a
    redirect would have replaced: checking only the leaf would let a symlinked
    ancestor with a missing leaf pass as an offline root. A root with no
    existing component, or whose leaf is absent or not a directory, is
    offline ("missing"), which never degrades resolution.
    """
    try:
        existing = _nearest_existing(root)
    except _Unverifiable:
        return "unverifiable"
    if existing is None:
        return "missing"
    if os.path.realpath(existing) != existing:
        return "not-canonical"
    if existing != root:
        return "missing"
    try:
        return "ok" if stat.S_ISDIR(os.stat(root).st_mode) else "missing"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unverifiable"


def integrity_diagnostic(roots: Sequence[str]) -> str | None:
    """A diagnostic when any root was re-pointed or cannot be checked; None otherwise."""
    for root in roots:
        state = root_state(root)
        if state == "unverifiable":
            return f"{root}: registered root could not be checked"
        if state == "not-canonical":
            return f"{root}: registered root is no longer a canonical path"
    return None


@dataclass(frozen=True)
class Match:
    state: Literal["registered", "none", "degraded"]
    project_id: str | None = None
    diagnostic: str | None = None


def nearest_bound(start: str, bound: Sequence[tuple[str, str]]) -> Match:
    """The project `start` belongs to among `(project_id, root)` pairs.

    Integrity first: one re-pointed or uncheckable root degrades everything,
    never a silent fall-through to a parent project or to none. Then the
    nearest ancestor wins, with exact and same-directory matches pooled per
    level: text uniqueness of roots is not physical uniqueness.
    """
    canonical = canonical_directory(start)
    if canonical is None:
        return Match("degraded", diagnostic=UNRESOLVABLE_START)
    diagnostic = integrity_diagnostic([root for _, root in bound])
    if diagnostic is not None:
        return Match("degraded", diagnostic=diagnostic)
    cwd = Path(canonical)
    for ancestor in [cwd, *cwd.parents]:
        key = str(ancestor)
        ids: set[str] = set()
        for project_id, root in bound:
            if root == key:
                ids.add(project_id)
                continue
            same = same_directory(root, key)
            if same is None:
                return Match("degraded", diagnostic=f"{root}: registered root could not be checked")
            if same:
                ids.add(project_id)
        if len(ids) > 1:
            return Match("degraded", diagnostic=f"{key}: matched by more than one project")
        if ids:
            return Match("registered", project_id=ids.pop())
    return Match("none")


PurgeRefusalKind = Literal["unresolvable", "symlink", "too-broad", "not-directory",
                           "unopenable"]


@dataclass(frozen=True)
class PurgeRefusal:
    """Why a purge target is refused; nothing was removed.

    `path` is what the refusal is about (the given link, the base that failed
    to resolve, or the canonical target); `canonical` is the target once it
    resolved, None when it never did; `detail` is the OS reason text for
    unresolvable/unopenable, for the caller's message.
    """

    kind: PurgeRefusalKind
    path: Path
    canonical: Path | None
    detail: str = ""


@dataclass
class PurgePlan:
    """A purge target that passed every check, held open while the user decides.

    `fd`/`confirmed` bind the deletion to the directory object shown, so the
    prompt is not a window for swapping the target. Close it (or use it as a
    context manager) whatever the answer.
    """

    given: Path
    canonical: Path
    home: Path
    cwd: Path
    exists: bool
    confirmed: os.stat_result | None = None
    fd: int | None = None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


PurgeOutcome = Literal["removed", "unopenable-parent", "uncheckable", "not-confirmed",
                       "refused", "partly-removed", "replaced-root"]


@dataclass(frozen=True)
class PurgeResult:
    outcome: PurgeOutcome
    path: Path
    detail: str = ""
    replaced: tuple[Path, ...] = ()
    refusal: PurgeRefusal | None = None


def _resolve(path: Path) -> Path:
    """``path.resolve()`` that raises on a symlink loop on every interpreter.

    Since Python 3.13 a non-strict ``resolve()`` returns a loop unresolved
    instead of raising, which would let a guard reason about a path that names
    nothing. Strict resolution raises on a loop everywhere (``OSError`` on
    3.13+, ``RuntimeError`` on 3.12); only a missing component falls back to
    the non-strict form, since a purge target that does not exist yet is legal.

    The fallback's result is resolved strictly once more: ``<missing>/..``
    collapses in the non-strict form, so what it returns can still end in the
    loop the missing component hid. Only "still missing" is accepted there.
    """
    try:
        return path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        resolved = path.resolve()
    try:
        resolved.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        pass
    return resolved


def _refuse_purge_target(given: Path, canonical: Path, *, home: Path,
                         cwd: Path) -> PurgeRefusal | None:
    """The refusal for a target too dangerous to delete, or ``None``.

    Both checks run against ``canonical`` -- the destination the deletion would
    actually walk -- because a symlinked component anywhere above the leaf
    redirects the deletion somewhere the given spelling never named, and a
    relative path or a ``..`` chain names it only after resolution.

    A leaf that is itself a symlink is still refused outright rather than
    followed: ``--root`` names the store, and a link standing in for it is an
    arrangement memriver will not delete through.
    """
    # `lstat` directly, not `Path.is_symlink()`: on 3.14 that answers False
    # for any failed check, reading "could not look" as "not a link"
    try:
        is_link = stat.S_ISLNK(given.lstat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        is_link = False
    except OSError as error:
        return PurgeRefusal("unresolvable", given, canonical, str(error))
    if is_link:
        return PurgeRefusal("symlink", given, canonical)
    # `home`/`cwd` being relative to the target covers the target *being* one of
    # them and the target being any ancestor of one -- the filesystem root
    # included, since every path is relative to it
    for base in (home, cwd):
        try:
            resolved = _resolve(base)
        except (OSError, RuntimeError) as error:
            return PurgeRefusal("unresolvable", base, canonical, str(error))
        if resolved.is_relative_to(canonical):
            return PurgeRefusal("too-broad", canonical, canonical)
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


def plan_purge(given: Path, *, home: Path, cwd: Path,
               dry_run: bool = False) -> PurgePlan | PurgeRefusal:
    """Canonicalize and check a purge target; open it unless this is a dry run.

    The canonical path -- not the spelling that produced it -- is what every
    guard runs against and what the caller shows.
    """
    if not given.is_absolute():
        given = cwd / given
    try:
        canonical = _resolve(given)
    except (OSError, RuntimeError) as error:
        return PurgeRefusal("unresolvable", given, None, str(error))
    refusal = _refuse_purge_target(given, canonical, home=home, cwd=cwd)
    if refusal is not None:
        return refusal
    # `stat` directly, not `Path.exists()/is_dir()`: on 3.14 those swallow
    # every OSError, and a store that cannot be checked would be reported as
    # no data to remove
    try:
        mode = canonical.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return PurgePlan(given, canonical, home, cwd, exists=False)
    except OSError as error:
        return PurgeRefusal("unresolvable", canonical, canonical, str(error))
    if not stat.S_ISDIR(mode):
        return PurgeRefusal("not-directory", canonical, canonical)
    if dry_run:
        return PurgePlan(given, canonical, home, cwd, exists=True)
    try:
        fd = _open_directory_without_following_symlinks(canonical)
    except OSError as error:
        # the no-follow walk is what makes any component -- leaf or ancestor --
        # turned into a symlink between the guards above and here fail closed
        # rather than redirect the open onto a different real directory
        return PurgeRefusal("unopenable", canonical, canonical, error.strerror or str(error))
    try:
        confirmed = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    return PurgePlan(given, canonical, home, cwd, exists=True, confirmed=confirmed, fd=fd)


def purge(plan: PurgePlan) -> PurgeResult:
    """Delete the one directory ``plan.confirmed`` identifies, or nothing at all.

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
    if plan.confirmed is None or plan.fd is None:
        raise ValueError("purge needs an open plan made for deletion (not a dry run, not closed)")
    canonical = plan.canonical
    try:
        parent_fd = _open_directory_without_following_symlinks(canonical.parent)
    except OSError as error:
        return PurgeResult("unopenable-parent", canonical.parent, error.strerror or str(error))
    try:
        try:
            leaf_fd = os.open(canonical.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                              dir_fd=parent_fd)
        except OSError as error:
            return PurgeResult("uncheckable", canonical, error.strerror or str(error))
        try:
            if not _is_confirmed(os.fstat(leaf_fd), plan.confirmed):
                return PurgeResult("not-confirmed", canonical)
            # the object is the confirmed one; the last question is whether the
            # protected bases moved into it while the prompt was open
            refusal = _refuse_purge_target(plan.given, canonical, home=plan.home, cwd=plan.cwd)
            if refusal is not None:
                return PurgeResult("refused", canonical, refusal=refusal)
            replaced: list[Path] = []
            try:
                _empty_directory(leaf_fd, directory=canonical, replaced=replaced)
            except OSError as error:
                return PurgeResult("partly-removed", canonical, error.strerror or str(error),
                                   tuple(replaced))
            try:
                still_there = os.stat(canonical.name, dir_fd=parent_fd, follow_symlinks=False)
                if not _is_confirmed(still_there, plan.confirmed):
                    return PurgeResult("replaced-root", canonical, replaced=tuple(replaced))
                os.rmdir(canonical.name, dir_fd=parent_fd)
            except OSError as error:
                return PurgeResult("partly-removed", canonical, error.strerror or str(error),
                                   tuple(replaced))
        finally:
            os.close(leaf_fd)
    finally:
        os.close(parent_fd)
    return PurgeResult("removed", canonical)


def _is_confirmed(candidate: os.stat_result, confirmed: os.stat_result) -> bool:
    return (candidate.st_dev, candidate.st_ino) == (confirmed.st_dev,
                                                    confirmed.st_ino)


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
    standing at that name is appended to ``replaced``, which the
    partial-removal report names. ``directory`` is carried only to spell those
    paths out for the user.

    The descriptor is held open across that comparison and across the ``rmdir``
    itself, never closed the moment the recursion returns. An open descriptor
    keeps the original inode allocated, so a replacement created after the
    original is renamed away *and* unlinked cannot be handed the same inode and
    pass the comparison by wearing the confirmed identity.

    What remains is best-effort, not a guarantee: POSIX offers no atomic "stat
    and rmdir", so a stranger that takes the name in the instant between the
    two is still removed. The window needs exact, very short concurrent timing,
    and ``rmdir`` never touches a non-empty directory, but it is not closed.
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
            still_there = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
            if not _is_confirmed(still_there, opened):
                replaced.append(directory / child.name)
                continue
            os.rmdir(child.name, dir_fd=fd)
        finally:
            os.close(child_fd)
