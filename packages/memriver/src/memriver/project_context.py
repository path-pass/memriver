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
import re
import secrets
import stat
import tempfile
import tomllib
from collections.abc import Callable
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
    "missing-roots": "project file has no roots key",
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
        info = os.lstat(path)
    except FileNotFoundError:
        return ()
    except OSError as err:
        raise RegistryInvalid(location, REASONS["unreadable"]) from err
    if stat.S_ISLNK(info.st_mode):
        # a link is never followed, live or dangling: its target is chosen
        # outside the store layout and must not decide what the project owns
        raise RegistryInvalid(location, REASONS["unreadable"])
    try:
        raw = path.read_bytes()
    except OSError as err:
        # exists (or is a dangling link) but cannot be read: not "unbound"
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

def new_project_id(directory_name: str) -> ProjectId:
    """``<normalized name>-<16 hex>``, fitted to one 255-byte path component."""
    suffix = "-" + secrets.token_hex(8)
    name = re.sub(r"[^a-z0-9]+", "-", directory_name.lower()).strip("-")
    budget = _MAX_FILENAME_BYTES - len(suffix)
    name = name.encode()[:budget].decode("utf-8", errors="ignore").rstrip("-") or "project"
    return ProjectId(f"{name}{suffix}")


def project_exists(store_root: Path, project_id: ProjectId) -> bool:
    return (store_root / PROJECTS_DIRNAME / project_id).is_dir()


def bind(store_root: Path, project_id: ProjectId, root: str, *, create: bool) -> None:
    """Append ``root`` to the project's roots; ``create`` says whether the id is new.

    Re-validates the world under the lock: the id's existence, the root still
    being the canonical directory the caller confirmed, and the registry state.
    """
    def change(current: tuple[str, ...] | None, registry: Registry) -> tuple[str, ...] | None:
        if create and os.path.lexists(store_root / PROJECTS_DIRNAME / project_id):
            # lexists, not the registry: a stray file under that name is skipped
            # by load_registry but would still collide with the directory to create
            raise ValueError("project id already exists")
        if not create and current is None:
            raise ValueError("no such project")
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


def unbind(store_root: Path, project_id: ProjectId, root: str) -> None:
    """Remove every element string-equal to ``root``; the path need not exist.

    Other spellings that alias the same directory are left alone, and a
    hand-edited file holding one spelling twice comes back clean.
    """
    def change(current: tuple[str, ...] | None, registry: Registry) -> tuple[str, ...] | None:
        if current is None or root not in current:
            raise ValueError("root is not bound to this project")
        return tuple(r for r in current if r != root)

    _rewrite(store_root, project_id, change)


def _rewrite(store_root: Path, project_id: ProjectId,
             change: Callable[[tuple[str, ...] | None, Registry], tuple[str, ...] | None]) -> None:
    # imported here so that importing this module (the Stop hook does, every
    # turn) never loads the core service stack that bootstrap pulls in
    from memriver_core.bootstrap import store_lock

    if not valid_project_id(project_id):
        raise ValueError("invalid project id")
    with store_lock(store_root):
        registry = load_registry(store_root)   # RegistryInvalid propagates: never write over a broken registry
        current = next((p.roots for p in registry.projects if p.id == project_id), None)
        roots = change(current, registry)
        if roots is None:
            return
        _write_document(store_root, store_root / PROJECTS_DIRNAME / project_id, roots)


def _write_document(store_root: Path, project_dir: Path, roots: tuple[str, ...]) -> None:
    import tomlkit

    document = tomlkit.document()
    document["roots"] = list(roots)
    text = tomlkit.dumps(document)
    _mkdir_private(store_root, project_dir)
    fd, tmp = tempfile.mkstemp(dir=project_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # inside the with: a failing fchmod must still close the descriptor
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
        os.replace(tmp, project_dir / REGISTRY_FILENAME)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _mkdir_private(store_root: Path, directory: Path) -> None:
    """``mkdir -p`` from ``store_root`` down to ``directory``, 0700 on created levels only."""
    d = store_root
    for part in ("", *directory.relative_to(store_root).parts):
        d = d / part if part else d
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        d.chmod(0o700)


def count_child_git_markers(directory: Path) -> int | None:
    """Direct children holding a ``.git`` entry of any kind; ``None`` when unlistable."""
    try:
        with os.scandir(directory) as children:
            return sum(1 for child in children
                       if child.is_dir(follow_symlinks=False)
                       and os.path.lexists(os.path.join(child.path, ".git")))
    except OSError:
        return None
