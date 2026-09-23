"""``memriver project`` -- the only writers of the directory registry.

Every command shows its plan and asks before writing; ``explain`` writes
nothing. None of them ever creates or modifies a file inside a target
directory: the registry and the projects live in the store. ``init`` creates
the Project through the core service (its id is generated there) and then
binds the directory; the Project is core's, the binding is ours.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import IO

from memriver_core import ProjectNotFound, StorageFailure
from memriver_core.models import project_name

from . import project_context
from .project_context import (
    REGISTRY_DIRNAME,
    REGISTRY_SUFFIX,
    RegisteredProject,
    Registry,
    RegistryInvalid,
    bind,
    count_child_git_markers,
    load_registry,
    resolve,
    resolve_project,
    unbind,
    valid_project_id,
    visible,
)

RESTART_NOTE = ("Restart the affected harness sessions and their memriver MCP servers "
                "for the change to take effect.")
SUBTREE_NOTE = ("Every directory under this root -- including repositories added later -- "
                "will share this project's memories.")
INDEPENDENT_NOTE = "These registered sub-projects stay independent:"
OUT_OF_DATE = "refused: the plan is out of date (a path changed while waiting); run the command again"
STORE_FAILURE = "refused: could not complete the registry write; inspect with memriver project explain"
CANNOT_VERIFY = "refused: could not verify the directory's relation to {what}"
GLOBAL_REFUSAL = "refused: the global project cannot be bound to a directory"


def _service(store: Path):
    """The core facade over this store. Building it reads settings, never the disk."""
    from memriver_core.bootstrap import build_service
    from memriver_core.config import Settings

    try:
        return build_service(Settings(root=store), root=store)
    except Exception as err:   # a bad MEMRIVER_* value: reported as a store failure
        raise StorageFailure from err


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
        return f"refused: {visible(str(target))} is not an existing directory"
    if not canonical.is_dir():
        return f"refused: {visible(str(canonical))} is not a directory"
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
        return (f"refused: the memory store {visible(str(store))} lies inside this directory; "
                "the registry would end up inside the project")
    verdict = project_context.covers(store, canonical)
    if verdict is None:
        return CANNOT_VERIFY.format(what="the memory store")
    if verdict:
        return f"refused: {visible(str(canonical))} lies inside the memory store {visible(str(store))}"
    return None


def _plan_still_valid(canonical: Path, store: Path, *, root: Path | None, home: Path) -> bool:
    """After the prompt: same target, still a directory, same store, and still allowed."""
    try:
        unchanged = (canonical.resolve(strict=True) == canonical and canonical.is_dir()
                     and _store_root(root, home) == store)
    except (OSError, RuntimeError, ValueError):
        return False
    return unchanged and _refuse_target(canonical, home=home, store=store) is None


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


def _independent_lines(registry: Registry, canonical: Path, project_id: str | None) -> str | None:
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
    return (f"  {INDEPENDENT_NOTE}\n"
            + "".join(f"    {pid}: {visible(r)}\n" for pid, r in nested))


def _check_target(canonical: Path, store: Path, project_id: str | None, *, home: Path,
                  stdout: IO[str]) -> tuple[RegisteredProject | None, str] | int:
    """The pre-prompt checks shared by init and adopt.

    Returns (the project's registry entry or None, the independent-projects
    block), or an exit code: 2 for a refusal, 0 when the directory is already
    bound to this project and there is nothing to do.
    """
    refusal = _refuse_target(canonical, home=home, store=store)
    if refusal:
        stdout.write(refusal + "\n")
        return 2
    try:
        registry = load_registry(store)
    except RegistryInvalid as err:
        stdout.write(f"refused: the registry is invalid ({visible(err.location)}: "
                     f"{visible(err.reason)}); run memriver project explain\n")
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
                stdout.write(f"refused: {visible(str(canonical))} is already bound to project {other.id}\n")
                return 2
    if existing is not None:
        for r in existing.roots:
            same = _same(canonical, r)
            if same is None:
                stdout.write(CANNOT_VERIFY.format(what="registered roots") + "\n")
                return 2
            if same:
                stdout.write(f"{visible(str(canonical))} is already bound to {project_id}; nothing to do\n")
                return 0
    independent = _independent_lines(registry, canonical, project_id)
    if independent is None:
        stdout.write(CANNOT_VERIFY.format(what="registered roots") + "\n")
        return 2
    return existing, independent


def _bind_refusal(err: Exception) -> str:
    if isinstance(err, StorageFailure):
        return "the store could not be written"
    if isinstance(err, RegistryInvalid):
        return f"the registry is invalid ({visible(err.location)}: {visible(err.reason)})"
    return visible(str(err))


def run_init(directory: Path | None, *, name: str | None, root: Path | None, yes: bool,
             stdin_is_tty: bool, input_fn, stdout, cwd: Path, home: Path) -> int:
    store = _store_root(root, home)
    canonical = _canonical_target(directory, cwd)
    if isinstance(canonical, str):
        stdout.write(canonical + "\n")
        return 2
    try:
        display = project_name(name if name is not None else canonical.name)
    except ValueError as err:
        stdout.write(f"refused: {err}\n")
        return 2
    checked = _check_target(canonical, store, None, home=home, stdout=stdout)
    if isinstance(checked, int):
        return checked
    _, independent = checked
    plan = (f"memriver project init\n"
            f"  store:   {visible(str(store))}\n"
            f"  project: {visible(display)}  (new; id assigned when written)\n"
            f"  root:    {visible(str(canonical))}\n"
            f"  {SUBTREE_NOTE}\n"
            f"  {_git_count_line(canonical)}\n"
            f"{independent}")
    code = _confirm(plan, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    # the prompt was a window: re-check the paths the plan showed before
    # anything is created (store_lock makes directories on entry)
    if not _plan_still_valid(canonical, store, root=root, home=home):
        stdout.write(OUT_OF_DATE + "\n")
        return 2
    try:
        service = _service(store)
        # bind needs the global id; a Project must not be created when
        # binding is already known to fail
        service.global_project_id()
        project = service.create_project(display)
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    try:
        bind(store, service, project.id, str(canonical))
    except (ValueError, RegistryInvalid, StorageFailure) as err:
        # an empty Project is a legal state and there is no delete action:
        # say exactly what exists and how to finish
        stdout.write(f"refused: project {project.id} was created but {visible(str(canonical))} "
                     f"could not be bound ({_bind_refusal(err)}); run memriver project adopt "
                     f"{project.id} {visible(str(canonical))}\n")
        return 2
    stdout.write(f"created project {visible(project.name)} [{project.id}] and bound "
                 f"{visible(str(canonical))}\n{RESTART_NOTE}\n")
    return 0


def _no_such_project(raw: str, stdout) -> int:
    stdout.write(f"no such project: {visible(raw[:255])}\n")
    return 2


def run_adopt(project_id: str, directory: Path | None, *, root: Path | None, yes: bool,
              stdin_is_tty: bool, input_fn, stdout, cwd: Path, home: Path) -> int:
    store = _store_root(root, home)
    if not valid_project_id(project_id):
        return _no_such_project(project_id, stdout)
    try:
        service = _service(store)
        project = service.read_project(project_id)
        is_global = project_id == service.global_project_id()
    except ProjectNotFound:
        return _no_such_project(project_id, stdout)
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    if is_global:
        stdout.write(GLOBAL_REFUSAL + "\n")
        return 2
    canonical = _canonical_target(directory, cwd)
    if isinstance(canonical, str):
        stdout.write(canonical + "\n")
        return 2
    checked = _check_target(canonical, store, project_id, home=home, stdout=stdout)
    if isinstance(checked, int):
        return checked
    existing, independent = checked
    roots_line = ("" if existing is None or not existing.roots
                  else "  roots:   " + ", ".join(visible(r) for r in existing.roots) + "\n")
    plan = (f"memriver project adopt\n"
            f"  store:   {visible(str(store))}\n"
            f"  project: {visible(project.name)} [{project.id}]  (existing)\n"
            f"{roots_line}"
            f"  root:    {visible(str(canonical))}\n"
            f"  {SUBTREE_NOTE}\n"
            f"  {_git_count_line(canonical)}\n"
            f"{independent}")
    code = _confirm(plan, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    if not _plan_still_valid(canonical, store, root=root, home=home):
        stdout.write(OUT_OF_DATE + "\n")
        return 2
    try:
        bind(store, service, project_id, str(canonical))
    except ValueError as err:
        stdout.write(f"refused: {visible(str(err))}\n")
        return 2
    except RegistryInvalid as err:
        stdout.write(f"refused: the registry is invalid ({visible(err.location)}: "
                     f"{visible(err.reason)})\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    stdout.write(f"bound {visible(str(canonical))} to {project_id}\n{RESTART_NOTE}\n")
    return 0


def run_unbind(project_id: str, directory: Path, *, root: Path | None, yes: bool,
               stdin_is_tty: bool, input_fn, stdout, cwd: Path, home: Path) -> int:
    store_root = _store_root(root, home)
    if not valid_project_id(project_id):
        return _no_such_project(project_id, stdout)
    literal = str(Path(directory) if Path(directory).is_absolute() else (cwd / directory))
    try:
        registry = load_registry(store_root)
    except RegistryInvalid as err:
        # a registry that fails validation cannot be edited through this command
        # (the lock-time reload would refuse it too); the recovery is manual, and
        # it targets the project the user named -- err.location is where the
        # clash was noticed, which for a duplicate root is usually the other one
        invalid = f"refused: the registry is invalid ({visible(err.location)}: {visible(err.reason)})"
        if err.reason == "root is already bound to another project":
            path = store_root / REGISTRY_DIRNAME / f"{project_id}{REGISTRY_SUFFIX}"
            stdout.write(f"{invalid}; edit {visible(str(path))} by hand and remove the root "
                         f"{visible(literal)}, then run memriver project explain\n")
        else:
            stdout.write(f"{invalid}; fix that file by hand, then run memriver project explain\n")
        return 2
    # existence here is the registry's: a registry file whose Project is gone
    # from the store must still be removable
    project = next((p for p in registry.projects if p.id == project_id), None)
    if project is None:
        return _no_such_project(project_id, stdout)
    roots = project.roots
    key = literal if literal in roots else None
    if key is None:
        try:
            resolved = str(Path(literal).resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            resolved = None
        key = resolved if resolved in roots else None
    if key is None:
        stdout.write(f"refused: {visible(literal)} is not bound to {project_id}\n")
        return 2
    after = tuple(r for r in roots if r != key)
    next_registry = Registry(tuple(RegisteredProject(project_id, after) if p.id == project_id else p
                                   for p in registry.projects))
    afterwards = resolve_project(next_registry, cwd)
    afterwards_line = (f"registered {afterwards.project_id}" if afterwards.state == "registered"
                       else afterwards.state)
    plan = (f"memriver project unbind\n"
            f"  store:   {visible(str(store_root))}\n"
            f"  project: {project_id}\n"
            f"  roots before: {', '.join(visible(r) for r in roots)}\n"
            f"  roots after:  {', '.join(visible(r) for r in after) or '(none)'}\n"
            f"  the current directory resolves afterwards: {afterwards_line}\n")
    code = _confirm(plan, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    if _store_root(root, home) != store_root:
        stdout.write(OUT_OF_DATE + "\n")
        return 2
    try:
        unbind(store_root, project_id, key)
    except (ValueError, RegistryInvalid) as err:
        stdout.write(f"refused: {visible(str(err))}\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    stdout.write(f"unbound {visible(key)} from {project_id}\n{RESTART_NOTE}\n")
    return 0


def run_explain(*, root: Path | None, project_dir: Path | None, stdout, cwd: Path,
                home: Path) -> int:
    """What a directory resolves to and what a session there may actually read and write.

    The rights come from the same `open_session` the server and the hook use,
    so an invalid manifest or a missing project is reported here exactly as it
    would degrade a session, and global is only named when it exists.
    """
    from .session import Session, open_session

    store_root = _store_root(root, home)
    start = Path(project_dir) if project_dir is not None else cwd
    resolution = resolve(store_root, start)
    try:
        canonical = str(start.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        canonical = str(start)
    try:
        session = open_session(_service(store_root), resolution)
    except StorageFailure:          # settings could not even be built
        from memriver_core.models import AccessContext

        session = Session("", AccessContext(project_id=None, global_project_id=None),
                          "unavailable")
    lines = [f"store: {visible(str(store_root))}", f"cwd: {visible(canonical)}",
             f"state: {resolution.state}"]
    code = 0
    if resolution.state == "degraded":
        lines.append(f"diagnostic: {visible(resolution.diagnostic or '')}")
        code = 1
    if session.state == "registered" and session.project is not None:
        lines += [f"project: {session.project.id}", f"name: {visible(session.project.name)}",
                  f"root: {visible(resolution.root or '')}"]
    elif session.state == "missing":
        lines += [f"project: {resolution.project_id}",
                  "diagnostic: this registered project does not exist in the store"]
        code = 1
    elif session.state == "unavailable":
        if resolution.project_id:
            lines.append(f"project: {resolution.project_id}")
        lines.append("diagnostic: the memory store could not be read")
        code = 1
    ctx = session.ctx
    reads = [ctx.project_id] if ctx.project_id else []
    if ctx.global_project_id:
        reads.append("global")
    writes = sorted(ctx.writable())
    lines += [f"reads: {', '.join(reads) or 'none'}", f"writes: {', '.join(writes) or 'none'}"]
    stdout.write("\n".join(lines) + "\n")
    return code
