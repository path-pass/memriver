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

Applying harness configuration is a transaction. Each target is re-read and
compared to its planning snapshot immediately before it is written, so a config
that another agent changed while the user was answering prompts aborts the run
instead of being overwritten from a stale render. Each changed target is backed
up to a sibling ``<target>.memriver-backup-<UTC timestamp>`` created
exclusively, then replaced through a same-directory temporary file. That backup
**is** the pre-image (spec 10, DEFERRED-1): if any replacement fails, the run
walks its write list in reverse, copies each backup back over its target and
deletes the files it created. Backups are never removed -- not on success, not
on rollback, not when the rollback itself fails, which is exactly when the user
needs them.

This package never imports memriver_core (enforced by tests/test_architecture.py).
The CLI may supply a StoreStep callback for global initialization. After all
accepted edits are rendered and validated, that callback runs before harness
configuration is written. Store initialization is not part of the harness-file
rollback: a later harness failure may leave a valid empty global project.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

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
    RemovalOperation,
    Snapshot,
    Target,
    apply_edit,
    apply_removal,
    display_path,
    hook_array_identity_merge,
    hook_array_identity_remove,
    hook_group,
    hook_identity,
    json_object_merge,
    json_object_remove,
    marker_block,
    marker_block_remove,
    mcp_server_payload,
    operation_label,
    render_change_summary,
    render_removal_summary,
    toml_roundtrip,
    toml_table_remove,
    validate_document,
)


@dataclass(frozen=True)
class StoreStep:
    """The memory store's pending initialization, planned and confirmed with the rest.

    Built by the CLI (this package never imports memriver_core). `apply`
    creates the global project and returns the line to print. It runs before
    every harness change and gates them: while the store is not ready, no new
    harness configuration is applied.
    """

    summary: str
    label: str
    apply: Callable[[], str]


STORE_NEEDS_A_TERMINAL = ("\nmemriver install: the memory store needs initializing and stdin "
                          "is not a terminal; re-run with --yes. No file was changed.\n")
STORE_DECLINED = ("\ninstallation cancelled; memory store not initialized; no file was changed.\n")
STORE_FAILED = ("\nmemriver install: the memory store could not be initialized; no harness "
                "configuration was applied. Run memriver doctor.\n")

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
    "RemovalOperation",
    "Snapshot",
    "StoreStep",
    "Target",
    "apply_edit",
    "apply_removal",
    "display_path",
    "hook_array_identity_merge",
    "hook_array_identity_remove",
    "hook_group",
    "hook_identity",
    "json_object_merge",
    "json_object_remove",
    "marker_block",
    "marker_block_remove",
    "mcp_server_payload",
    "operation_label",
    "render_change_summary",
    "render_removal_summary",
    "run_config_uninstall",
    "run_install",
    "toml_roundtrip",
    "toml_table_remove",
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

MISSING_UVX_NOTE = (
    "The hook and MCP entries memriver configures invoke 'uvx memriver', and "
    "uvx was not found on PATH.\n"
    "Each harness resolves that command itself at startup, so install uv "
    "(https://docs.astral.sh/uv/) -- or make uvx reachable from the "
    "environment the harness starts in -- before relying on these edits."
)


def _utc_timestamp() -> str:
    """The backup-name suffix; microseconds so two runs a second apart differ."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


# --- planning ----------------------------------------------------------------


@dataclass(frozen=True)
class _PlannedChange:
    operation: EditOperation | RemovalOperation
    summary: str


@dataclass(frozen=True)
class _Plan:
    project_root: Path | None
    targets: dict[Path, Target]
    snapshots: dict[Path, Snapshot]
    operations: tuple[EditOperation | RemovalOperation, ...]
    changes: tuple[_PlannedChange, ...]
    # what each harness planner read, kept for the completion notes: those are
    # composed after the confirmations, because what a run leaves behind is
    # the planning snapshot plus the answers, not the snapshot alone
    harness_snapshots: dict[str, tuple[Snapshot, ...]]
    env: Mapping[str, str]


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
                     home: Path, project_root: Path | None, command_name: str,
                     ) -> tuple[dict[Path, Target], dict[str, tuple[Target, ...]]]:
    """Every harness's targets, with incompatible duplicate claims rejected."""
    per_harness: dict[str, tuple[Target, ...]] = {}
    classified: dict[Path, Target] = {}
    for name in harnesses:
        targets = HARNESSES[name].targets(home, project_root, command_name)
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


