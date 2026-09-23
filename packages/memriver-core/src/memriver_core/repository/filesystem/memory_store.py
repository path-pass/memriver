"""`MemoryStore` over a flat `memories/` directory of frontmatter markdown files."""

from __future__ import annotations

from pathlib import Path

from memriver_core.models import ID_RE, AccessContext, Memory, now_strictly_after
from memriver_core.models.errors import (
    GlobalReadOnly,
    IdCollision,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)

from ..protocol import ProjectStore
from .files import memory_path, replace_file, write_new
from .locking import store_lock
from .markdown_codec import encode
from .memory_files import load_memory

_NO_WRITABLE_PROJECT = "no writable project in this context"


class FileMemoryStore:
    def __init__(self, root: Path, project_store: ProjectStore) -> None:
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
        memory = load_memory(self.root, memory_id)
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
