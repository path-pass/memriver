"""Human management of memories -- history, restore, undo, delete.

None of this is reachable over MCP. Every rule and every write is the core's (the
services ``memriver_core.bootstrap`` composes); this module shows plans, asks, and
words refusals -- nothing else. When a write is refused because the store moved on
while the prompt was open, a person at the prompt is shown the new state and asked
again; under ``--yes`` or ``--confirm`` the command fails instead (exit 2), because
nobody looked at the new state.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path
from typing import IO

from memriver_core import (
    BatchConflict,
    ContentRejected,
    GlobalReadOnly,
    MemoryNotFound,
    PlanChanged,
    ProjectUnavailable,
    StorageFailure,
    UndoRefused,
    VersionConflict,
)
from memriver_core.models import (
    Change,
    HardDeletePlan,
    MemoryVersion,
    SoftDelete,
    Step,
    single_line,
)

from .project_context import visible
from .views import STORE_UNREADABLE, _body, _cue

STORE_UNWRITABLE = "refused: the memory store could not be written"
STORE_MOVED = "refused: the memory store changed while it was read; run the command again"


def _services(root: Path | None, home: Path):
    """The core services. An unusable settings.toml or MEMRIVER_* value raises
    SettingsError (cli.main names the file and the field); any other failure building
    them is StorageFailure. Building reads settings, never the store."""
    from memriver_core.bootstrap import build_services
    from memriver_core.settings import SettingsError, load_settings

    try:
        settings = load_settings(root_override=root)
        return build_services(settings, root=settings.root, home=home)
    except SettingsError:
        raise
    except Exception as err:   # never shown: its text may carry a path
        raise StorageFailure from err


def _confirm(*, yes: bool, stdin_is_tty: bool, input_fn: Callable[[str], str],
             stdout: IO[str]) -> int | None:
    """None to go ahead; else the exit code: 2 without a terminal, 1 when declined."""
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
        stdout.write("aborted; nothing was changed\n")
        return 1
    return None


def _where(project_id: str, global_id: str | None) -> str:
    return "global" if project_id == global_id else f"project {project_id}"


def _no_such_memory(memory_id: str, stdout: IO[str]) -> int:
    stdout.write(f"no such memory: {visible(memory_id[:255])}\n")
    return 2


# --- history -----------------------------------------------------------------

def _origin(version: MemoryVersion) -> str:
    """Who made a version: its change, or "imported" for one with no recorded change
    (a row carried over from an older store)."""
    change = version.change
    if change is None:
        return "imported"
    via = f" ({visible(change.changed_via)})" if change.changed_via else ""
    return f"{visible(change.at)}  {visible(change.changed_by)}{via}  change {change.change_id}"


def _sources(version: MemoryVersion) -> str:
    return ", ".join(f"{visible(source.memory_id)} v{source.version}"
                     for source in version.sources)


def _version_line(version: MemoryVersion) -> str:
    deleted = "  [deleted]" if version.deleted else ""
    line = (f"v{version.version}  {_origin(version)}{deleted}  "
            f"{visible(single_line(version.description))}\n")
    sources = _sources(version)
    return line + (f"    sources: {sources}\n" if sources else "")


def _version_in_full(version: MemoryVersion) -> str:
    lines = [f"id: {version.memory_id}", f"version: {version.version}",
             f"type: {version.type}", f"trust: {version.trust}",
             f"sync: {str(version.sync).lower()}", f"deleted: {str(version.deleted).lower()}",
             f"change: {_origin(version)}", f"sources: {_sources(version) or '-'}",
             f"description: {visible(single_line(version.description))}"]
    return "\n".join(lines) + "\n---\n" + _body(version.body) + "\n"


def _versions(services, memory_id: str) -> list[MemoryVersion]:
    """Every version of a memory, oldest first; empty for an unknown id."""
    try:
        return sorted(services.memory.versions(memory_id), key=lambda v: v.version)
    except MemoryNotFound:
        return []


def run_history(memory_id: str, *, show: int | None, root: Path | None, stdout: IO[str],
                home: Path) -> int:
    try:
        versions = _versions(_services(root, home), memory_id)
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    if not versions:
        return _no_such_memory(memory_id, stdout)
    if show is None:
        stdout.write("".join(_version_line(version) for version in versions))
        return 0
    chosen = next((version for version in versions if version.version == show), None)
    if chosen is None:
        stdout.write(f"no such version: {versions[0].memory_id} v{show}\n")
        return 2
    stdout.write(_version_in_full(chosen))
    return 0


# --- restore -----------------------------------------------------------------

def _state(version: MemoryVersion) -> str:
    return _cue(version) + ("  [deleted]" if version.deleted else "")


def _restore_plan(current: MemoryVersion, target: MemoryVersion) -> str:
    changes = [name for name, differs in (
        ("content", (current.description, current.body) != (target.description, target.body)),
        ("sources", set(current.sources) != set(target.sources)),
        ("deleted state", current.deleted != target.deleted)) if differs]
    return (f"memriver restore: {current.memory_id} from v{current.version} to the state of "
            f"v{target.version}\n"
            f"  now (v{current.version}): {_state(current)}\n"
            f"  target (v{target.version}): {_state(target)}\n"
            f"  changes: {', '.join(changes) or 'nothing'}\n")


_RESTORE_CONFLICTS = {
    "version": ("refused (version): {memory_id} changed while waiting; nothing was changed; "
                "run the command again"),
    "same-state": ("refused (same-state): the state of v{to_version} is already the current "
                   "one; nothing was changed"),
}


def run_restore(memory_id: str, *, to_version: int, yes: bool, root: Path | None,
                stdin_is_tty: bool, input_fn: Callable[[str], str], stdout: IO[str],
                home: Path) -> int:
    while True:
        try:
            services = _services(root, home)
            versions = _versions(services, memory_id)
        except StorageFailure:
            stdout.write(STORE_UNREADABLE + "\n")
            return 2
        if not versions:
            return _no_such_memory(memory_id, stdout)
        current = versions[-1]
        target = next((version for version in versions if version.version == to_version), None)
        if target is None:
            stdout.write(f"no such version: {current.memory_id} v{to_version}\n")
            return 2
        stdout.write(_restore_plan(current, target))
        refused = _confirm(yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
        if refused is not None:
            return refused
        try:
            change = services.memory.restore(current.memory_id, to_version,
                                             expected_version=current.version,
                                             changed_by="human")
        except BatchConflict as err:
            if err.reason == "version" and not yes:
                stdout.write(f"{current.memory_id} changed while waiting; nothing was changed "
                             "yet\n")
                continue
            template = _RESTORE_CONFLICTS.get(err.reason, "refused ({reason}): nothing was "
                                                          "changed")
            stdout.write(template.format(memory_id=current.memory_id, to_version=to_version,
                                         reason=err.reason) + "\n")
            return 2
        except ContentRejected as err:
            stdout.write(f"refused (content policy): the state of v{to_version} fails rule "
                         f"{err.rule_id}; nothing was changed\n")
            return 2
        except MemoryNotFound:
            return _no_such_memory(memory_id, stdout)
        except StorageFailure:
            stdout.write(STORE_UNWRITABLE + "\n")
            return 2
        stdout.write(f"restored {current.memory_id} to the state of v{to_version} as "
                     f"v{change.steps[0].after_version} (change {change.change_id})\n")
        return 0


# --- undo --------------------------------------------------------------------

_UNDO_REFUSALS = {
    "not-found": "refused (not-found): no change {change_id}",
    "hard-deleted": ("refused (hard-deleted): a hard delete removed part of change "
                     "{change_id}; it cannot be undone"),
    "changed": ("refused (changed): {memory_ids} changed after change {change_id}; nothing "
                "was changed; use memriver history and memriver restore"),
}


def _step_line(step: Step, memory, global_id: str | None) -> str:
    before = "" if step.before_version is None else f"v{step.before_version}->"
    return (f"  {step.memory_id}  {_where(memory.project_id, global_id)}  "
            f"{step.op} {before}v{step.after_version}  {_cue(memory)}\n")


def _inverse_line(step: Step) -> str:
    if step.op == "create":
        return f"  {step.memory_id}: soft delete\n"
    return f"  {step.memory_id}: restore the state of v{step.before_version}\n"


def _change_plan(change: Change, memories: dict, global_id: str | None) -> str:
    via = f" ({visible(change.changed_via)})" if change.changed_via else ""
    undoes = f", an undo of {change.undoes}" if change.undoes else ""
    text = (f"memriver undo: change {change.change_id} at {visible(change.at)} by "
            f"{visible(change.changed_by)}{via}{undoes}\n")
    text += "".join(_step_line(step, memories[step.memory_id], global_id)
                    for step in change.steps)
    missing = change.step_count - len(change.steps)
    if missing:
        text += (f"  ({missing} more {'step' if missing == 1 else 'steps'} removed by a "
                 "hard delete)\n")
    return text + "the undo:\n" + "".join(_inverse_line(step) for step in change.steps)


def run_undo(change_id: str, *, yes: bool, root: Path | None, stdin_is_tty: bool,
             input_fn: Callable[[str], str], stdout: IO[str], home: Path) -> int:
    try:
        services = _services(root, home)
        change = services.memory.change(change_id)
        if change is None:
            stdout.write(_UNDO_REFUSALS["not-found"].format(
                change_id=visible(change_id[:255])) + "\n")
            return 2
        global_id = services.project.global_project_id()
        memories = {step.memory_id: services.memory.show(step.memory_id, include_deleted=True)
                    for step in change.steps}
    except MemoryNotFound:
        stdout.write(STORE_MOVED + "\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    stdout.write(_change_plan(change, memories, global_id))
    refused = _confirm(yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if refused is not None:
        return refused
    try:
        undo = services.memory.undo(change.change_id, changed_by="human")
    except UndoRefused as err:
        template = _UNDO_REFUSALS.get(err.reason, "refused ({reason}): nothing was changed")
        stdout.write(template.format(change_id=change.change_id, reason=err.reason,
                                     memory_ids=", ".join(err.memory_ids)) + "\n")
        return 2
    except ContentRejected as err:
        stdout.write(f"refused (content policy): the undo would restore content failing rule "
                     f"{err.rule_id}; nothing was changed\n")
        return 2
    except BatchConflict as err:
        stdout.write(f"refused ({err.reason}): the undo conflicts with the current state; "
                     "nothing was changed\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_UNWRITABLE + "\n")
        return 2
    stdout.write(f"undone change {change.change_id} by change {undo.change_id}\n")
    return 0


# --- delete ------------------------------------------------------------------

def run_delete(memory_id: str, *, version: int | None, hard: bool, dry_run: bool,
               confirm_code: str | None, yes: bool, root: Path | None, stdin_is_tty: bool,
               input_fn: Callable[[str], str], stdout: IO[str], cwd: Path, home: Path) -> int:
    """`--version` (soft) and `--hard` [`--dry-run` | `--confirm`] are exclusive; the CLI
    enforces spec §8.2's flag matrix before this runs."""
    try:
        services = _services(root, home)
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    ask = {"yes": yes, "stdin_is_tty": stdin_is_tty, "input_fn": input_fn, "stdout": stdout}
    if hard:
        return _hard_delete(services, memory_id, dry_run=dry_run, confirm_code=confirm_code,
                            root=root, **ask)
    return _soft_delete(services, memory_id, version=version, cwd=cwd, **ask)


