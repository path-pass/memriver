"""``memriver project`` -- plan, confirm and report directory bindings.

Every command shows its plan and asks before writing; ``explain`` writes
nothing. None of them ever creates or modifies a file inside a target
directory. Every rule and every write is the core's (``MemoryService``); this
module owns the plan text, the prompt and the sentences.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import IO

from memriver_core import BindingRefused, ProjectNotFound, StorageFailure
from memriver_core.models import RootPlan, project_name
from memriver_core.settings import PROJECT_NAME_MAX_CHARS

from .project_context import count_child_git_markers, visible

RESTART_NOTE = ("Restart the affected harness sessions and their memriver MCP servers "
                "for the change to take effect.")
SUBTREE_NOTE = ("Every directory under this root -- including repositories added later -- "
                "will share this project's memories.")
INDEPENDENT_NOTE = "These registered sub-projects stay independent:"
OUT_OF_DATE = "refused: the plan is out of date (a path changed while waiting); run the command again"
STORE_FAILURE = "refused: could not complete the store write; inspect with memriver project explain"
CANNOT_VERIFY = ("refused: could not verify the directory's relation to the home directory, "
                 "the memory store or the bound directories")
GLOBAL_REFUSAL = "refused: the global project cannot be bound to a directory"
BINDING_CHANGED = "refused: the binding changed while waiting; nothing was changed; run the command again"


def _service(store: Path, home: Path):
    """The core facade over this store. Building it reads settings, never the disk."""
    from memriver_core.bootstrap import build_service
    from memriver_core.settings import Settings

    try:
        return build_service(Settings(root=store), root=store, home=home)
    except Exception as err:   # a bad MEMRIVER_* value: reported as a store failure
        raise StorageFailure from err


def _configured_root(root: Path | None, home: Path, cwd: Path) -> Path:
    """The store path as configured: absolute, but symlinks NOT resolved.

    The core compares this path's canonical form with the confirmed plan's
    store after the prompt; resolving it here would hide a redirect made
    while the prompt was open.
    """
    from memriver_core.settings import storage_root

    given = Path(root) if root is not None else storage_root(home=home)
    return given if given.is_absolute() else cwd / given


def _display_root(store: Path) -> str:
    """The configured store's canonical path, for display only."""
    try:
        return str(store.resolve())     # non-strict: the store may not exist yet
    except (OSError, RuntimeError, ValueError):
        return str(store)


def _absolute(directory: Path | None, cwd: Path) -> str:
    """A user-given directory against the injected cwd; not resolved (the core does that)."""
    target = Path(directory) if directory is not None else cwd
    return str(target if target.is_absolute() else cwd / target)


def _refusal(err: BindingRefused, *, target: str, store: str) -> str:
    """The sentence for one refusal reason; `target`/`store` are what the user typed or saw."""
    if err.reason == "not-a-directory":
        return f"refused: {visible(target)} is not an existing directory"
    if err.reason == "covers-home":
        return "refused: the filesystem root, the home directory and its ancestors cannot be a project"
    if err.reason == "covers-store":
        return (f"refused: the memory store {visible(store)} lies inside this directory; "
                "the store would end up inside the project")
    if err.reason == "inside-store":
        return f"refused: {visible(target)} lies inside the memory store {visible(store)}"
    if err.reason == "bound-elsewhere":
        return f"refused: {visible(target)} is already bound to project {err.project_id}"
    if err.reason == "unverifiable":
        return CANNOT_VERIFY
    if err.reason == "is-global":
        return GLOBAL_REFUSAL
    if err.reason == "plan-changed":
        return OUT_OF_DATE
    if err.reason == "binding-changed":
        return BINDING_CHANGED
    return "refused: the project could not be bound"   # no-such-project / has-directory: callers word these


