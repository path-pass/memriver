"""Project identity: the registry in the store is the only source.

A directory belongs to a project when it is, or lies under, a root that the
user bound to that project with ``memriver project init``/``adopt``. Nothing
else confers identity -- not a ``.git`` directory, not a marker file, not a
path hash. The registry maps directories to project ids; the Project itself
(its name, its existence) is core's, reached through the service.

``find_git_root`` survives for one unrelated job: the Cursor/Kiro installers
place their static instruction file at the nearest git root.
"""

from __future__ import annotations

import os
import stat
import tomllib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from memriver_core import ProjectNotFound
from memriver_core.models import ID_RE, single_line

REGISTRY_DIRNAME = "registry"
REGISTRY_SUFFIX = ".toml"
_HEADER_FIELD_CHARS = 120

# fixed, client-safe wording per invalid-registry cause: the TOML message, the
# offending value and the absolute path never travel with the exception
REASONS = {
    "bad-name": "registry file name is not an addressable project id",
    "unlistable": "registry entry could not be read",
    "unreadable": "registry file could not be read",
    "not-toml": "registry file is not valid TOML",
    "missing-roots": "registry file has no roots key",
    "extra-key": "registry file has a key other than roots",
    "not-strings": "roots is not an array of strings",
    "not-absolute": "root is not an absolute path",
    "not-addressable": "root is not an addressable path",
    "duplicate-root": "root is already bound to another project",
    "unverifiable": "root could not be checked",
}
_UNRESOLVABLE_CWD = "working directory could not be resolved"

# What the management surfaces (the project commands, doctor) neutralise
# before printing a registry-derived string: a directory or file name the
# user can hand-edit to carry a newline (forging a second output line) or an
# ANSI escape (a raw terminal control sequence). Categorised rather than
# enumerated, because a code-point list keeps missing things a real name
# carries: Cc is the C0/C1 controls, Cf the format controls (U+202E
# RIGHT-TO-LEFT OVERRIDE reorders the line a terminal draws without being a
# control character), Zl/Zp the line and paragraph separators U+2028/U+2029,
# and Cs the lone surrogates that a filename no codec accepts arrives as --
# those turn back into their original raw byte the moment stdout, which uses
# surrogateescape on a terminal, encodes them.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def find_git_root(start: Path) -> Path | None:
    """The nearest ``.git`` root at or above ``start``, or ``None`` outside a repo."""
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None


@dataclass(frozen=True)
class RegisteredProject:
    id: str
    roots: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    projects: tuple[RegisteredProject, ...]


class RegistryInvalid(Exception):
    def __init__(self, location: str, reason: str) -> None:
        super().__init__(f"{location}: {reason}")
        self.location = location
        self.reason = reason


ResolutionState = Literal["registered", "none", "degraded"]


@dataclass(frozen=True)
class ProjectResolution:
    """Which registered project a directory resolves to; nothing about the Project itself."""

    state: ResolutionState
    project_id: str | None
    root: str | None
    diagnostic: str | None


def _degraded(diagnostic: str) -> ProjectResolution:
    return ProjectResolution("degraded", None, None, diagnostic)


def header_field(value: str) -> str:
    """One registry- or store-derived value as it may appear in an agent-facing header."""
    return single_line(value)[:_HEADER_FIELD_CHARS]


def visible(text: str) -> str:
    """``text`` with every invisible character replaced by one space, one for one.

    Unlike ``single_line`` this never collapses or strips ordinary spaces: a
    management surface shows a path as the user spelled it, so a root named
    ``two  spaces`` stays recognisable. The agent-facing header keeps
    ``single_line`` and its length cap.
    """
    return "".join(
        " " if unicodedata.category(char) in _INVISIBLE_CATEGORIES else char
        for char in text
    )


def valid_project_id(name: str) -> bool:
    return bool(ID_RE.fullmatch(name))


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


