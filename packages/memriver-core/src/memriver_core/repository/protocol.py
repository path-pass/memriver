from __future__ import annotations

from typing import Protocol

from memriver_core.models import Memory, Project, ReadWriteSet


class MemoryStore(Protocol):
    """Single-memory actions: record, read, update, delete one memory.

    Binding semantics (every backend):

    - Authorization is decided on the entry's stored `project_id` at the
      moment of the action; `update`/`delete` decide it inside their atomic
      section. `read_write_set.readable()`/`read_write_set.writable()` are the
      only inputs.
    - `record`: a global target raises `GlobalReadOnly`; a target outside
      `read_write_set.writable()`, or a project that does not exist, raises
      `ProjectUnavailable`; a taken id raises `IdCollision` and nothing is
      written or replaced.
    - `read`/`update`/`delete` raise `MemoryNotFound(memory_id)` for an id
      that is malformed, absent, another project's, or orphaned (its project
      does not exist). A present file that cannot be read or decoded, and an
      unsafe container, raise `StorageFailure`. `update`/`delete` of a global
      entry raise `GlobalReadOnly`.
    - Errors carry fields, never words (see `models.errors`).
    """

    def record(self, memory: Memory, read_write_set: ReadWriteSet) -> None: ...
    def read(self, memory_id: str, read_write_set: ReadWriteSet) -> Memory: ...
    def update(self, memory_id: str, read_write_set: ReadWriteSet, *, body: str,
               description: str | None) -> Memory: ...
    def delete(self, memory_id: str, read_write_set: ReadWriteSet) -> None: ...


class ProjectStore(Protocol):
    """Collection actions: create and read a project, identify global, search one project.

    - `create` never overwrites: a taken id raises `IdCollision`.
    - `read`: malformed or absent → `ProjectNotFound`; unusable → `StorageFailure`.
    - `global_project_id`: `None` for an uninitialized store; `StorageFailure`
      when the manifest is invalid or names a missing project.
    - `ensure_global`: idempotent; creates the global project and the
      manifest only when the manifest is absent; never touches memories. A
      taken project id raises `IdCollision` with nothing written.
    - `search`: exactly one project. A project outside
      `read_write_set.readable()` or one that does not exist answers `[]`; a
      damaged project file or an unusable `memories/` is `StorageFailure`; a
      single damaged memory is skipped. `query=None` lists everything,
      `query=""` matches nothing, any other query is a case-insensitive
      substring of description or body. Newest `updated` first; `limit=None`
      returns all.
    """

    def create(self, project: Project) -> None: ...
    def read(self, project_id: str) -> Project: ...
    def global_project_id(self) -> str | None: ...
    def ensure_global(self) -> str: ...
    def search(self, project_id: str, read_write_set: ReadWriteSet, *, query: str | None,
               limit: int | None) -> list[Memory]: ...