def _soft_delete(services, memory_id: str, *, version: int, cwd: Path, yes: bool,
                 stdin_is_tty: bool, input_fn: Callable[[str], str], stdout: IO[str]) -> int:
    try:
        project_context = services.project.open_project_context(str(cwd))
        global_id = services.project.global_project_id()
        memory = services.memory.show(memory_id)
    except MemoryNotFound:
        return _no_such_memory(memory_id, stdout)
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    # a global entry is deleted by id as a management delete, wherever this runs; any
    # other entry only from its own project's directory. The in-transaction checks
    # below still decide at write time.
    is_global = global_id is not None and memory.project_id == global_id
    if not is_global and memory.project_id not in project_context.read_write_set.writable():
        # `memriver show` displays it, so "no such memory" would mislead a human
        stdout.write(f"refused: {memory.id} belongs to project {memory.project_id}, not this "
                     "directory's project; run memriver delete from that project's directory\n")
        return 2
    stdout.write(f"memriver delete: {memory.id} [{memory.type}] in "
                 f"{_where(memory.project_id, global_id)}: {_cue(memory)}  (soft)\n")
    refused = _confirm(yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if refused is not None:
        return refused
    try:
        if is_global:
            # a management delete, like restore and undo: the is-global check above is
            # this command's own; apply's version check still decides at write time
            services.memory.apply([SoftDelete(memory.id, version)], changed_by="human")
        else:
            services.memory.delete(memory.id, project_context, expected_version=version,
                                   changed_by="human")
    except MemoryNotFound:
        return _no_such_memory(memory_id, stdout)
    except (GlobalReadOnly, ProjectUnavailable):
        # an ordinary project's entry whose project changed role between the plan and
        # the write (the global path never raises these)
        stdout.write("refused: the memory's project changed while waiting; run the command "
                     "again\n")
        return 2
    except BatchConflict as err:
        if err.reason == "missing":             # hard-deleted meanwhile
            return _no_such_memory(memory_id, stdout)
        stdout.write(f"refused: {memory.id} changed since version {version}; run memriver "
                     f"show {memory.id} and retry\n")
        return 2
    except VersionConflict:
        stdout.write(f"refused: {memory.id} changed since version {version}; run memriver "
                     f"show {memory.id} and retry\n")
        return 2
    except ContentRejected as err:
        stdout.write(f"refused (content policy): {visible(memory.id)} fails rule "
                     f"{visible(err.rule_id)}; remove it with memriver delete "
                     f"{visible(memory.id)} --hard\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_UNWRITABLE + "\n")
        return 2
    stdout.write(f"deleted {memory.id}\n")
    return 0


def _item_cue(services, memory_id: str) -> str:
    try:
        return _cue(services.memory.show(memory_id, include_deleted=True))
    except (MemoryNotFound, StorageFailure):
        return ""       # the plan stays exact; only its cue is missing


def _plan_text(services, plan: HardDeletePlan, global_id: str | None) -> str:
    count = len(plan.items)
    lines = [(f"memriver delete --hard: removes {count} "
              f"{'memory' if count == 1 else 'memories'} with every version, source and read:")]
    for item in plan.items:
        deleted = "  (deleted)" if item.deleted else ""
        lines.append(f"  {visible(item.memory_id)}  {visible(_where(item.project_id, global_id))}"
                     f"  v{item.version}{deleted}  {_item_cue(services, item.memory_id)}")
        lines.extend(f"    because {visible(citation.citing_id)} v{citation.citing_version}"
                     f"{'' if citation.citing_current else ' (history)'} cites "
                     f"{visible(citation.cited_id)} v{citation.cited_version}"
                     for citation in item.citations)
    lines.append("  changes that touched them stay in the log but can no longer be undone")
    return "\n".join(lines) + "\n"


def _confirm_command(plan: HardDeletePlan, root: Path | None) -> str:
    root_option = f" --root {shlex.quote(str(root))}" if root is not None else ""
    return (f"to delete exactly these, run: memriver delete {plan.target} --hard "
            f"--confirm {plan.code}{root_option}\n")


def _purged(memory_ids: list[str], stdout: IO[str]) -> int:
    stdout.write(f"purged {', '.join(memory_ids)}\n")
    return 0


def _hard_delete(services, memory_id: str, *, dry_run: bool, confirm_code: str | None,
                 root: Path | None, yes: bool, stdin_is_tty: bool,
                 input_fn: Callable[[str], str], stdout: IO[str]) -> int:
    try:
        global_id = services.project.global_project_id()
        if confirm_code is not None:
            # the second step: the code names the plan the dry run printed, and the
            # core recomputes and compares it; nothing was stored in between
            try:
                deleted = services.maintenance.hard_delete(memory_id, code=confirm_code)
            except PlanChanged as err:
                stdout.write("refused: the plan changed since it was printed; nothing was "
                             "deleted\n" + _plan_text(services, err.plan, global_id)
                             + _confirm_command(err.plan, root))
                return 2
            return _purged(deleted, stdout)
        plan = services.maintenance.plan_hard_delete(memory_id)
        if dry_run:
            stdout.write(_plan_text(services, plan, global_id) + _confirm_command(plan, root))
            return 0
        while True:
            stdout.write(_plan_text(services, plan, global_id))
            refused = _confirm(yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn,
                               stdout=stdout)
            if refused is not None:
                return refused
            try:
                return _purged(services.maintenance.hard_delete(memory_id,
                                                                expected=plan.expected),
                               stdout)
            except PlanChanged as err:
                if yes:
                    stdout.write("refused: the plan changed while waiting; nothing was "
                                 "deleted; run the command again\n")
                    return 2
                stdout.write("the plan changed while waiting; nothing was deleted yet\n")
                plan = err.plan
    except MemoryNotFound:
        return _no_such_memory(memory_id, stdout)
    except StorageFailure:
        stdout.write("refused: the memory store failed; nothing was deleted\n")
        return 2