def _confirm(plan: str, *, yes: bool, stdin_is_tty: bool,
             input_fn: Callable[[str], str], stdout: IO[str]) -> int | None:
    stdout.write(plan)
    if yes:
        return None
    if not stdin_is_tty:
        stdout.write("refused: stdin is not a terminal; pass --yes to confirm non-interactively\n")
        return 2
    try:
        answer = input_fn("Proceed? [y/N] ")
    except EOFError:        # Ctrl-D at the prompt declines
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        stdout.write("aborted; nothing was written\n")
        return 1
    return None


def _git_count_line(root: str) -> str:
    count = count_child_git_markers(Path(root))
    return ("Direct child directories containing a .git marker: "
            + ("could not be counted" if count is None else str(count)))


def _independent_lines(plan: RootPlan) -> str:
    if not plan.nested:
        return ""
    return (f"  {INDEPENDENT_NOTE}\n"
            + "".join(f"    {p.id}: {visible(p.root or '')}\n" for p in plan.nested))


def _no_such_project(raw: str, stdout) -> int:
    stdout.write(f"no such project: {visible(raw[:255])}\n")
    return 2


def run_init(directory: Path | None, *, name: str | None, root: Path | None, yes: bool,
             stdin_is_tty: bool, input_fn, stdout, cwd: Path, home: Path) -> int:
    store = _configured_root(root, home, cwd)
    target = _absolute(directory, cwd)
    try:
        service = _service(store, home)
        plan = service.plan_root(target)
    except BindingRefused as err:
        stdout.write(_refusal(err, target=target, store=_display_root(store)) + "\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    try:
        display = project_name(name if name is not None else Path(plan.root).name,
                               PROJECT_NAME_MAX_CHARS)
    except ValueError as err:
        stdout.write(f"refused: {err}\n")
        return 2
    text = (f"memriver project init\n"
            f"  store:   {visible(plan.store)}\n"
            f"  project: {visible(display)}  (new; id assigned when written)\n"
            f"  root:    {visible(plan.root)}\n"
            f"  {SUBTREE_NOTE}\n"
            f"  {_git_count_line(plan.root)}\n"
            f"{_independent_lines(plan)}")
    code = _confirm(text, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    try:
        # one transaction: the project and its directory, under the confirmed plan
        project = service.init_project(display, plan)
    except BindingRefused as err:
        stdout.write(_refusal(err, target=plan.root, store=plan.store) + "\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    stdout.write(f"created project {visible(project.name)} [{project.id}] and bound "
                 f"{visible(plan.root)}\n{RESTART_NOTE}\n")
    return 0


def run_adopt(project_id: str, directory: Path | None, *, root: Path | None, yes: bool,
              stdin_is_tty: bool, input_fn, stdout, cwd: Path, home: Path) -> int:
    store = _configured_root(root, home, cwd)
    target = _absolute(directory, cwd)
    try:
        service = _service(store, home)
        plan = service.plan_root(target, project_id)
        project = service.read_project(project_id)
    except BindingRefused as err:
        if err.reason == "no-such-project":
            return _no_such_project(project_id, stdout)
        if err.reason == "has-directory":
            try:        # a second read: the project may be gone or the store broken by now
                current = service.read_project(project_id).root or ""
            except ProjectNotFound:
                return _no_such_project(project_id, stdout)
            except StorageFailure:
                stdout.write(STORE_FAILURE + "\n")
                return 2
            stdout.write(f"refused: project {project_id} already has a directory "
                         f"({visible(current)}); unbind it first\n")
            return 2
        stdout.write(_refusal(err, target=target, store=_display_root(store)) + "\n")
        return 2
    except ProjectNotFound:
        return _no_such_project(project_id, stdout)
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    if plan.already_bound:
        stdout.write(f"{visible(plan.root)} is already bound to {project_id}; nothing to do\n")
        return 0
    text = (f"memriver project adopt\n"
            f"  store:   {visible(plan.store)}\n"
            f"  project: {visible(project.name)} [{project.id}]  (existing)\n"
            f"  root:    {visible(plan.root)}\n"
            f"  {SUBTREE_NOTE}\n"
            f"  {_git_count_line(plan.root)}\n"
            f"{_independent_lines(plan)}")
    code = _confirm(text, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    try:
        service.adopt(project_id, plan)
    except BindingRefused as err:
        if err.reason == "no-such-project":
            return _no_such_project(project_id, stdout)
        if err.reason == "has-directory":
            stdout.write(f"refused: project {project_id} already has a directory; "
                         "unbind it first\n")
            return 2
        stdout.write(_refusal(err, target=plan.root, store=plan.store) + "\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    stdout.write(f"bound {visible(plan.root)} to {project_id}\n{RESTART_NOTE}\n")
    return 0


def run_unbind(project_id: str, directory: Path, *, root: Path | None, yes: bool,
               stdin_is_tty: bool, input_fn, stdout, cwd: Path, home: Path) -> int:
    store = _configured_root(root, home, cwd)
    literal = _absolute(directory, cwd)
    try:
        service = _service(store, home)
        plan, afterwards = service.plan_unbind(project_id, literal, str(cwd))
    except BindingRefused as err:
        if err.reason == "no-such-project":
            return _no_such_project(project_id, stdout)
        if err.reason == "binding-changed":
            stdout.write(f"refused: {visible(literal)} is not bound to {project_id}\n")
            return 2
        stdout.write(_refusal(err, target=literal, store=_display_root(store)) + "\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    afterwards_line = (f"registered {afterwards.project.id}"
                       if afterwards.state == "registered" and afterwards.project
                       else afterwards.state)
    text = (f"memriver project unbind\n"
            f"  store:   {visible(plan.store)}\n"
            f"  project: {project_id}\n"
            f"  directory before: {visible(plan.root)}\n"
            f"  directory after:  (none)\n"
            f"  the current directory resolves afterwards: {afterwards_line}\n")
    code = _confirm(text, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    try:
        service.unbind(plan)        # exactly the pair the confirmation showed
    except BindingRefused as err:
        stdout.write(_refusal(err, target=plan.root, store=plan.store) + "\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_FAILURE + "\n")
        return 2
    stdout.write(f"unbound {visible(plan.root)} from {project_id}\n{RESTART_NOTE}\n")
    return 0


def run_explain(*, root: Path | None, project_dir: Path | None, stdout, cwd: Path,
                home: Path) -> int:
    """What a directory resolves to and what its project context may actually read and write.

    The rights come from the same ``open_project_context`` the server and the
    hook use, so a damaged store is reported here exactly as it would degrade
    a project context.
    """
    store_root = _configured_root(root, home, cwd)
    start = Path(_absolute(project_dir, cwd))
    try:
        canonical = str(start.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        canonical = str(start)
    try:
        project_context = _service(store_root, home).open_project_context(str(start))
    except StorageFailure:          # settings could not even be built
        from memriver_core.models import ProjectContext, ReadWriteSet

        project_context = ProjectContext("unavailable", "", ReadWriteSet(project_id=None,
                                                                         global_project_id=None))
    lines = [f"store: {visible(_display_root(store_root))}", f"cwd: {visible(canonical)}",
             f"state: {project_context.state}"]
    code = 0
    if project_context.state == "degraded":
        lines.append(f"diagnostic: {visible(project_context.diagnostic or '')}")
        code = 1
    elif project_context.state == "registered" and project_context.project is not None:
        lines += [f"project: {project_context.project.id}",
                  f"name: {visible(project_context.project.name)}",
                  f"root: {visible(project_context.project.root or '')}"]
    elif project_context.state == "unavailable":
        lines.append("diagnostic: the memory store could not be read")
        code = 1
    read_write_set = project_context.read_write_set
    reads = [read_write_set.project_id] if read_write_set.project_id else []
    if read_write_set.global_project_id:
        reads.append("global")
    writes = sorted(read_write_set.writable())
    lines += [f"reads: {', '.join(reads) or 'none'}", f"writes: {', '.join(writes) or 'none'}"]
    stdout.write("\n".join(lines) + "\n")
    return code
