"""Project identity: the registry in the store is the only source.

A directory belongs to a project when it is, or lies under, a root that the
user bound to that project with ``memriver project init``/``adopt``. Nothing
else confers identity -- not a ``.git`` directory, not a marker file, not a
path hash. The core still knows a project only as a ``ProjectId``; this
module is where a working directory becomes one.

``find_git_root`` survives for one unrelated job: the Cursor/Kiro installers
place their static instruction file at the nearest git root.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from memriver_core.models import PROJECT_ID_RE, AccessContext, ProjectId, single_line

PROJECTS_DIRNAME = "projects"
REGISTRY_FILENAME = "project.toml"
_MAX_FILENAME_BYTES = 255
_HEADER_FIELD_CHARS = 120

# fixed, client-safe wording per invalid-registry cause: the TOML message, the
# offending value and the absolute path never travel with the exception
REASONS = {
    "bad-name": "project directory name is not an addressable project id",
    "unlistable": "project directory could not be read",
    "unreadable": "project file could not be read",
    "not-toml": "project file is not valid TOML",
    "extra-key": "project file has a key other than roots",
    "not-strings": "roots is not an array of strings",
    "not-absolute": "root is not an absolute path",
    "not-addressable": "root is not an addressable path",
    "duplicate-root": "root is already bound to another project",
    "unverifiable": "root could not be checked",
}
_UNRESOLVABLE_CWD = "working directory could not be resolved"


def find_git_root(start: Path) -> Path | None:
    """The nearest ``.git`` root at or above ``start``, or ``None`` outside a repo."""
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None


@dataclass(frozen=True)
class RegisteredProject:
    id: ProjectId
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
    state: ResolutionState
    project_id: ProjectId | None
    root: str | None
    diagnostic: str | None

    def context(self) -> AccessContext:
        return AccessContext(project_id=self.project_id)

    def header(self) -> str:
        if self.state == "registered":
            return f"project: {self.project_id} (root {_field(self.root or '')})"
        if self.state == "none":
            return ("project: none — global is read-only; "
                    "ask the user to run memriver project init")
        return (f"project: unavailable — registry invalid ({_field(self.diagnostic or '')}); "
                "ask the user to run memriver project explain")


def _degraded(diagnostic: str) -> ProjectResolution:
    return ProjectResolution("degraded", None, None, diagnostic)


def _field(value: str) -> str:
    return single_line(value)[:_HEADER_FIELD_CHARS]


def valid_project_id(name: str) -> bool:
    return bool(PROJECT_ID_RE.fullmatch(name)) and len(name.encode()) <= _MAX_FILENAME_BYTES


def same_directory(a: str, b: str) -> bool | None:
    """``os.path.samefile`` with "absent" and "could not check" told apart.

    False: both exist and differ, or one of them does not exist (an offline
    root is not a match). None: the check itself failed (permission, I/O) --
    no caller may read that as "no match". Every comparison in this package
    goes through this one name, so a test can swap it for a whole module.
    """
    try:
        return os.path.samefile(a, b)
    except (FileNotFoundError, NotADirectoryError):
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
    """Every project directory under ``<store>/projects``, fully validated.

    A missing ``projects/`` directory is an empty registry. Anything that
    cannot be read or does not have the one accepted shape raises
    ``RegistryInvalid`` -- the caller must not resolve against a registry it
    could only partly read, because the unread part may be the nearer root.
    """
    projects_dir = store_root / PROJECTS_DIRNAME
    try:
        info = os.lstat(projects_dir)
    except FileNotFoundError:
        return Registry(())
    except OSError as err:
        raise RegistryInvalid(PROJECTS_DIRNAME, REASONS["unlistable"]) from err
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        # a link or a file where the registry container should be is never
        # followed and never read as "no projects"
        raise RegistryInvalid(PROJECTS_DIRNAME, REASONS["unlistable"])
    try:
        children = sorted(os.scandir(projects_dir), key=lambda e: e.name)
    except OSError as err:
        raise RegistryInvalid(PROJECTS_DIRNAME, REASONS["unlistable"]) from err
    projects: list[RegisteredProject] = []
    bound: list[tuple[str, str]] = []          # (root, id) seen so far
    for child in children:
        location = f"{PROJECTS_DIRNAME}/{child.name}"
        try:
            if child.is_symlink():
                # skipping it could drop the nearer sub-project and hand its
                # directories to a parent: refuse instead
                raise RegistryInvalid(location, REASONS["unlistable"])
            if not child.is_dir(follow_symlinks=False):
                continue                      # a stray file
        except OSError as err:
            raise RegistryInvalid(PROJECTS_DIRNAME, REASONS["unlistable"]) from err
        if not valid_project_id(child.name):
            raise RegistryInvalid(location, REASONS["bad-name"])
        file_location = f"{location}/{REGISTRY_FILENAME}"
        roots = _read_roots(Path(child.path) / REGISTRY_FILENAME, file_location)
        for root in roots:
            for other_root, other_id in bound:
                if other_id == child.name:
                    continue
                same = root == other_root or same_directory(root, other_root)
                if same is None:
                    raise RegistryInvalid(file_location, REASONS["unverifiable"])
                if same:
                    raise RegistryInvalid(file_location, REASONS["duplicate-root"])
            bound.append((root, child.name))
        projects.append(RegisteredProject(ProjectId(child.name), roots))
    return Registry(tuple(projects))


def _read_roots(path: Path, location: str) -> tuple[str, ...]:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return ()
    except OSError as err:
        raise RegistryInvalid(location, REASONS["unreadable"]) from err
    try:
        raw = path.read_bytes()
    except OSError as err:
        # exists (or is a dangling link) but cannot be read: not "unbound"
        raise RegistryInvalid(location, REASONS["unreadable"]) from err
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise RegistryInvalid(location, REASONS["not-toml"]) from err
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
        except OSError:
            return "error:"
        return str(candidate)
    return None


def resolve_project(registry: Registry, start: Path) -> ProjectResolution:
    """The project ``start`` belongs to: nearest registered ancestor wins."""
    try:
        cwd = Path(start).resolve(strict=True)
        if not cwd.is_dir():
            return _degraded(_UNRESOLVABLE_CWD)
    except (OSError, RuntimeError):
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