def _symlinked_component(target: Target, root: Path | None) -> Path | None:
    """The first path component at or below ``target.path`` that is a link,
    checked the same way ``_refuse_symlinks`` does -- ``root`` itself is
    exempt, everything below it (including the leaf) is checked -- or
    ``None`` if none of them is.
    """
    components = [target.path]
    if root is not None and target.path.is_relative_to(root):
        parts = target.path.relative_to(root).parts
        components = [root.joinpath(*parts[:depth]) for depth in range(1, len(parts) + 1)]
    for component in components:
        # `lstat` directly, not `Path.is_symlink()`: on 3.14 that method
        # routes through `os.path.islink`, which swallows every `OSError`
        # (not just "nothing here") and reports False -- silently reading a
        # failed check as "no link found, safe to proceed". A missing
        # component (or one below a non-directory) genuinely has nothing to
        # follow, so that alone is treated as "no link here"; any other
        # OSError (a permission or I/O failure) proves nothing and must
        # propagate to the caller's own unexpected-failure handling.
        try:
            mode = component.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        if stat.S_ISLNK(mode):
            return component
    return None


def _refuse_symlinks(target: Target, root: Path | None, command_name: str) -> None:
    """Refuse the target and every path component below ``root`` that is a link.

    Checking only the leaf would still write through a symlinked ``~/.claude``,
    which lands the file somewhere the user never named. ``root`` itself is not
    checked: a home or project directory reached through a link is the user's
    own arrangement, not something this edit redirects.
    """
    component = _symlinked_component(target, root)
    if component is not None:
        raise PlanningError(
            f"{component} is a symlink; memriver will not write through it "
            f"to {target.path}. Replace it with a regular file or directory "
            f"(or remove it) and run {command_name} again"
        )


def _read_snapshot(target: Target, root: Path | None,
                   command_name: str) -> Snapshot:
    """Read text and mode for planning only; a symlinked path is refused."""
    path = target.path
    _refuse_symlinks(target, root, command_name)
    try:
        if not path.exists():
            return Snapshot(target=target, text=None, mode=None)
        if not path.is_file():
            raise PlanningError(f"{path} is not a regular file")
        # decoded from bytes, never `read_text`: text mode translates CRLF and
        # CR to LF, and a single accepted change then writes the whole
        # translated file back -- rewriting foreign lines outside the managed
        # region that no summary showed and no prompt confirmed. The editors
        # carry the original separators through untouched; the write side is
        # already binary.
        return Snapshot(target=target, text=path.read_bytes().decode("utf-8"),
                        mode=_mode_of(path))
    except (UnicodeError, OSError) as err:
        # the whole read is one boundary, not just the decode: a target that
        # exists but cannot be decoded, opened or stat'ed is a planning
        # failure exactly like one that cannot be parsed. The cause is kept
        # for a debugger; the user gets fixed text, because the underlying
        # message carries the rejected bytes and an errno string.
        raise PlanningError(
            f"{path} could not be read; check that it is UTF-8 text this user "
            f"can read, then run memriver {command_name} again"
        ) from err


def _rendered(operations: Iterable[EditOperation | RemovalOperation],
              snapshots: Mapping[Path, Snapshot],
              apply_fn: Callable[[Any, str], EditResult], command_name: str,
              ) -> tuple[dict[Path, str], dict[str, EditResult]]:
    """Apply operations to in-memory copies, then validate each whole document."""
    texts = {path: snapshot.text or "" for path, snapshot in snapshots.items()}
    results: dict[str, EditResult] = {}
    for operation in operations:
        path = operation.target.path
        result = apply_fn(operation, texts[path])
        texts[path] = result.rendered
        results[operation.id] = result
    for path, text in texts.items():
        if text != (snapshots[path].text or ""):
            validate_document(text, _document_kind(snapshots[path].target),
                              command_name)
    return texts, results


def _plan(harnesses: Sequence[str], home: Path, cwd: Path, env: Mapping[str, str], *,
          collect_operations: Callable[[str, tuple[Snapshot, ...], Mapping[str, str]],
                                       Sequence[Any]],
          apply_fn: Callable[[Any, str], EditResult],
          summary_fn: Callable[[Any, EditResult, Path], str],
          command_name: str) -> _Plan:
    """The complete planning pipeline of spec 5.1 -- pure, no filesystem writes.

    ``collect_operations``/``apply_fn``/``summary_fn`` are the only install-vs-
    uninstall differences: which per-harness function builds the operations,
    which editor runs them, and how a changed one is rendered for confirmation.
    Everything else -- target classification, snapshotting, validation -- is
    the same pipeline either direction runs through.
    """
    unknown = [name for name in harnesses if name not in HARNESSES]
    if unknown:
        raise PlanningError(
            f"unknown harness {', '.join(unknown)}; choose from "
            f"{', '.join(HARNESSES)}"
        )
    project_root = _resolve_project_root(harnesses, cwd)
    targets, per_harness = _collect_targets(harnesses, home, project_root,
                                            command_name)
    roots = {True: home, False: project_root}
    snapshots = {
        path: _read_snapshot(target, roots[target.user_level], command_name)
        for path, target in targets.items()
    }
    harness_snapshots = {
        name: tuple(snapshots[target.path] for target in per_harness[name])
        for name in harnesses
    }
    operations: list[Any] = []
    for name in harnesses:
        operations.extend(collect_operations(name, harness_snapshots[name], env))
    _, results = _rendered(operations, snapshots, apply_fn, command_name)
    changes = tuple(
        _PlannedChange(operation, summary_fn(operation, results[operation.id], home))
        for operation in operations if results[operation.id].changed
    )
    return _Plan(project_root, targets, snapshots, tuple(operations), changes,
                 harness_snapshots, env)