def load_registry(store_root: Path) -> Registry:
    """Every registry file under ``<store>/registry``, fully validated.

    A missing ``registry/`` is an empty registry. Anything that cannot be read
    or does not have the one accepted shape raises ``RegistryInvalid`` -- the
    caller must not resolve against a registry it could only partly read,
    because the unread part may be the nearer root.
    """
    registry_dir = store_root / REGISTRY_DIRNAME
    try:
        info = os.lstat(registry_dir)
    except FileNotFoundError:
        return Registry(())
    except OSError as err:
        raise RegistryInvalid(REGISTRY_DIRNAME, REASONS["unlistable"]) from err
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        # a link or a file where the registry should be is never followed and
        # never read as "no projects"
        raise RegistryInvalid(REGISTRY_DIRNAME, REASONS["unlistable"])
    try:
        children = sorted(os.scandir(registry_dir), key=lambda e: e.name)
    except OSError as err:
        raise RegistryInvalid(REGISTRY_DIRNAME, REASONS["unlistable"]) from err
    projects: list[RegisteredProject] = []
    bound: list[tuple[str, str]] = []          # (root, id) seen so far
    for child in children:
        location = f"{REGISTRY_DIRNAME}/{child.name}"
        try:
            if child.is_symlink():
                # skipping it could drop the nearer project and hand its
                # directories to a parent: refuse instead
                raise RegistryInvalid(location, REASONS["unlistable"])
        except OSError as err:
            raise RegistryInvalid(REGISTRY_DIRNAME, REASONS["unlistable"]) from err
        if not child.name.endswith(REGISTRY_SUFFIX):
            continue                          # a stray: not a registry file by name
        project_id = child.name[: -len(REGISTRY_SUFFIX)]
        if not valid_project_id(project_id):
            raise RegistryInvalid(location, REASONS["bad-name"])
        # a correctly named directory, FIFO, socket or device is refused by
        # _read_roots, never skipped
        roots = _read_roots(Path(child.path), location)
        for root in roots:
            for other_root, other_id in bound:
                if other_id == project_id:
                    continue
                same = root == other_root or same_directory(root, other_root)
                if same is None:
                    raise RegistryInvalid(location, REASONS["unverifiable"])
                if same:
                    raise RegistryInvalid(location, REASONS["duplicate-root"])
            bound.append((root, project_id))
        projects.append(RegisteredProject(project_id, roots))
    return Registry(tuple(projects))


def _read_roots(path: Path, location: str) -> tuple[str, ...]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ()
    except OSError as err:
        raise RegistryInvalid(location, REASONS["unreadable"]) from err
    if not stat.S_ISREG(info.st_mode):
        # only a regular file is ever opened. A link, live or dangling, is
        # never followed: its target is chosen outside the store layout and
        # must not decide what the project owns. A FIFO, socket, device or
        # directory is refused here because opening one can block forever
        # or read something that is not the registry
        raise RegistryInvalid(location, REASONS["unreadable"])
    try:
        raw = path.read_bytes()
    except OSError as err:
        # exists but cannot be read: not "unbound"
        raise RegistryInvalid(location, REASONS["unreadable"]) from err
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise RegistryInvalid(location, REASONS["not-toml"]) from err
    if "roots" not in document:
        raise RegistryInvalid(location, REASONS["missing-roots"])
    if set(document) != {"roots"}:
        raise RegistryInvalid(location, REASONS["extra-key"])
    roots = document["roots"]
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise RegistryInvalid(location, REASONS["not-strings"])
    if not all(os.path.isabs(r) for r in roots):
        raise RegistryInvalid(location, REASONS["not-absolute"])
    if any("\x00" in r for r in roots):
        raise RegistryInvalid(location, REASONS["not-addressable"])
    return tuple(roots)


def root_integrity(registry: Registry) -> str | None:
    """A diagnostic when any root's nearest existing path is no longer canonical.

    The nearest existing path among the root and its ancestors is what a
    redirect would have replaced: checking only the leaf would let a symlinked
    ancestor with a missing leaf pass as an offline root. A root with no
    existing component at all is offline and passes.
    """
    for project in registry.projects:
        for root in project.roots:
            existing = _nearest_existing(root)
            if isinstance(existing, str) and existing.startswith("error:"):
                return f"{root}: registered root could not be checked"
            if existing is None:
                continue                      # fully absent: offline
            if os.path.realpath(existing) != existing:
                return f"{root}: registered root is no longer a canonical path"
    return None


def _nearest_existing(root: str) -> str | None:
    """The root or its closest ancestor that ``lstat`` finds; ``"error:"`` on failure."""
    for candidate in [Path(root), *Path(root).parents]:
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            # ValueError: a path the OS cannot even address (an embedded
            # NUL) -- not offline, but not checkable either
            return "error:"
        return str(candidate)
    return None


