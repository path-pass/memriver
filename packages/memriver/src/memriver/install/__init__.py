"""``memriver install``: plan, render, confirm, and apply harness config edits.

The whole module is built around one promise: **nothing is written until every
structural check has passed and the user has said yes to each change.**

Planning is a pure pipeline (spec 5.1). It resolves harnesses, reads every
target, runs the format-specific editors over in-memory copies, validates the
complete rendered documents, renders one summary per changed fragment, collects
one confirmation per change, then re-applies only the accepted edits to the
original snapshots and validates again. No directory is created, no backup is
written, no file is touched anywhere in that phase -- a planning failure is a
raised ``PlanningError``, never a pretend change.

Applying is a transaction. Each changed target is backed up to a sibling
``<target>.memriver-backup-<UTC timestamp>`` created exclusively, then replaced
through a same-directory temporary file. That backup **is** the pre-image
(spec 10, DEFERRED-1): if any replacement fails, the run walks its write list
in reverse, copies each backup back over its target and deletes the files it
created. Backups are never removed -- not on success, not on rollback, not
when the rollback itself fails, which is exactly when the user needs them.

This package never imports ``memriver_core`` (enforced by
tests/test_architecture.py): it edits harness config files, it does not touch
the memory store.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TextIO

from . import claude_code, codex, cursor, kiro
from .editors import (
    HARNESS_SETTING_TAKEOVER_NOTICE,
    MARKER_BEGIN,
    MARKER_END,
    TAKEOVER_NOTICE,
    EditOperation,
    EditorKind,
    EditResult,
    PlanningError,
    Snapshot,
    Target,
    apply_edit,
    hook_array_identity_merge,
    hook_group,
    hook_identity,
    json_object_merge,
    marker_block,
    mcp_server_payload,
    operation_label,
    render_change_summary,
    toml_roundtrip,
    validate_document,
)

__all__ = [
    "HARNESSES",
    "HARNESS_SETTING_TAKEOVER_NOTICE",
    "MARKER_BEGIN",
    "MARKER_END",
    "TAKEOVER_NOTICE",
    "EditOperation",
    "EditResult",
    "EditorKind",
    "PlanningError",
    "Snapshot",
    "Target",
    "apply_edit",
    "hook_array_identity_merge",
    "hook_group",
    "hook_identity",
    "json_object_merge",
    "marker_block",
    "mcp_server_payload",
    "operation_label",
    "render_change_summary",
    "run_install",
    "toml_roundtrip",
    "validate_document",
]

# The order is the install order, so reports and rollbacks read the same way
# every run. Cursor and Kiro are last because they are the ones that need a
# project root.
HARNESSES: dict[str, ModuleType] = {
    "claude-code": claude_code,
    "codex": codex,
    "cursor": cursor,
    "kiro": kiro,
}

BACKUP_INFIX = ".memriver-backup-"

# Spec 5.4: Codex needs an out-of-band trust step, and a changed definition
# invalidates the trust the user already gave. Both lines are fixed text.
CODEX_TRUST_NOTE = (
    "Run /hooks in Codex, review the memriver hook definitions, and trust them.\n"
    "If this reinstall changed a hook definition, Codex may require re-trust."
)


def _utc_timestamp() -> str:
    """The backup-name suffix; microseconds so two runs a second apart differ."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


# --- planning ----------------------------------------------------------------


@dataclass(frozen=True)
class _PlannedChange:
    operation: EditOperation
    summary: str


@dataclass(frozen=True)
class _Plan:
    project_root: Path | None
    targets: dict[Path, Target]
    snapshots: dict[Path, Snapshot]
    operations: tuple[EditOperation, ...]
    changes: tuple[_PlannedChange, ...]


def _document_kind(target: Target) -> EditorKind:
    suffix = target.path.suffix
    if suffix == ".toml":
        return "toml-table"
    return "json-object" if suffix == ".json" else "marker-block"


def _resolve_project_root(harnesses: Sequence[str], cwd: Path) -> Path | None:
    """The nearest current-or-ancestor ``.git`` root, only when one is needed."""
    if not any(name in ("cursor", "kiro") for name in harnesses):
        return None
    # imported here so `import memriver.install` stays free of the project /
    # core stack; only the two project-scoped harnesses ever need it
    from memriver.project_context import find_git_root

    return find_git_root(cwd)


def _collect_targets(harnesses: Sequence[str],
                     home: Path, project_root: Path | None,
                     ) -> tuple[dict[Path, Target], dict[str, tuple[Target, ...]]]:
    """Every harness's targets, with incompatible duplicate claims rejected."""
    per_harness: dict[str, tuple[Target, ...]] = {}
    classified: dict[Path, Target] = {}
    for name in harnesses:
        targets = HARNESSES[name].targets(home, project_root)
        per_harness[name] = targets
        for target in targets:
            seen = classified.get(target.path)
            if seen is not None and seen.user_level != target.user_level:
                raise PlanningError(
                    f"{target.path} is claimed as both a user-level and a "
                    "project-level target; memriver will not guess which it is"
                )
            classified.setdefault(target.path, target)
    return classified, per_harness