def _install_operations(name: str, snapshots: tuple[Snapshot, ...],
                        env: Mapping[str, str]) -> Sequence[EditOperation]:
    return HARNESSES[name].operations(snapshots, env)


def _uninstall_operations(name: str, snapshots: tuple[Snapshot, ...],
                          env: Mapping[str, str]) -> Sequence[RemovalOperation]:
    return HARNESSES[name].uninstall_operations(snapshots, env)


# --- the write transaction ----------------------------------------------------


@dataclass(frozen=True)
class _CreatedDir:
    """A directory ``_make_dirs`` created, identified rather than just named.

    ``mkdir`` creating the directory is the ownership claim (see
    ``_make_dirs``), but the path alone stops proving that claim the moment
    another actor deletes and recreates it: a directory is a path plus an
    inode, and the pair narrows -- it does not eliminate -- the chance that
    rollback removes a directory this run did not make. ``st_dev`` travels
    with ``st_ino`` because inode numbers are only unique within one
    filesystem.

    This is best-effort identity verification, not a proof: POSIX has no
    atomic "mkdir and stat" or "stat and rmdir", so two windows stay open no
    matter how this is written -- between this ``mkdir`` and the ``lstat``
    that records identity, and, later, between the cleanup ``lstat`` and its
    ``rmdir`` (see ``_remove_created_dirs``). Both require another actor to
    delete and recreate this exact path inside a very short window, and
    ``rmdir`` only ever removes an empty directory, but neither window can be
    closed with plain path operations. ``dev``/``ino`` are ``None`` when the
    post-``mkdir`` ``lstat`` itself failed: the directory is still ours by
    construction, just unverified, and cleanup falls back to removing it
    outright rather than dropping it and leaking it.
    """

    path: Path
    dev: int | None
    ino: int | None


@dataclass(frozen=True)
class _Write:
    """One completed replacement, and everything rollback needs to undo it.

    ``written`` is the exact bytes this run put at ``target.path`` -- ``None``
    for a deletion (the ``delete_if_emptied`` branch, where this run's own
    effect on the path is its absence). Rollback compares this against what
    is on disk before touching anything: undoing a write that another process
    has since changed would destroy that change with no backup to recover it
    from, since the backup only ever holds the *pre*-run bytes.

    ``root`` is the same root ``_write_target`` was given (``home`` or the
    project root), kept so rollback can re-run the component check below it:
    reading or writing through ``target.path`` alone would still follow a
    parent directory swapped for a symlink after this run's own write landed.
    """

    target: Target
    backup: Path | None
    original_mode: int | None
    written: bytes | None
    root: Path | None
    # the parents this write had to create, deepest first, so rollback can put
    # the tree back the way it found it
    created_dirs: tuple[_CreatedDir, ...] = ()


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


