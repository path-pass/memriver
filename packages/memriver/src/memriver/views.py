"""Read-only views of the store for people, and one confirmed delete.

Every read is the core's management view (all projects); every rule is the
core's. This module renders, prompts and words refusals -- nothing else.
"""

from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import IO

from memriver_core import (
    GlobalReadOnly,
    MemoryNotFound,
    ProjectNotFound,
    StorageFailure,
    VersionConflict,
)
from memriver_core.models import Memory, Project, single_line
from memriver_core.settings import INDEX_CUE_CHARS

from .project_context import _INVISIBLE_CATEGORIES, visible

STORE_UNREADABLE = "refused: the memory store could not be read"
_EXPORT_FIELDS = ("id", "project_id", "type", "source_harness", "source_method", "trust",
                  "sync", "created", "updated", "version", "description")


def _export_value(memory: Memory, field: str) -> object:
    """One export header value: `source` is split into two JSON scalars, never one object."""
    if field == "source_harness":
        return memory.source["harness"]
    if field == "source_method":
        return memory.source["method"]
    return getattr(memory, field)


def _service(root: Path | None, home: Path):
    """The facade; a bad MEMRIVER_* value is reported like an unreadable store."""
    from memriver_core.bootstrap import build_service
    from memriver_core.settings import load_settings

    try:
        settings = load_settings(root_override=root)
        return build_service(settings, root=settings.root, home=home)
    except Exception as err:   # pydantic's ValidationError echoes the value: never shown
        raise StorageFailure from err


def _cue(memory: Memory) -> str:
    raw = memory.description or (memory.body.splitlines() or [""])[0]
    return visible(single_line(raw))[:INDEX_CUE_CHARS]


def _line(memory: Memory) -> str:
    # updated is stored text like any other field: neutralised before it is shown
    return f"  {memory.id}  [{memory.type}]  {visible(memory.updated)[:10]}  {_cue(memory)}\n"


def _where(project: Project, global_id: str | None) -> str:
    if project.id == global_id:
        return "global"
    return visible(project.root) if project.root else "no directory"


def _body(text: str) -> str:
    """The body for a terminal: newlines and tabs kept, every other invisible character a space."""
    return "".join(ch if ch in "\n\t" or unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
                   else " " for ch in text)