def _refuse_symlinks(target: Target, root: Path | None) -> None:
    """Refuse the target and every path component below ``root`` that is a link.

    Checking only the leaf would still write through a symlinked ``~/.claude``,
    which lands the file somewhere the user never named. ``root`` itself is not
    checked: a home or project directory reached through a link is the user's
    own arrangement, not something this edit redirects.
    """
    components = [target.path]
    if root is not None and target.path.is_relative_to(root):
        parts = target.path.relative_to(root).parts
        components = [root.joinpath(*parts[:depth]) for depth in range(1, len(parts) + 1)]
    for component in components:
        if component.is_symlink():
            raise PlanningError(
                f"{component} is a symlink; memriver will not write through it "
                f"to {target.path}. Replace it with a regular file or directory "
                "(or remove it) and run install again"
            )


def _read_snapshot(target: Target, root: Path | None) -> Snapshot:
    """Read text and mode for planning only; a symlinked path is refused."""
    path = target.path
    _refuse_symlinks(target, root)
    if not path.exists():
        return Snapshot(target=target, text=None, mode=None)
    if not path.is_file():
        raise PlanningError(f"{path} is not a regular file")
    return Snapshot(target=target, text=path.read_text(encoding="utf-8"),
                    mode=_mode_of(path))


def _rendered(operations: Iterable[EditOperation],
              snapshots: Mapping[Path, Snapshot],
              ) -> tuple[dict[Path, str], dict[str, EditResult]]:
    """Apply operations to in-memory copies, then validate each whole document."""
    texts = {path: snapshot.text or "" for path, snapshot in snapshots.items()}
    results: dict[str, EditResult] = {}
    for operation in operations:
        path = operation.target.path
        result = apply_edit(operation, texts[path])
        texts[path] = result.rendered
        results[operation.id] = result
    for path, text in texts.items():
        if text != (snapshots[path].text or ""):
            validate_document(text, _document_kind(snapshots[path].target))
    return texts, results


def _plan(harnesses: Sequence[str], home: Path, cwd: Path,
          env: Mapping[str, str]) -> _Plan:
    """The complete planning pipeline of spec 5.1 -- pure, no filesystem writes."""
    unknown = [name for name in harnesses if name not in HARNESSES]
    if unknown:
        raise PlanningError(
            f"unknown harness {', '.join(unknown)}; choose from "
            f"{', '.join(HARNESSES)}"
        )
    project_root = _resolve_project_root(harnesses, cwd)
    targets, per_harness = _collect_targets(harnesses, home, project_root)
    roots = {True: home, False: project_root}
    snapshots = {
        path: _read_snapshot(target, roots[target.user_level])
        for path, target in targets.items()
    }
    operations: list[EditOperation] = []
    for name in harnesses:
        harness_snapshots = tuple(
            snapshots[target.path] for target in per_harness[name]
        )
        operations.extend(HARNESSES[name].operations(harness_snapshots, env))
    _, results = _rendered(operations, snapshots)
    changes = tuple(
        _PlannedChange(operation,
                       render_change_summary(operation, results[operation.id], home))
        for operation in operations if results[operation.id].changed
    )
    return _Plan(project_root, targets, snapshots, tuple(operations), changes)


# --- the write transaction ----------------------------------------------------


@dataclass(frozen=True)
class _Write:
    """One completed replacement, and everything rollback needs to undo it."""

    target: Target
    backup: Path | None
    original_mode: int | None