def resolve_project(registry: Registry, start: Path) -> ProjectResolution:
    """The project ``start`` belongs to: nearest registered ancestor wins."""
    try:
        cwd = Path(start).resolve(strict=True)
        if not cwd.is_dir():
            return _degraded(_UNRESOLVABLE_CWD)
    except (OSError, RuntimeError, ValueError):
        # start is not always the OS cwd -- the hooks take it from the harness
        # payload, and a NUL in it makes resolve() raise ValueError, not OSError
        return _degraded(_UNRESOLVABLE_CWD)
    integrity = root_integrity(registry)
    if integrity is not None:
        return _degraded(integrity)
    for ancestor in [cwd, *cwd.parents]:
        key = str(ancestor)
        exact: list[tuple[RegisteredProject, str]] = []
        alias: list[tuple[RegisteredProject, str]] = []
        for project in registry.projects:
            for root in project.roots:
                if root == key:
                    exact.append((project, root))
                    continue
                same = same_directory(root, key)
                if same is None:
                    return _degraded(f"{root}: registered root could not be checked")
                if same:
                    alias.append((project, root))
        ids = {project.id for project, _ in exact + alias}
        if len(ids) > 1:
            return _degraded(f"{key}: matched by more than one project")
        if ids:
            project, root = (exact or alias)[0]
            return ProjectResolution("registered", project.id, root, None)
    return ProjectResolution("none", None, None, None)


def resolve(store_root: Path, start: Path) -> ProjectResolution:
    """``load_registry`` + ``resolve_project``; an invalid registry is ``degraded``."""
    try:
        registry = load_registry(store_root)
    except RegistryInvalid as err:
        return _degraded(f"{err.location}: {err.reason}")
    return resolve_project(registry, start)


# --- registry writes ---

def bind(store_root: Path, service, project_id: str, root: str) -> None:
    """Append ``root`` to the project's registry file.

    Re-validates the world under the store lock: the Project exists and is not
    global (core's answers; service reads never take the lock), the root is
    still the canonical directory the caller confirmed, and no other project
    binds it or an alias of it. A StorageFailure from the service propagates.
    """
    def change(current: tuple[str, ...] | None, registry: Registry) -> tuple[str, ...] | None:
        try:
            service.read_project(project_id)
        except ProjectNotFound:
            raise ValueError("no such project") from None
        if project_id == service.global_project_id():
            raise ValueError("the global project cannot be bound to a directory")
        if not os.path.isdir(root) or os.path.realpath(root) != root:
            raise ValueError("root is not a canonical existing directory")
        for other in registry.projects:
            if other.id != project_id and _bound_here(root, other.roots):
                raise ValueError("root is already bound to another project")
        roots = current or ()
        if _bound_here(root, roots):
            return None                       # already bound (by string or alias): nothing to write
        return (*roots, root)

    _rewrite(store_root, project_id, change)


def _bound_here(root: str, roots: tuple[str, ...]) -> bool:
    """Whether ``root`` is one of ``roots`` by string or alias; a None check refuses."""
    for r in roots:
        if root == r:
            return True
        same = same_directory(root, r)
        if same is None:
            raise ValueError("root could not be checked")
        if same:
            return True
    return False


def unbind(store_root: Path, project_id: str, root: str) -> None:
    """Remove every element string-equal to ``root``; the path need not exist.

    Other spellings that alias the same directory are left alone, and a
    hand-edited file holding one spelling twice comes back clean.
    """
    def change(current: tuple[str, ...] | None, registry: Registry) -> tuple[str, ...] | None:
        if current is None or root not in current:
            raise ValueError("root is not bound to this project")
        return tuple(r for r in current if r != root)

    _rewrite(store_root, project_id, change)


def _rewrite(store_root: Path, project_id: str,
             change: Callable[[tuple[str, ...] | None, Registry], tuple[str, ...] | None]) -> None:
    # imported here so that importing this module (the Stop hook does, every
    # turn) never loads the core service stack that bootstrap pulls in
    from memriver_core.bootstrap import replace_file, store_lock

    if not valid_project_id(project_id):
        raise ValueError("invalid project id")
    with store_lock(store_root):
        registry = load_registry(store_root)   # RegistryInvalid propagates: never write over a broken registry
        current = next((p.roots for p in registry.projects if p.id == project_id), None)
        roots = change(current, registry)
        if roots is None:
            return
        # an OSError here (a symlinked registry/ included) leaves store_lock
        # as StorageFailure
        replace_file(store_root, store_root / REGISTRY_DIRNAME / f"{project_id}{REGISTRY_SUFFIX}",
                     _render(roots))


def _render(roots: tuple[str, ...]) -> str:
    import tomlkit

    document = tomlkit.document()
    document["roots"] = list(roots)
    return tomlkit.dumps(document)


def count_child_git_markers(directory: Path) -> int | None:
    """Direct children holding a ``.git`` entry of any kind; ``None`` when unlistable."""
    try:
        with os.scandir(directory) as children:
            return sum(1 for child in children
                       if child.is_dir(follow_symlinks=False)
                       and os.path.lexists(os.path.join(child.path, ".git")))
    except OSError:
        return None