def _write_target(snapshot: Snapshot, text: str, root: Path | None, stamp: str,
                  home: Path, command_name: str,
                  replace_file: Callable[[Path, Path], None],
                  record: Callable[[_Write], None]) -> None:
    """Re-read the target, refuse it if it moved since planning, then replace it.

    Planning read this file before the prompts, and ``text`` was rendered from
    what it read. Another agent editing the same config in that window would be
    silently overwritten -- and the backup would be no help, since restoring it
    also undoes the memriver edits the user just accepted. Re-reading through
    ``_read_snapshot`` re-runs the symlink refusal on the way, so the target is
    compared and re-checked in one step. ``command_name`` names the command
    whose own apply hit the race, so the remediation text tells the user to
    re-run the command they actually ran.

    ``record`` appends the completed write to the caller's rollback ledger.
    A rewrite is recorded once the replacement has landed -- an atomic swap
    that never happened leaves nothing to undo. A deletion is recorded
    *before* the ``unlink``, because that side effect has no atomic swap in
    front of it: an interrupt in between would otherwise take the file away
    with no entry telling rollback to bring it back.
    """
    target = snapshot.target
    if _read_snapshot(target, root, command_name) != snapshot:
        raise PlanningError(
            f"{display_path(target.path, home)}: file changed since planning; "
            f"nothing further was written -- re-run memriver {command_name}"
        )
    original_mode = snapshot.mode
    created_dirs: tuple[_CreatedDir, ...] = ()
    recorded = False
    try:
        created_dirs = _make_dirs(target.path.parent)
        backup = (
            _write_backup(target, original_mode, stamp) if original_mode is not None
            else None
        )
        deleting = text == "" and target.delete_if_emptied and original_mode is not None
        data = text.encode("utf-8")
        write = _Write(target=target, backup=backup, original_mode=original_mode,
                       written=None if deleting else data, root=root,
                       created_dirs=created_dirs)
        mode = original_mode
        if mode is None:
            mode = 0o600 if target.user_level else _umask_mode()
        if deleting:
            # this target is entirely memriver's own file; a removal that
            # empties it takes the file with it rather than leaving an empty
            # one behind (spec P2-6). The backup just written above still
            # holds the pre-removal bytes, so rollback and the printed
            # restore command both work exactly as they do for a rewrite.
            record(write)
            recorded = True
            target.path.unlink()
        else:
            _replace_atomically(target.path, data, mode, replace_file)
            record(write)
    except BaseException:
        # a write that never joined the rollback list takes its own directories
        # back; a backup already written keeps its parent, which is what
        # `_remove_created_dirs` refusing a non-empty directory does. One
        # already on the list leaves both to `_roll_back`, which restores the
        # file first and only then reclaims the directories holding it.
        if not recorded:
            _remove_created_dirs(created_dirs)
        raise


def _make_dirs(directory: Path) -> tuple[_CreatedDir, ...]:
    """Create ``directory`` and its missing parents; return the ones we made.

    One level at a time, shallow to deep, and every ``mkdir`` is exclusive:
    creating the directory *is* the ownership claim. ``FileExistsError`` means
    somebody else owns that level -- a directory that was always there, or one
    another process created while this run was on its way to it -- so it is
    skipped rather than recorded. ``mkdir(parents=True)`` cannot do this: it
    is not atomic across levels, so a failure part way up leaves directories
    behind that no return value names. A failure here cleans up its own
    climb.

    Each record carries the ``(st_dev, st_ino)`` a fresh post-``mkdir``
    ``lstat`` produced, not just the path: rollback re-checks that pair
    before it removes anything, which narrows -- but, being plain POSIX path
    operations with no atomic "mkdir and stat", cannot eliminate -- the
    chance that a path recycled by another actor in the meantime is mistaken
    for the directory this run made (see ``_CreatedDir`` and
    ``_remove_created_dirs``). A directory is appended to ``created`` the
    instant ``mkdir`` returns, with ``dev``/``ino`` still ``None``, and only
    upgraded with the ``lstat`` identity afterwards -- so the record exists
    before ``lstat`` is even attempted, and nothing from that point on can
    drop it while still leaking the directory on disk. ``lstat`` failing with
    an ordinary ``OSError`` is a degraded but unremarkable outcome, not a
    reason to give up on the rest of the install: the placeholder stays
    unverified and the climb continues. Anything else -- a
    ``KeyboardInterrupt``, any other ``BaseException`` -- is not caught here;
    it propagates past this loop and straight to the outer handler below,
    which still finds the directory already recorded.
    """
    created: list[_CreatedDir] = []
    try:
        for parent in reversed([d for d in (directory, *directory.parents)
                                if not d.exists()]):
            try:
                parent.mkdir()
            except FileExistsError:
                continue
            created.insert(0, _CreatedDir(parent, None, None))
            try:
                info = parent.lstat()
            except OSError:
                continue
            created[0] = _CreatedDir(parent, info.st_dev, info.st_ino)
    except BaseException:
        _remove_created_dirs(created)
        raise
    return tuple(created)