def run_list(*, root: Path | None, project_id: str | None, stdout: IO[str], home: Path) -> int:
    try:
        service = _service(root, home)
        listed = service.list_memories(project_id)
        global_id = service.global_project_id()
    except ProjectNotFound:
        stdout.write(f"no such project: {visible((project_id or '')[:255])}\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    for project, memories in listed:
        stdout.write(f"{project.id}  {visible(project.name)}  ({_where(project, global_id)})\n")
        stdout.write("".join(_line(m) for m in memories) or "  (no memories)\n")
    return 0


def run_show(memory_id: str, *, root: Path | None, deleted: bool, stdout: IO[str],
             home: Path) -> int:
    try:
        memory = _service(root, home).show(memory_id, include_deleted=deleted)
    except MemoryNotFound:
        stdout.write(f"no such memory: {visible(memory_id[:255])}\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    lines = [f"id: {memory.id}", f"project: {memory.project_id}", f"type: {memory.type}",
             f"source: {visible(memory.source['harness'])}/{visible(memory.source['method'])}",
             f"trust: {memory.trust}", f"sync: {str(memory.sync).lower()}",
             f"created: {visible(memory.created)}", f"updated: {visible(memory.updated)}",
             f"version: {memory.version}", f"description: {visible(single_line(memory.description))}"]
    if memory.deleted_at is not None:
        lines.append(f"deleted: {visible(memory.deleted_at)}")
    stdout.write("\n".join(lines) + "\n---\n" + _body(memory.body) + "\n")
    return 0


def run_search(query: str, *, root: Path | None, project_id: str | None, limit: int | None,
               stdout: IO[str], home: Path) -> int:
    try:
        hits = _service(root, home).search_all(query, project_id, limit)
    except ProjectNotFound:
        stdout.write(f"no such project: {visible((project_id or '')[:255])}\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    stdout.write("".join(_line(m) for m in hits) or "(no matches)\n")
    return 0


def _refuse_unsafe_export_name(name: str) -> None:
    """Refuse a name that would step outside DIR (spec section 10.4): every export write
    is a single path component, never a path.

    Ids already pass ID_RE and never contain these characters, so this never fires on
    real data; it is defence in depth against a future caller passing something else.
    """
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("unsafe export name")


def _write_private(dir_fd: int, name: str, text: str) -> None:
    """Write ``name`` inside the directory ``dir_fd`` names -- never by path.

    ``dir_fd`` (held open by the caller) is what makes this safe against a
    symlink swapped in after the enclosing directory was created: every
    create is relative to the fd, not re-walked from a string path, and
    O_NOFOLLOW refuses a symlink placed at ``name`` itself.
    """
    _refuse_unsafe_export_name(name)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def run_export(directory: Path, *, root: Path | None, stdout: IO[str], home: Path,
               cwd: Path) -> int:
    target = directory if directory.is_absolute() else cwd / directory
    if os.path.lexists(target):
        stdout.write(f"refused: {visible(str(target))} already exists\n")
        return 2
    try:
        service = _service(root, home)
        listed = service.list_memories(None)
        global_id = service.global_project_id()
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        stdout.write(f"refused: {visible(str(target))} already exists\n")
        return 2
    except OSError as err:
        stdout.write(f"refused: {visible(str(target))} could not be created "
                     f"({err.strerror or 'error'})\n")
        return 2
    count = 0
    # Held directory descriptors, not path checks: a path re-resolved after
    # the directory it names was created can be re-defined from under us by
    # swapping that name for a symlink. Every create below is relative to an
    # fd opened O_NOFOLLOW right after its directory's own mkdir, so a swap
    # in between turns into an ELOOP/ENOTDIR OSError instead of a write that
    # follows the symlink out of the target.
    target_fd: int | None = None
    try:
        target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _write_private(target_fd, "projects.md", "".join(
            f"- {p.id} {single_line(p.name)} ({_where(p, global_id)})\n" for p, _ in listed))
        for project, memories in listed:
            if not memories:
                continue
            _refuse_unsafe_export_name(project.id)
            os.mkdir(project.id, 0o700, dir_fd=target_fd)
            folder_fd = os.open(project.id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=target_fd)
            try:
                for memory in memories:
                    header = "".join(
                        f"{field}: {json.dumps(_export_value(memory, field), ensure_ascii=True)}\n"
                        for field in _EXPORT_FIELDS)
                    _write_private(folder_fd, f"{memory.id}.md", f"---\n{header}---\n{memory.body}")
                    count += 1
            finally:
                os.close(folder_fd)
    except (OSError, ValueError, UnicodeError) as err:
        reason = err.strerror if isinstance(err, OSError) and err.strerror else "write failed"
        stdout.write(f"refused: the export stopped after {count} memories ({reason}); "
                     f"{visible(str(target))} holds a partial snapshot -- delete it and run "
                     "memriver export again\n")
        return 2
    finally:
        if target_fd is not None:
            os.close(target_fd)
    stdout.write(f"exported {count} memories to {visible(str(target))}\n")
    return 0


def run_delete(memory_id: str, *, version: int, hard: bool, yes: bool, root: Path | None,
               stdin_is_tty: bool, input_fn: Callable[[str], str], stdout: IO[str], cwd: Path,
               home: Path) -> int:
    try:
        service = _service(root, home)
        project_context = service.open_project_context(str(cwd))
        read_write_set = project_context.read_write_set
        memory = service.show(memory_id, include_deleted=hard)
    except MemoryNotFound:
        stdout.write(f"no such memory: {visible(memory_id[:255])}\n")
        return 2
    except StorageFailure:
        stdout.write(STORE_UNREADABLE + "\n")
        return 2
    # decided from the facts already in hand, before any plan line or prompt: the
    # in-transaction checks below still decide at write time, for a change between
    # this read and the delete itself
    if memory.project_id == read_write_set.global_project_id:
        stdout.write("refused: global memories cannot be deleted here\n")
        return 2
    if memory.project_id not in read_write_set.writable():
        # `memriver show` displays it, so "no such memory" would mislead a human
        stdout.write(f"refused: {memory.id} belongs to project {memory.project_id}, not this "
                     "directory's project; run memriver delete from that project's directory\n")
        return 2
    plan = (f"memriver delete: {memory.id} [{memory.type}] in project {memory.project_id}: "
            f"{_cue(memory)}  ({'hard' if hard else 'soft'})")
    if hard and memory.deleted_at is not None:
        plan += " (already deleted)"
    stdout.write(plan + "\n")
    if not yes:
        if not stdin_is_tty:
            stdout.write("refused: stdin is not a terminal; pass --yes to confirm non-interactively\n")
            return 2
        try:
            answer = input_fn("Proceed? [y/N] ")
        except EOFError:
            stdout.write("aborted; nothing was changed\n")
            return 1
        if answer.strip().lower() not in ("y", "yes"):
            stdout.write("aborted; nothing was changed\n")
            return 1
    try:
        service.delete(memory_id, project_context, expected_version=version, hard=hard)
    except MemoryNotFound:
        stdout.write(f"no such memory: {visible(memory_id[:255])}\n")
        return 2
    except GlobalReadOnly:
        stdout.write("refused: global memories cannot be deleted here\n")
        return 2
    except VersionConflict:
        stdout.write(f"refused: {memory_id} changed since version {version}; run memriver "
                     f"show {memory_id} and retry\n")
        return 2
    except StorageFailure:
        stdout.write("refused: the memory store could not be written\n")
        return 2
    stdout.write(f"{'purged' if hard else 'deleted'} {memory_id}\n")
    return 0
