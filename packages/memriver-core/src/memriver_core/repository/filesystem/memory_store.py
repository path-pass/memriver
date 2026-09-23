"""`MemoryStore` over a flat `memories/` directory of frontmatter markdown files."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from memriver_core.application.errors import (
    GlobalReadOnly,
    IdCollision,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)
from memriver_core.models import ID_RE, AccessContext, Memory, now_strictly_after

from .files import (
    MEMORIES_DIRNAME,
    container_exists,
    memory_path,
    read_regular_text,
    replace_file,
    write_new,
)
from .locking import store_lock
from .markdown_codec import decode, encode

if TYPE_CHECKING:
    from .project_store import FileProjectStore

logger = logging.getLogger(__name__)

_NO_WRITABLE_PROJECT = "no writable project in this context"


def _load(root: Path, memory_id: str) -> Memory | None:
    """The memory stored under `memory_id`; None when nothing is stored there.

    A malformed id and an absent file are "nothing". A file that is present
    but unusable -- not a regular file, unreadable, undecodable, or naming a
    different id -- is StorageFailure: damage is reported as damage, never
    disguised as absence (`doctor` names the file). An unsafe `memories/`
    container is StorageFailure too.
    """
    if not ID_RE.fullmatch(memory_id):
        return None
    try:
        if not container_exists(root, MEMORIES_DIRNAME):
            return None
        text = read_regular_text(memory_path(root, memory_id))
    except (OSError, UnicodeDecodeError) as err:
        raise StorageFailure from err
    if text is None:
        return None
    try:
        memory = decode(text)
    except Exception as err:
        raise StorageFailure from err
    if memory.id != memory_id:
        raise StorageFailure
    return memory


def iter_memories(root: Path) -> Iterator[Memory]:
    """Every usable memory in the store, in file-name order.

    One damaged file does not fail a scan: it is skipped with a path-free log
    line and `doctor` reports it. An unsafe or unlistable `memories/`
    directory fails the whole scan with StorageFailure.
    """
    try:
        if not container_exists(root, MEMORIES_DIRNAME):
            return
        names = sorted(entry.name for entry in os.scandir(root / MEMORIES_DIRNAME))
    except OSError as err:
        raise StorageFailure from err
    for name in names:
        memory_id = name[: -len(".md")] if name.endswith(".md") else ""
        if not ID_RE.fullmatch(memory_id):
            continue
        try:
            memory = _load(root, memory_id)
        except StorageFailure:
            logger.warning("skipping unusable memory file %s", memory_id)
            continue
        if memory is not None:
            yield memory


class FileMemoryStore:
    def __init__(self, root: Path, project_store: FileProjectStore) -> None:
        self.root = Path(root)
        self._project_store = project_store

    def record(self, memory: Memory, ctx: AccessContext) -> None:
        if memory.project_id == ctx.global_project_id:
            raise GlobalReadOnly()
        if memory.project_id not in ctx.writable():
            raise ProjectUnavailable(_NO_WRITABLE_PROJECT)
        if not ID_RE.fullmatch(memory.id):
            raise ValueError("invalid memory id")
        with store_lock(self.root):
            try:
                self._project_store.read(memory.project_id)
            except ProjectNotFound:
                raise ProjectUnavailable(_NO_WRITABLE_PROJECT) from None
            try:
                write_new(self.root, memory_path(self.root, memory.id), encode(memory))
            except FileExistsError:
                # the id is taken: nothing was written; the facade draws again
                raise IdCollision(memory.id) from None
            except OSError as err:
                raise StorageFailure from err

    def read(self, memory_id: str, ctx: AccessContext) -> Memory:
        memory = _load(self.root, memory_id)
        if memory is None or memory.project_id not in ctx.readable():
            raise MemoryNotFound(memory_id)
        # the project is re-checked at action time, not only when the context
        # was built: a server keeps its context for its whole life, a project
        # file can be deleted by hand meanwhile, and a caller can hand-build a
        # context. A true orphan answers like any other hidden id; a damaged
        # project file is StorageFailure.
        try:
            self._project_store.read(memory.project_id)
        except ProjectNotFound:
            raise MemoryNotFound(memory_id) from None
        return memory

    def update(self, memory_id: str, ctx: AccessContext, *, body: str,
               description: str | None) -> Memory:
        with store_lock(self.root):
            memory = self._writable(memory_id, ctx)
            memory.body = body.strip()
            if description is not None:
                # None keeps the existing description; "" clears it
                memory.description = description.strip()
            memory.updated = now_strictly_after(memory.updated)
            try:
                replace_file(self.root, memory_path(self.root, memory_id), encode(memory))
            except OSError as err:
                raise StorageFailure from err
        return memory

    def delete(self, memory_id: str, ctx: AccessContext) -> None:
        with store_lock(self.root):
            self._writable(memory_id, ctx)
            try:
                memory_path(self.root, memory_id).unlink()
            except OSError as err:
                raise StorageFailure from err

    def _writable(self, memory_id: str, ctx: AccessContext) -> Memory:
        """Locate and authorize inside the lock; nothing moves between check and write."""
        memory = self.read(memory_id, ctx)
        if memory.project_id == ctx.global_project_id:
            # global is readable, so naming the rule reveals nothing
            raise GlobalReadOnly()
        if memory.project_id not in ctx.writable():
            raise MemoryNotFound(memory_id)
        return memory