def _remove_created_dirs(created_dirs: Sequence[_CreatedDir]) -> None:
    """Take back the directories this run made, deepest first, while they hold
    the same identity ``_make_dirs`` recorded (or none was ever verified) and
    are still empty.

    A path missing outright is not proof that an ancestor this run also made
    cannot still be removed -- another actor taking the emptied leaf away
    leaves the parent just as removable -- so a ``FileNotFoundError`` keeps
    the climb going rather than stopping it. A path that still exists but
    resolves to a different ``(st_dev, st_ino)`` is a different directory --
    deleted and recreated by another actor in the same window -- and neither
    it nor anything above it (now proven non-empty by holding that stranger)
    is this run's to remove, so the climb stops there. Only once identity
    matches, or was never captured (``dev`` is ``None``, best-effort: `mkdir`
    succeeded but the identity ``lstat`` in ``_make_dirs`` did not), does
    ``rmdir`` run, and it adds the last guard on its own: a directory holding
    a backup this run wrote, another harness's file, or a target still to be
    rolled back refuses removal, which stops the climb the same way.

    This check is best-effort, not a proof of ownership: POSIX has no atomic
    "stat and rmdir" any more than ``_make_dirs`` has an atomic "mkdir and
    stat", so a replacement that lands in the instant between this ``lstat``
    and the ``rmdir`` below is still removed. Both windows require exact,
    very short concurrent timing to hit, and ``rmdir`` never touches a
    non-empty directory, but neither one is closed.
    """
    for created in created_dirs:
        if created.dev is None:
            try:
                created.path.rmdir()
            except FileNotFoundError:
                continue
            except OSError:
                return
            continue
        try:
            info = created.path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return
        if (info.st_dev, info.st_ino) != (created.dev, created.ino):
            return
        try:
            created.path.rmdir()
        except OSError:
            return


# a path that exists but is a symlink or another non-regular file (FIFO,
# device, ...): never read through it -- that could follow the link
# somewhere this run never wrote, or block forever on a FIFO -- so its
# content never equals anything and it always falls to the "changed" branch
_NOT_A_REGULAR_FILE = object()