def _mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _umask_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _replace_atomically(path: Path, data: bytes, mode: int,
                        replace_file: Callable[[Path, Path], None]) -> None:
    """Write through a same-directory temporary file, so the swap is atomic."""
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".memriver-")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        temporary.chmod(mode)
        replace_file(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_backup(target: Target, original_mode: int, stamp: str) -> Path:
    """Copy the pre-image to a sibling, refusing to touch an existing backup."""
    backup = target.path.with_name(target.path.name + BACKUP_INFIX + stamp)
    mode = 0o600 if target.user_level else original_mode
    handle = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(handle, "wb") as stream:
        stream.write(target.path.read_bytes())
    backup.chmod(mode)  # O_CREAT masks the mode through the umask; this does not
    return backup


def _write_target(target: Target, text: str, root: Path | None, stamp: str,
                  replace_file: Callable[[Path, Path], None]) -> _Write:
    _refuse_symlinks(target, root)  # re-checked: planning was a moment ago
    original_mode = _mode_of(target.path) if target.path.exists() else None
    target.path.parent.mkdir(parents=True, exist_ok=True)
    backup = (
        _write_backup(target, original_mode, stamp) if original_mode is not None
        else None
    )
    mode = original_mode
    if mode is None:
        mode = 0o600 if target.user_level else _umask_mode()
    _replace_atomically(target.path, text.encode("utf-8"), mode, replace_file)
    return _Write(target=target, backup=backup, original_mode=original_mode)


def _roll_back(writes: Sequence[_Write],
               replace_file: Callable[[Path, Path], None]) -> list[str]:
    """Undo completed writes newest-first. Backups survive every outcome."""
    report: list[str] = []
    for write in reversed(writes):
        path = write.target.path
        try:
            if write.backup is None:
                path.unlink(missing_ok=True)
                report.append(f"removed {path} (this run created it)")
            else:
                _replace_atomically(path, write.backup.read_bytes(),
                                    write.original_mode, replace_file)
                report.append(f"restored {path} from {write.backup}")
        except Exception as error:  # noqa: BLE001 - every outcome gets reported
            report.append(
                f"COULD NOT recover {path}"
                + (f" from {write.backup}" if write.backup else "")
                + f": {error}"
            )
    return report


# --- reporting ----------------------------------------------------------------


def _restore_command(backup: Path, path: Path) -> str:
    return f"cp -p -- {shlex.quote(str(backup))} {shlex.quote(str(path))}"


def _success_report(writes: Sequence[_Write], harnesses: Sequence[str]) -> str:
    lines = ["", "installed:"]
    for write in writes:
        lines.append(f"  {write.target.path}")
        if write.backup is None:
            lines.append("    new file, no backup needed")
            lines.append(f"    to undo: {write.target.rollback_instruction}")
        else:
            lines.append(f"    backup:  {write.backup}")
            lines.append(
                f"    restore: {_restore_command(write.backup, write.target.path)}"
            )
    if "codex" in harnesses:
        lines.extend(["", CODEX_TRUST_NOTE])
    return "\n".join(lines) + "\n"


# --- entry point --------------------------------------------------------------


def run_install(harnesses: Sequence[str], *, yes: bool, dry_run: bool,
                home: Path, cwd: Path, env: Mapping[str, str],
                input_fn: Callable[[str], str], stdout: TextIO,
                replace_file: Callable[[Path, Path], None]) -> int:
    """Plan, confirm, and apply the install; return the process exit code."""
    try:
        plan = _plan(harnesses, home, cwd, env)
    except PlanningError as error:
        stdout.write(f"memriver install: {error}\n")
        return 1

    if not plan.changes:
        stdout.write("memriver install: already up to date, nothing to change.\n")
        return 0

    stdout.write("".join("\n" + change.summary for change in plan.changes))

    if dry_run:
        stdout.write("\ndry run: nothing was written.\n")
        if "codex" in harnesses:
            stdout.write("\n" + CODEX_TRUST_NOTE + "\n")
        return 0

    try:
        accepted = _confirm(plan.changes, yes=yes, input_fn=input_fn, home=home)
    except EOFError:
        stdout.write(
            "\nmemriver install: stdin is not interactive and no answer can be "
            "read; re-run with --yes to accept every change shown above.\n"
        )
        return 1

    if not accepted:
        stdout.write("\nnothing accepted; no file was changed.\n")
        return 0

    try:
        texts, _ = _rendered(accepted, plan.snapshots)
    except PlanningError as error:
        stdout.write(f"\nmemriver install: {error}\n")
        return 1

    roots = {True: home, False: plan.project_root}
    pending = [
        (plan.targets[path], text, roots[plan.targets[path].user_level])
        for path, text in texts.items()
        if text != (plan.snapshots[path].text or "")
    ]
    return _apply(pending, harnesses, stdout=stdout, replace_file=replace_file)


def _confirm(changes: Sequence[_PlannedChange], *, yes: bool,
             input_fn: Callable[[str], str],
             home: Path) -> tuple[EditOperation, ...]:
    """One labelled confirmation per change; ``--yes`` accepts them all.

    The label names the harness and the file, because ``--all`` asks the same
    question four times over four different targets.
    """
    if yes:
        return tuple(change.operation for change in changes)
    accepted = []
    for change in changes:
        answer = input_fn(f"apply: {operation_label(change.operation, home)}? [y/N] ")
        if answer.strip().lower() in ("y", "yes"):
            accepted.append(change.operation)
    return tuple(accepted)


def _apply(pending: Sequence[tuple[Target, str, Path | None]],
           harnesses: Sequence[str], *, stdout: TextIO,
           replace_file: Callable[[Path, Path], None]) -> int:
    stamp = _utc_timestamp()
    writes: list[_Write] = []
    try:
        for target, text, root in pending:
            writes.append(_write_target(target, text, root, stamp, replace_file))
    except BaseException as error:  # a Ctrl-C between replacements rolls back too
        stdout.write(f"\nmemriver install failed: {error}\n")
        stdout.write("".join(
            f"  {line}\n" for line in _roll_back(writes, replace_file)))
        stdout.write("  backups were kept; no backup is ever deleted.\n")
        if not isinstance(error, Exception):
            raise  # KeyboardInterrupt / SystemExit: rolled back, never swallowed
        return 1
    stdout.write(_success_report(writes, harnesses))
    return 0
