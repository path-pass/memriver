from __future__ import annotations

from typing import Protocol

from memriver_core.models import (
    Memory,
    Project,
    ReadWriteSet,
    Resolution,
    RootPlan,
    UnbindPlan,
)


class MemoryStore(Protocol):
    """Single-memory actions: record, read, update, delete one memory.

    Binding semantics (every backend):

    - Authorization is decided on the stored row inside the operation's own
      transaction; `read_write_set.readable()`/`writable()` are the only inputs.
    - `record`: a global target raises `GlobalReadOnly`; a target outside
      `writable()`, or a project that does not exist, raises
      `ProjectUnavailable`; a taken id (deleted rows included) raises
      `IdCollision`; nothing is written.
    - `read`: malformed, absent, soft-deleted, orphaned or another project's
      id raises `MemoryNotFound(memory_id)`; a row that fails validation
      raises `StorageFailure`.
    - `update` / `delete(hard=False)`: absent, deleted, orphaned or outside
      `read_write_set.readable()` → `MemoryNotFound`; a row that fails
      validation → `StorageFailure`; global → `GlobalReadOnly`; not writable →
      `MemoryNotFound`; a version other than `expected_version` →
      `VersionConflict`. An update never revives a deleted row. A soft delete
      sets `deleted_at` and moves the version on; it returns the new version.
    - `delete(hard=True)`: the same checks, but a soft-deleted row of a
      writable project is accepted at its current version; the row is removed
      and 0 is returned.
    - `read_any`: the management read (human CLI only): any project, no
      read/write set; deleted rows only with `include_deleted`.
    - `touch_read`: best effort, after a successful `memory_read` (spec §3.3):
      moves `last_read_at` to `max(stored, at)`, never backwards; `version`
      and `updated` are untouched. An unknown id, and a store that is absent
      or fails, are all no-ops -- nothing here creates a store, and a failure
      never fails the read that asked for it.
    - Errors carry fields, never words (see `models.errors`).
    """

    def record(self, memory: Memory, read_write_set: ReadWriteSet) -> None: ...
    def read(self, memory_id: str, read_write_set: ReadWriteSet) -> Memory: ...
    def update(self, memory_id: str, read_write_set: ReadWriteSet, *, expected_version: int,
               body: str, description: str | None) -> Memory: ...
    def delete(self, memory_id: str, read_write_set: ReadWriteSet, *, expected_version: int,
               hard: bool) -> int: ...
    def read_any(self, memory_id: str, *, include_deleted: bool) -> Memory: ...
    def touch_read(self, memory_id: str, at: str) -> None: ...


class ProjectStore(Protocol):
    """Collections and directories: projects, global, one-project search, bindings.

    - `create(project, plan)`: the project and its directory in one
      transaction, under the confirmed plan (see `bind`); a taken id raises
      `IdCollision`; nothing is written on any refusal.
    - `read`: malformed or absent → `ProjectNotFound`; an invalid row →
      `StorageFailure`. `list_projects`: every project, global last.
    - `global_project_id`: None for an uninitialized store.
    - `ensure_global`: idempotent, one transaction; never touches memories.
    - `search`: exactly one project. A project outside `readable()` (when a
      read/write set is given) or one that does not exist answers `[]`; a
      `None` read/write set is the management view. Deleted rows never match;
      a single invalid memory row is skipped. `query=None` lists everything,
      `query=""` matches nothing, any other query is a case-insensitive
      substring of description or body. Newest `updated` first, then id.
    - `resolve(start, ignoring=None)`: which project `start` belongs to;
      `ignoring=(project_id, root)` previews the answer without that binding.
    - `plan_root` / `bind` / `plan_unbind` / `unbind`: the directory rules of
      the spec, every refusal a `BindingRefused(reason, project_id)`. A
      confirmed plan pins its store: a store that no longer canonicalizes to
      `plan.store` is `plan-changed` before anything is opened or created.
    """

    def create(self, project: Project, plan: RootPlan) -> None: ...
    def read(self, project_id: str) -> Project: ...
    def list_projects(self) -> list[Project]: ...
    def global_project_id(self) -> str | None: ...
    def ensure_global(self) -> str: ...
    def search(self, project_id: str, read_write_set: ReadWriteSet | None, *,
               query: str | None, limit: int | None) -> list[Memory]: ...
    def resolve(self, start: str, *,
                ignoring: tuple[str, str] | None = None) -> Resolution: ...
    def plan_root(self, directory: str, project_id: str | None) -> RootPlan: ...
    def bind(self, project_id: str, plan: RootPlan) -> None: ...
    def plan_unbind(self, project_id: str, root: str,
                    cwd: str) -> tuple[UnbindPlan, Resolution]: ...
    def unbind(self, plan: UnbindPlan) -> None: ...