def _current_bytes(path: Path) -> bytes | None | object:
    """The bytes at ``path`` right now: ``None`` if absent, the sentinel
    above if it is not a plain file, otherwise its contents.

    ``lstat`` rather than ``stat``, consistent with ``_refuse_symlinks``:
    a symlink swapped in for the target is refused, not followed.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return _NOT_A_REGULAR_FILE
    return path.read_bytes()


def _left_as_is_report(path: Path, backup: Path | None) -> str:
    return (
        f"{path} changed after this run wrote it; left as is -- "
        + (f"merge your changes from {backup}" if backup
           else "this run created it, and there is no backup")
    )


def _roll_back(writes: Sequence[_Write],
               replace_file: Callable[[Path, Path], None]) -> list[str]:
    """Undo completed writes newest-first. Backups survive every outcome.

    Before touching a path at all, ``_symlinked_component`` re-runs the same
    component check ``_refuse_symlinks`` already ran before this run's own
    write: a parent directory swapped for a symlink after that write landed
    would otherwise still be followed straight through by a plain
    ``read_bytes``/``unlink``/atomic replace on the leaf path, which checking
    only the leaf itself (even by ``lstat``) cannot catch. A link anywhere
    below the root is treated exactly like any other foreign change below --
    left alone and reported -- without reading through it or writing to
    whatever it resolves to.

    Past that check, the path's current bytes (``None`` if absent, or the
    not-a-regular-file sentinel for a symlink or special file at the leaf)
    are compared against ``write.written`` -- what this run itself left
    there. A match means nothing has touched the file since, so the backup
    is restored, or the file this run created is removed, exactly as before.

    Two mismatches are not a foreign edit, only this run's own write never
    actually landing, and are treated as already undone rather than reported:
    a deletion whose ``unlink`` never happened (it raised, or an interrupt
    landed between recording the write and running it) leaves the file
    holding exactly the bytes the backup does, so restoring the backup over
    it would be a no-op; and a file this run created that is already absent
    needs nothing restored either, only its created directories reclaimed.

    Any other mismatch -- another process edited a rewritten target,
    recreated a path this run deleted, or changed a file this run created --
    leaves it exactly as it is: the backup is the pre-run state, and
    overwriting or deleting the current content would destroy an edit no
    backup holds. The directories this run created are only reclaimed once
    their file was actually rolled back (or found already gone); one left in
    place keeps its directory.

    None of this closes the check-then-act window between this check and the
    restore/removal below -- POSIX has no atomic "check every component,
    then act on the leaf" -- it only narrows the case a plain path operation
    would miss outright: a link already in place by the time rollback looks.

    An ``OSError`` from the component check itself (as opposed to it finding
    a link) is never treated as "no link found": a stat failure proves
    nothing was verified, not that nothing is wrong, and a parent really
    could have been swapped for a link right where this failed to look. It
    falls through to the catch-all below like any other unexpected failure,
    reported as unable to recover and left exactly as it is -- this is one
    write inside the loop, so it does not stop any other write in the same
    run from rolling back normally.
    """
    report: list[str] = []
    for write in reversed(writes):
        path = write.target.path
        try:
            if _symlinked_component(write.target, write.root) is not None:
                report.append(_left_as_is_report(path, write.backup))
                continue
            current = _current_bytes(path)
            if current == write.written:
                if write.backup is None:
                    path.unlink(missing_ok=True)
                    report.append(f"removed {path} (this run created it)")
                else:
                    _replace_atomically(path, write.backup.read_bytes(),
                                        write.original_mode, replace_file)
                    report.append(f"restored {path} from {write.backup}")
                _remove_created_dirs(write.created_dirs)
                continue
            if (write.written is None and write.backup is not None
                    and current == write.backup.read_bytes()):
                continue  # this run's own unlink never happened; already fine
            if write.backup is None and current is None:
                _remove_created_dirs(write.created_dirs)
                continue  # this run's own file is already gone; already fine
            report.append(_left_as_is_report(path, write.backup))
        except Exception as error:  # noqa: BLE001 - every outcome gets reported
            report.append(
                f"COULD NOT recover {path}"
                + (f" from {write.backup}" if write.backup else "")
                + f": {error}"
            )
    return report


# --- reporting ----------------------------------------------------------------


def _in_effect(plan: _Plan,
               accepted: Sequence[EditOperation]) -> frozenset[str]:
    """The operations whose expected state holds once the run is over.

    Everything the files already satisfied, plus everything the user said yes
    to. Planning cannot answer this on its own: consent is one question per
    change, and a declined change leaves the file exactly as it was found.
    """
    changed = {change.operation.id for change in plan.changes}
    return frozenset(
        [operation.id for operation in plan.operations
         if operation.id not in changed]
        + [operation.id for operation in accepted])


def _write_completion_notes(plan: _Plan, harnesses: Sequence[str],
                            accepted: Sequence[EditOperation],
                            stdout: TextIO) -> None:
    """The read-only tail of every completion report: notes, then Codex trust.

    Spec 5.3 wants a checked-and-left-alone decision said out loud, and spec
    5.4 wants the Codex trust step named on every path that names Codex --
    including the one where nothing was written. The notes are composed here
    rather than during planning because they describe the run's outcome, and
    only the accepted operations know what that is.
    """
    in_effect = _in_effect(plan, accepted)
    for name in harnesses:
        module = HARNESSES[name]
        # optional: only a harness with something read-only to report defines it
        if hasattr(module, "notes"):
            stdout.writelines("\n" + note + "\n" for note in module.notes(
                plan.harness_snapshots[name], plan.env, in_effect))
    if shutil.which("uvx") is None:
        stdout.write("\n" + MISSING_UVX_NOTE + "\n")
    if "codex" in harnesses:
        stdout.write("\n" + CODEX_TRUST_NOTE + "\n")


def _write_uninstall_completion_notes(plan: _Plan, harnesses: Sequence[str],
                                      accepted: Sequence[RemovalOperation],
                                      stdout: TextIO) -> None:
    """The read-only tail of an uninstall report: only the native-memory verdict.

    There is no trust step to repeat -- the hooks it names are being removed,
    not installed -- and no property of the run depends on what was accepted:
    the native-memory setting is never one of the operations uninstall offers
    (spec: it is left exactly where install put it), so it is read straight
    from the planning snapshot regardless of which other changes were taken.
    """
    del accepted
    for name in harnesses:
        module = HARNESSES[name]
        if hasattr(module, "uninstall_notes"):
            stdout.writelines("\n" + note + "\n" for note in module.uninstall_notes(
                plan.harness_snapshots[name], plan.env))


def _restore_command(backup: Path, path: Path) -> str:
    return f"cp -p -- {shlex.quote(str(backup))} {shlex.quote(str(path))}"


def _success_report(writes: Sequence[_Write], command_name: str) -> str:
    lines = ["", f"{command_name}ed:"]
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
    return "\n".join(lines) + "\n"


# --- entry point --------------------------------------------------------------


def run_install(harnesses: Sequence[str], *, yes: bool, dry_run: bool,
                home: Path, cwd: Path, env: Mapping[str, str],
                input_fn: Callable[[str], str], stdout: TextIO, stderr: TextIO,
                replace_file: Callable[[Path, Path], None],
                store_step: StoreStep | None = None, stdin_is_tty: bool = True) -> int:
    """Plan, confirm, and apply installation; return the process exit code.

    Store initialization is a required prerequisite with explicit cancellation.
    Accepted harness edits are rendered and validated before applying the store;
    the store is ready before any harness file is written. Failures use stderr,
    plans and normal results use stdout. No store/harness transaction is implied.
    """
    try:
        plan = _plan(harnesses, home, cwd, env, collect_operations=_install_operations,
                    apply_fn=apply_edit, summary_fn=render_change_summary,
                    command_name="install")
    except PlanningError as error:
        stderr.write(f"memriver install: {error}\n")
        return 1

    if not plan.changes and store_step is None:
        stdout.write("memriver install: already up to date, nothing to change.\n")
        # notes and the trust step are properties of the harness, not of
        # having written something: an untrusted hook definition does not run,
        # and a reinstall is what a user who missed the note reaches for
        _write_completion_notes(plan, harnesses, (), stdout)
        return 0

    stdout.write("".join("\n" + change.summary for change in plan.changes))
    if store_step is not None:
        # the store is a change of this plan like any other: shown before any prompt
        stdout.write("\n" + store_step.summary + "\n")

    if dry_run:
        stdout.write("\ndry run: nothing was written.\n")
        # nothing was written, so nothing this run planned is in effect
        _write_completion_notes(plan, harnesses, (), stdout)
        return 0

    if store_step is not None and not yes and not stdin_is_tty:
        # piped input is not consent to create the store
        stderr.write(STORE_NEEDS_A_TERMINAL)
        return 1

    try:
        store_accepted = store_step is None or yes or input_fn(
            f"initialize {store_step.label} and continue installation? [y/N] "
        ).strip().lower() in ("y", "yes")
        if not store_accepted:
            stdout.write(STORE_DECLINED)
            return 1
        accepted = _confirm(plan.changes, yes=yes, input_fn=input_fn, home=home)
    except EOFError:
        stderr.write(
            "\nmemriver install: stdin is not interactive and no answer can be "
            "read; re-run with --yes to accept every change shown above.\n"
        )
        return 1

    # render and validate the accepted harness edits before anything is
    # written: a known PlanningError must not leave a store behind
    texts: dict = {}
    if accepted:
        try:
            texts, _ = _rendered(accepted, plan.snapshots, apply_edit, "install")
        except PlanningError as error:
            stderr.write(f"\nmemriver install: {error}\n")
            return 1

    if store_step is not None:
        # then the store, alone: a harness change is only applied once the
        # store it will point at is ready; a later harness failure may leave
        # this (legal, empty) global project behind -- no cross-file rollback
        try:
            stdout.write("\n" + store_step.apply() + "\n")
        except Exception:  # noqa: BLE001 - one fixed line, whatever the cause
            stderr.write(STORE_FAILED)
            return 1

    if not accepted:
        stdout.write("\nno harness change accepted; no harness file was changed.\n"
                     if store_step is not None else
                     "\nnothing accepted; no file was changed.\n")
        # same reasoning as the no-change branch above: hooks installed by an
        # earlier run may still be untrusted, and the native-memory verdict is
        # owed on every completion path, not only the ones that wrote something
        _write_completion_notes(plan, harnesses, (), stdout)
        return 0

    roots = {True: home, False: plan.project_root}
    pending = [
        (plan.snapshots[path], text, roots[plan.targets[path].user_level])
        for path, text in texts.items()
        if text != (plan.snapshots[path].text or "")
    ]
    return _apply(pending, plan, harnesses, accepted, home=home, stdout=stdout, stderr=stderr,
                  replace_file=replace_file, write_notes_fn=_write_completion_notes,
                  command_name="install")


def run_config_uninstall(harnesses: Sequence[str], *, yes: bool, dry_run: bool,
                         home: Path, cwd: Path, env: Mapping[str, str],
                         input_fn: Callable[[str], str], stdout: TextIO,
                         replace_file: Callable[[Path, Path], None]) -> int:
    """Plan, confirm, and apply the config removal; return the process exit code.

    This is install's exact inverse, through the same plan/confirm/apply/
    backup/rollback machinery -- only which per-harness function builds the
    operations, which editor runs them, and which completion notes are owed
    differ. Purging the storage root and clearing the uv cache are not this
    package's business (see the module docstring): ``memriver.uninstall``'s own
    ``run_uninstall``, one layer up, calls this first and only proceeds past a
    nonzero exit here.
    """
    try:
        plan = _plan(harnesses, home, cwd, env,
                    collect_operations=_uninstall_operations,
                    apply_fn=apply_removal, summary_fn=render_removal_summary,
                    command_name="uninstall")
    except PlanningError as error:
        stdout.write(f"memriver uninstall: {error}\n")
        return 1

    if not plan.changes:
        stdout.write("memriver uninstall: already clean, nothing to remove.\n")
        _write_uninstall_completion_notes(plan, harnesses, (), stdout)
        return 0

    stdout.write("".join("\n" + change.summary for change in plan.changes))

    if dry_run:
        stdout.write("\ndry run: nothing was written.\n")
        _write_uninstall_completion_notes(plan, harnesses, (), stdout)
        return 0

    try:
        accepted = _confirm(plan.changes, yes=yes, input_fn=input_fn, home=home)
    except EOFError:
        stdout.write(
            "\nmemriver uninstall: stdin is not interactive and no answer can be "
            "read; re-run with --yes to accept every change shown above.\n"
        )
        return 1

    if not accepted:
        stdout.write("\nnothing accepted; no file was changed.\n")
        _write_uninstall_completion_notes(plan, harnesses, (), stdout)
        return 0

    try:
        texts, _ = _rendered(accepted, plan.snapshots, apply_removal, "uninstall")
    except PlanningError as error:
        stdout.write(f"\nmemriver uninstall: {error}\n")
        return 1

    roots = {True: home, False: plan.project_root}
    pending = [
        (plan.snapshots[path], text, roots[plan.targets[path].user_level])
        for path, text in texts.items()
        if text != (plan.snapshots[path].text or "")
    ]
    return _apply(pending, plan, harnesses, accepted, home=home, stdout=stdout,
                  stderr=stdout, replace_file=replace_file,
                  write_notes_fn=_write_uninstall_completion_notes,
                  command_name="uninstall")


def _confirm(changes: Sequence[_PlannedChange], *, yes: bool,
             input_fn: Callable[[str], str],
             home: Path) -> tuple[EditOperation | RemovalOperation, ...]:
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


def _apply(pending: Sequence[tuple[Snapshot, str, Path | None]], plan: _Plan,
           harnesses: Sequence[str],
           accepted: Sequence[EditOperation | RemovalOperation], *,
           home: Path, stdout: TextIO, stderr: TextIO,
           replace_file: Callable[[Path, Path], None],
           write_notes_fn: Callable[[_Plan, Sequence[str], Sequence[Any], TextIO], None],
           command_name: str) -> int:
    stamp = _utc_timestamp()
    writes: list[_Write] = []
    try:
        for snapshot, text, root in pending:
            _write_target(snapshot, text, root, stamp, home, command_name,
                          replace_file, writes.append)
    except BaseException as error:  # a Ctrl-C between replacements rolls back too
        stderr.write(f"\nmemriver {command_name} failed: {error}\n")
        stderr.write("".join(
            f"  {line}\n" for line in _roll_back(writes, replace_file)))
        stderr.write("  backups were kept; no backup is ever deleted.\n")
        if not isinstance(error, Exception):
            raise  # KeyboardInterrupt / SystemExit: rolled back, never swallowed
        return 1
    stdout.write(_success_report(writes, command_name))
    if command_name == "uninstall":
        stdout.write(_left_empty_report(pending, home))
    write_notes_fn(plan, harnesses, accepted, stdout)
    return 0


def _is_effectively_empty(text: str, kind: EditorKind) -> bool:
    """Whether a removal left ``text`` holding nothing memriver-relevant.

    A shared harness file is never deleted (spec P2-6): when the container
    install put its entry in ends up holding nothing else, the file itself
    still exists, just emptied -- this is what the completion report calls
    out as residue.
    """
    if kind == "marker-block":
        return text == ""
    if kind == "toml-table":
        return not text.strip()
    if not text.strip():
        return True
    try:
        parsed = json.loads(text)
    except ValueError:
        return False  # unparseable text never reaches here in practice
    return _holds_only_empty_containers(parsed)


def _holds_only_empty_containers(value: object) -> bool:
    """Whether ``value`` is nothing but nested empty dicts/lists.

    ``{"mcpServers": {}}`` is exactly this -- the container install put its
    entry in, holding nothing else. A single real leaf anywhere (a string, a
    number, a foreign non-empty list) means the file still carries content
    that is not memriver's residue to report.
    """
    if isinstance(value, dict):
        return all(_holds_only_empty_containers(v) for v in value.values())
    if isinstance(value, list):
        return len(value) == 0
    return False


def _left_empty_report(pending: Sequence[tuple[Snapshot, str, Path | None]],
                       home: Path) -> str:
    """One line naming every shared target a removal emptied but did not
    delete -- a file ``Target.delete_if_emptied`` marks memriver's own
    (kiro's steering file) is excluded, since that one is deleted outright
    rather than left behind (see ``_write_target``).
    """
    left_empty = [
        display_path(snapshot.target.path, home) for snapshot, text, _ in pending
        if not snapshot.target.delete_if_emptied
        and _is_effectively_empty(text, _document_kind(snapshot.target))
    ]
    if not left_empty:
        return ""
    return f"\nleft empty (not deleted): {', '.join(left_empty)}\n"
