from __future__ import annotations

from typing import Protocol

from memriver_core.models import (
    Memory,
    Project,
    PromptEntry,
    ReadWriteSet,
    Resolution,
    RootPlan,
    Session,
    SessionKey,
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
    - `delete(hard=True)` and `delete_global(hard=True)` on a row any
      `memory_sources` row cites as a source raise
      `MemoryReferenced(memory_id, derived_ids)`; nothing is deleted.
    - `delete_global`: the management delete of a global entry (human CLI
      only, never MCP): any project's global row by id, the same version
      rules as `delete`; a row that is not global raises
      `ProjectUnavailable(reason="not-global")`.
    - `read_any`: the management read (human CLI only): any project, no
      read/write set; deleted rows only with `include_deleted`.
    - `touch_read`: best effort, after a successful `memory_read` (spec §3.3):
      moves `last_read_at` to `max(stored, at)`, never backwards; `version`
      and `updated` are untouched. An unknown id, a malformed `at`, and a
      store that is absent or fails, are all no-ops -- nothing here creates a
      store, and a failure never fails the read that asked for it. In the same
      transaction it inserts one `memory_reads` row (`memory_version` as
      given -- the version the read returned -- `at`, `harness`, `session_id`),
      only while the row is active, and, with `prune_before`, deletes every
      `memory_reads` row read before it. A failure of either rolls both back
      and is still a no-op for the caller.
    - Errors carry fields, never words (see `models.errors`).
    """

    def record(self, memory: Memory, read_write_set: ReadWriteSet) -> None: ...
    def read(self, memory_id: str, read_write_set: ReadWriteSet) -> Memory: ...
    def update(self, memory_id: str, read_write_set: ReadWriteSet, *, expected_version: int,
               body: str, description: str | None) -> Memory: ...
    def delete(self, memory_id: str, read_write_set: ReadWriteSet, *, expected_version: int,
               hard: bool) -> int: ...
    def delete_global(self, memory_id: str, *, expected_version: int, hard: bool) -> int: ...
    def read_any(self, memory_id: str, *, include_deleted: bool) -> Memory: ...
    def touch_read(self, memory_id: str, at: str, *, memory_version: int,
                   harness: str = "unknown", session_id: str | None = None,
                   prune_before: str | None = None) -> None: ...


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
    - `resolve(start, ignoring=None, logical=None)`: which project `start`
      belongs to; `ignoring=(project_id, root)` previews the answer without
      that binding; `logical` walks that path's ancestors instead of
      `start`'s (a linked worktree mapped onto its main tree), `start` still
      having to be a real directory.
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
    def resolve(self, start: str, *, ignoring: tuple[str, str] | None = None,
                logical: str | None = None) -> Resolution: ...
    def plan_root(self, directory: str, project_id: str | None) -> RootPlan: ...
    def bind(self, project_id: str, plan: RootPlan) -> None: ...
    def plan_unbind(self, project_id: str, root: str,
                    cwd: str) -> tuple[UnbindPlan, Resolution]: ...
    def unbind(self, plan: UnbindPlan) -> None: ...


class SessionStore(Protocol):
    """One row per harness session (spec §3.1, §3.3); the stored row is the answer.

    - Every method is one short transaction. Nothing here creates a store:
      every write opens without creating, and a store that is absent -- or
      removed between the check and the connect -- makes it a no-op that
      returns None (False for `nudge_if_due`).
    - A row the adapter returns is validated like a memory row: a bad row is
      `StorageFailure` on a direct lookup and skipped by `search`. A row a
      write would leave behind that would not read back is a `ValueError`,
      and nothing is written. Other store trouble is `StorageFailure`.
    - `store_exists`: whether the store is there at all; never creates it.
      `StorageFailure` when it cannot be checked (not a regular file, an
      unreadable directory).
    - `register`: insert if absent, never overwrite; returns the stored row,
      which may be a concurrent peer's.
    - `touch`: `last_active_at = max(stored, at)`; a non-None
      `transcript_path` replaces the stored one. None for an unknown key.
    - `add_prompt`: inserts `seed` when no row exists (the bool: this call
      inserted it), then counts the prompt, sets `first_prompt` once, keeps
      the last `keep_recent` entries of `recent_prompts` (newest last) and
      moves `last_active_at` forward.
    - `end`: `ended_at = at`, touching `last_active_at`.
    - `nudge_if_due`: registered rows only (anything else: False, nothing
      written). Touches the row; True, recording the nudge, iff it has a
      project, at least `min_prompts` prompts since the last save, and either
      no nudge yet or at least `interval` prompts since the last one.
    - `mark_saved`: the save watermark moves to the prompt count.
    - `confirm`: a pending row becomes registered -- with no project when it
      has no candidate, else with its candidate, provided that project still
      exists, is not global and has the root the candidate was computed
      from; otherwise `ProjectUnavailable(reason="candidate-changed")` and
      the row is unchanged. A registered row is returned unchanged; None for
      an unknown key.
    - `assign_project`: a row with no project and no candidate (registered,
      or pending with a NULL candidate) becomes registered with `project_id`;
      any other row is returned unchanged -- a project set meanwhile is never
      overwritten. `origin`, `entry_cwd` and `branch` stay. None for an
      unknown key.
    - `search`: `project_id=None` is every row (the human CLI); otherwise
      that project's registered rows. A case-insensitive substring of a
      prompt text, `entry_cwd` or `branch`; newest `last_active_at` first.
    - `record_call`: maps a harness's tool-call id to `key`'s session
      (replacing an earlier mapping of the same id), then drops every
      mapping recorded more than `retention_s` seconds before `at`, in the
      same transaction. A call id that is not a harness id (see
      `is_call_id`), a malformed `at` or a non-positive `retention_s` is a
      `ValueError`, and nothing is written.
    - `session_for_call`: the session a call id was mapped to, or None --
      also for an impossible call id and for a stored row whose session id
      is invalid. Read-only.
    """

    def store_exists(self) -> bool: ...
    def get(self, key: SessionKey) -> Session | None: ...
    def register(self, session: Session) -> Session | None: ...
    def touch(self, key: SessionKey, at: str, *,
              transcript_path: str | None = None) -> Session | None: ...
    def add_prompt(self, key: SessionKey, entry: PromptEntry, *, seed: Session,
                   keep_recent: int) -> tuple[Session, bool] | None: ...
    def end(self, key: SessionKey, at: str) -> None: ...
    def nudge_if_due(self, key: SessionKey, at: str, *, min_prompts: int,
                     interval: int) -> bool: ...
    def mark_saved(self, key: SessionKey) -> None: ...
    def confirm(self, key: SessionKey) -> Session | None: ...
    def assign_project(self, key: SessionKey, project_id: str) -> Session | None: ...
    def search(self, project_id: str | None, query: str, limit: int) -> list[Session]: ...
    def record_call(self, key: SessionKey, call_id: str, at: str, *,
                    retention_s: int) -> None: ...
    def session_for_call(self, harness: str, call_id: str) -> SessionKey | None: ...
