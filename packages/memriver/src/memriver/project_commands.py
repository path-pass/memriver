"""``memriver project`` -- the only writers of the project registry.

Every command shows its plan and asks before writing; ``explain`` writes
nothing. None of them ever creates or modifies a file inside a target
directory: the registry lives in the store.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import IO

from memriver_core import StorageFailure
from memriver_core.models import ProjectId

from . import project_context
from .project_context import (
    RegisteredProject,
    Registry,
    RegistryInvalid,
    bind,
    count_child_git_markers,
    load_registry,
    new_project_id,
    project_exists,
    resolve,
    resolve_project,
    unbind,
)

RESTART_NOTE = ("Restart the affected harness sessions and their memriver MCP servers "
                "for the change to take effect.")
SUBTREE_NOTE = ("Every directory under this root -- including repositories added later -- "
                "will share this project's memories.")
INDEPENDENT_NOTE = "These registered sub-projects stay independent:"
OUT_OF_DATE = "refused: the plan is out of date (a path changed while waiting); run the command again"
STORE_FAILURE = "refused: could not complete the registry write; inspect with memriver project explain"
CANNOT_VERIFY = "refused: could not verify the directory's relation to {what}"


def _store_root(root: Path | None, home: Path) -> Path:
    """The store path this command will use: resolved once, compared again later."""
    from memriver_core.config import storage_root

    given = Path(root) if root is not None else storage_root(home=home)
    try:
        return given.resolve()          # non-strict: the store may not exist yet
    except (OSError, RuntimeError, ValueError):
        return given


def _same(a: Path | str, b: Path | str) -> bool | None:
    if str(a) == str(b):
        return True
    return project_context.same_directory(str(a), str(b))


def _canonical_target(directory: Path | None, cwd: Path) -> Path | str:
    target = Path(directory) if directory is not None else cwd
    try:
        canonical = target.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        # ValueError: a path the OS cannot even address (an embedded NUL),
        # exactly as project_context.resolve_project treats it
        return f"refused: {target} is not an existing directory"
    if not canonical.is_dir():
        return f"refused: {canonical} is not a directory"
    return canonical


def _refuse_target(canonical: Path, *, home: Path, store: Path) -> str | None:
    try:
        home_canonical = home.resolve()
    except (OSError, RuntimeError, ValueError):
        home_canonical = home
    verdict = project_context.covers(canonical, home_canonical)
    if verdict is None:
        return CANNOT_VERIFY.format(what="the home directory")
    if verdict:
        return "refused: the filesystem root, the home directory and its ancestors cannot be a project"
    verdict = project_context.covers(canonical, store)
    if verdict is None:
        return CANNOT_VERIFY.format(what="the memory store")
    if verdict:
        return (f"refused: the memory store {store} lies inside this directory; "
                "the registry would end up inside the project")
    verdict = project_context.covers(store, canonical)
    if verdict is None:
        return CANNOT_VERIFY.format(what="the memory store")
    if verdict:
        # the other direction: a directory inside the store is memriver's own
        # bookkeeping, not a project the user works in
        return f"refused: {canonical} lies inside the memory store {store}"
    return None


def _plan_still_valid(canonical: Path, store: Path, *, root: Path | None, home: Path) -> bool:
    """After the prompt: the same target, still a directory, and the same store path."""
    try:
        return canonical.resolve(strict=True) == canonical and canonical.is_dir() \
            and _store_root(root, home) == store
    except (OSError, RuntimeError, ValueError):
        return False


def _confirm(plan: str, *, yes: bool, stdin_is_tty: bool,
             input_fn: Callable[[str], str], stdout: IO[str]) -> int | None:
    stdout.write(plan)
    if yes:
        return None
    if not stdin_is_tty:
        stdout.write("refused: stdin is not a terminal; pass --yes to confirm non-interactively\n")
        return 2
    if input_fn("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        stdout.write("aborted; nothing was written\n")
        return 1
    return None


def _git_count_line(canonical: Path) -> str:
    count = count_child_git_markers(canonical)
    return ("Direct child directories containing a .git marker: "
            + ("could not be counted" if count is None else str(count)))


def _independent_lines(registry: Registry, canonical: Path, project_id: ProjectId) -> str | None:
    """Roots of other projects that lie under the target; None when unverifiable."""
    nested = []
    for p in registry.projects:
        if p.id == project_id:
            continue
        for r in p.roots:
            inside = project_context.covers(canonical, r)
            if inside is None:
                return None
            same = _same(canonical, r)
            if same is None:
                return None
            if inside and not same:
                nested.append((p.id, r))
    if not nested:
        return ""
    return f"  {INDEPENDENT_NOTE}\n" + "".join(f"    {pid}: {r}\n" for pid, r in nested)


def _bind_command(command: str, project_id: ProjectId, *, create: bool, canonical: Path,
                  store: Path, root: Path | None, yes: bool, stdin_is_tty: bool, input_fn,
                  stdout, home: Path) -> int:
    refusal = _refuse_target(canonical, home=home, store=store)
    if refusal:
        stdout.write(refusal + "\n")
        return 2
    try:
        registry = load_registry(store)
    except RegistryInvalid as err:
        stdout.write(f"refused: the registry is invalid ({err.location}: {err.reason}); "
                     "run memriver project explain\n")
        return 2
    existing = next((p for p in registry.projects if p.id == project_id), None)
    for other in registry.projects:
        if other.id == project_id:
            continue
        for r in other.roots:
            same = _same(canonical, r)
            if same is None:
                stdout.write(CANNOT_VERIFY.format(what="registered roots") + "\n")
                return 2
            if same:
                stdout.write(f"refused: {canonical} is already bound to project {other.id}\n")
                return 2
    if existing is not None:
        for r in existing.roots:
            same = _same(canonical, r)
            if same is None:
                stdout.write(CANNOT_VERIFY.format(what="registered roots") + "\n")
                return 2
            if same:
                stdout.write(f"{canonical} is already bound to {project_id}; nothing to do\n")
                return 0
    independent = _independent_lines(registry, canonical, project_id)
    if independent is None:
        stdout.write(CANNOT_VERIFY.format(what="registered roots") + "\n")
        return 2
    roots_line = ("" if existing is None or not existing.roots
                  else "  roots:   " + ", ".join(existing.roots) + "\n")
    plan = (f"memriver project {command}\n"
            f"  store:   {store}\n"
            f"  project: {project_id}  ({'new' if create else 'existing'})\n"
            f"{roots_line}"
            f"  root:    {canonical}\n"
            f"  {SUBTREE_NOTE}\n"
            f"  {_git_count_line(canonical)}\n"
            f"{independent}")
    code = _confirm(plan, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    # the prompt was a window: re-check the two paths the plan showed before the
    # lock creates anything (store_lock makes directories on entry)
    if not _plan_still_valid(canonical, store, root=root, home=home):
        stdout.write(OUT_OF_DATE + "\n")
        return 2
    while True:
        try:
            bind(store, project_id, str(canonical), create=create)
            break
        except ValueError as err:
            if create and str(err) == "project id already exists":
                project_id = new_project_id(canonical.name)   # a concurrent init took it
                continue
            stdout.write(f"refused: {err}\n")
            return 2
        except RegistryInvalid as err:
            stdout.write(f"refused: the registry is invalid ({err.location}: {err.reason})\n")
            return 2
        except StorageFailure:
            stdout.write(STORE_FAILURE + "\n")
            return 2
    stdout.write(f"bound {canonical} to {project_id}\n{RESTART_NOTE}\n")
    return 0


def run_init(directory: Path | None, *, root: Path | None, yes: bool, stdin_is_tty: bool,
             input_fn, stdout, cwd: Path, home: Path) -> int:
    store = _store_root(root, home)
    canonical = _canonical_target(directory, cwd)
    if isinstance(canonical, str):
        stdout.write(canonical + "\n")
        return 2
    project_id = new_project_id(canonical.name)
    while project_exists(store, project_id):
        project_id = new_project_id(canonical.name)
    return _bind_command("init", project_id, create=True, canonical=canonical, store=store,
                         root=root, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn,
                         stdout=stdout, home=home)


def _existing_id(raw: str, store: Path, stdout) -> ProjectId | None:
    if not project_context.valid_project_id(raw) or not project_exists(store, ProjectId(raw)):
        stdout.write(f"no such project: {raw[:255]}\n")
        return None
    return ProjectId(raw)


def run_adopt(project_id: str, directory: Path | None, *, root: Path | None, yes: bool,
              stdin_is_tty: bool, input_fn, stdout, cwd: Path, home: Path) -> int:
    store = _store_root(root, home)
    pid = _existing_id(project_id, store, stdout)
    if pid is None:
        return 2
    canonical = _canonical_target(directory, cwd)
    if isinstance(canonical, str):
        stdout.write(canonical + "\n")
        return 2
    return _bind_command("adopt", pid, create=False, canonical=canonical, store=store,
                         root=root, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn,
                         stdout=stdout, home=home)


def run_unbind(project_id: str, directory: Path, *, root: Path | None, yes: bool,
               stdin_is_tty: bool, input_fn, stdout, cwd: Path, home: Path) -> int:
    store_root = _store_root(root, home)
    pid = _existing_id(project_id, store_root, stdout)
    if pid is None:
        return 2
    literal = str(Path(directory) if Path(directory).is_absolute() else (cwd / directory))
    try:
        registry = load_registry(store_root)
    except RegistryInvalid as err:
        # a registry that fails validation cannot be edited through this command
        # (the lock-time reload would refuse it too); the recovery is manual, and
        # it targets the project the user named -- err.location is where the
        # clash was noticed, which for a duplicate root is usually the *other*,
        # healthy project
        if err.reason == "root is already bound to another project":
            stdout.write(f"refused: the registry is invalid ({err.location}: {err.reason}); "
                         f"edit {store_root / 'projects' / pid / 'project.toml'} by hand and "
                         f"remove the root {literal}, then run memriver project explain\n")
        else:
            stdout.write(f"refused: the registry is invalid ({err.location}: {err.reason}); "
                         "fix that file by hand, then run memriver project explain\n")
        return 2
    project = next((p for p in registry.projects if p.id == pid), None)
    roots = project.roots if project is not None else ()
    key = literal if literal in roots else None
    if key is None:
        try:
            # `literal` is already the given path anchored to the injected cwd:
            # resolving that, not the bare argument, is what makes a relative
            # directory mean the same thing here as it did in the plan
            resolved = str(Path(literal).resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            resolved = None
        key = resolved if resolved in roots else None
    if key is None:
        stdout.write(f"refused: {literal} is not bound to {pid}\n")
        return 2
    after = tuple(r for r in roots if r != key)
    next_registry = Registry(tuple(RegisteredProject(pid, after) if p.id == pid else p
                                   for p in registry.projects))
    afterwards = resolve_project(next_registry, cwd)
    afterwards_line = (f"registered {afterwards.project_id}" if afterwards.state == "registered"
                       else afterwards.state)
    plan = (f"memriver project unbind\n"
            f"  store:   {store_root}\n"
            f"  project: {pid}\n"
            f"  roots before: {', '.join(roots)}\n"
            f"  roots after:  {', '.join(after) or '(none)'}\n"
            f"  the current directory resolves afterwards: {afterwards_line}\n")
    code = _confirm(plan, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    if _store_root(root, home) != store_root:
        stdout.write(OUT_OF_DATE + "\n")
        return 2
    try:
        unbind(store_root, pid, key)
    except (ValueError, RegistryInvalid) as err:
        stdout.write(f"refused: {err}\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    stdout.write(f"unbound {key} from {pid}\n{RESTART_NOTE}\n")
    return 0


def run_explain(*, root: Path | None, project_dir: Path | None, stdout, cwd: Path,
                home: Path) -> int:
    store_root = _store_root(root, home)
    start = Path(project_dir) if project_dir is not None else cwd
    resolution = resolve(store_root, start)
    try:
        canonical = str(start.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        canonical = str(start)
    lines = [f"store: {store_root}", f"cwd: {canonical}", f"state: {resolution.state}"]
    if resolution.state == "registered":
        lines += [f"project: {resolution.project_id}", f"root: {resolution.root}",
                  f"scopes: global, project:{resolution.project_id}",
                  f"writes: project:{resolution.project_id}"]
    else:
        if resolution.state == "degraded":
            lines.append(f"diagnostic: {resolution.diagnostic}")
        lines += ["scopes: global", "writes: none"]
    stdout.write("\n".join(lines) + "\n")
    return 1 if resolution.state == "degraded" else 0
