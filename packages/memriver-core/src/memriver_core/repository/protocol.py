from __future__ import annotations

from collections.abc import Callable, Sequence
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
from memriver_core.models.changes import (
    Change,
    Citation,
    HardDeletePlan,
    MemoryVersion,
    Op,
    PolicyHit,
    Usage,
)


class MemoryStore(Protocol):
    """Memory state, its permanent history and the change log.

    Binding semantics (every backend):

    - `apply` (management) and `write` (agent): every op is checked and
      written in one write transaction, as one change (`step_count =
      len(ops)`) with one step and one new version per op; any refusal writes
      nothing. `check(description, body)` is the content policy on each
      resulting state, deleted ones included: a rule id refuses it with
      `ContentRejected(rule_id=..., memory_id=...)` (memory_id None for a
      create). A created memory's `source_harness` is `changed_via` or
      "unknown", its `source_method` is `changed_by`. Every new version moves
      `updated`; a soft delete sets `deleted_at = updated`. A new version
      carries the current source set unless the op replaces it. The same
      memory twice is a `ValueError`; a taken id is `IdCollision`. Nothing
      here creates a store.
      - `apply` is the management path (spec §4.1):
        `BatchConflict(index, memory_id, reason)` for a missing target
        ("missing"), another version ("version"), an update or soft delete of
        a deleted memory ("deleted"), a result equal to the current state
        ("same-state"), a read at or after `unread_since` ("read-since"); a
        create in an unknown project is `ProjectNotFound`; global is an
        ordinary target.
      - `write` is the agent path: one op under a `ReadWriteSet`, decided on
        the stored rows inside the transaction: a create outside `writable()`
        or into a missing project → `ProjectUnavailable`, into global →
        `GlobalReadOnly`; an absent, deleted, orphaned, unreadable or not
        writable target → `MemoryNotFound`, a global one → `GlobalReadOnly`,
        another version → `VersionConflict`; a removed store answers the same
        and is never recreated. It returns the resulting memory as read in
        that transaction; a result equal to the current state writes nothing
        and returns the memory as checked (after every one of those checks
        passed).
    - `change`: a change with the steps still stored, or None. `undo`: in one
      transaction, `UndoRefused("not-found")` for an unknown change,
      `("hard-deleted")` when fewer steps are stored than `step_count`,
      `("changed", ids)` when any touched memory's current version is not its
      step's `after_version`; otherwise the inverse of every step (create →
      SoftDelete, update/soft_delete/restore → Restore to `before_version`) is
      applied as one new change with `undoes = change_id`, by the rules of
      `apply` (a `ContentRejected` refuses it).
    - `read`: malformed, absent, soft-deleted, orphaned or another project's
      id raises `MemoryNotFound(memory_id)`; a row that fails validation
      raises `StorageFailure`.
    - `read_any`: the management read (human CLI only): any project, no
      read/write set; deleted rows only with `include_deleted`.
    - `touch_read`: best effort, after a successful `memory_read` (spec §3.3):
      moves `last_read_at` to `max(stored, at)`, never backwards, and records
      one `memory_reads` row (the version handed out, the harness, the session
      id); `version` and `updated` are untouched. An unknown or deleted id, a
      malformed `at`, and a store that is absent or fails, are all no-ops --
      nothing here creates a store, and a failure never fails the read.
    - `apply` also takes `Restore` and explicit `sources` (spec §3.3): a
      reference in the current set is carried; any other must be the source's
      current, non-deleted version before the batch (else `BatchConflict(...,
      "source")`); a restored set needs every cited version to exist; a cycle
      through the source rows of any version is `BatchConflict(..., "cycle")`;
      a state with sources gets the lowest trust and the conjunction of sync of
      its previous state (updates) and every cited version; a restore takes the
      restored version's recorded trust and sync.
    - `versions`: every version of any memory, deleted ones included, with its
      sources and change (`steps=()`; None for an imported version);
      `MemoryNotFound` for an unknown id. `memories`: current states, one
      project or all (`None`), deleted only with `include_deleted`; bad rows
      are skipped. `citing`: every version of another memory citing any
      version of this one. `usage`: reads count and `last_read_at` per known
      id. `prune_reads(retention_days)`: drops older read facts, returns the
      count; never creates a store.
    - `scan(check)`: `check(text)` on the description and body of every stored
      version of every memory (deleted ones and all history included); one
      `PolicyHit` per hit version, never the text; no side effect.
    - `plan_hard_delete`: the target plus every memory with a stored version
      citing any version of a member, to a fixed point; referrers only.
      `hard_delete`: recomputes the plan in one write transaction and compares
      it exactly with `expected` or `code` (the other is None); a difference
      raises `PlanChanged(plan)` and deletes nothing; otherwise every member
      goes with its versions, sources, reads and steps, change rows stay.
      Both raise `MemoryNotFound` for an unknown id.
    - Errors carry fields, never words (see `models.errors`).
    """

    def apply(self, ops: Sequence[Op], *, changed_by: str, changed_via: str | None,
              check: Callable[[str, str], str | None]) -> Change: ...
    def change(self, change_id: str) -> Change | None: ...
    def undo(self, change_id: str, *, changed_by: str, changed_via: str | None,
             check: Callable[[str, str], str | None]) -> Change: ...
    def write(self, op: Op, *, restriction: ReadWriteSet, changed_by: str,
              changed_via: str | None, check: Callable[[str, str], str | None]) -> Memory: ...
    def read(self, memory_id: str, read_write_set: ReadWriteSet) -> Memory: ...
    def read_any(self, memory_id: str, *, include_deleted: bool) -> Memory: ...
    def touch_read(self, memory_id: str, at: str, *, memory_version: int, harness: str,
                   session_id: str | None) -> None: ...
    def prune_reads(self, retention_days: int) -> int: ...
    def usage(self, memory_ids: Sequence[str]) -> dict[str, Usage]: ...
    def memories(self, project_id: str | None = None, *,
                 include_deleted: bool = False) -> list[Memory]: ...
    def versions(self, memory_id: str) -> list[MemoryVersion]: ...
    def citing(self, memory_id: str) -> list[Citation]: ...
    def scan(self, check: Callable[[str], str | None]) -> list[PolicyHit]: ...
    def plan_hard_delete(self, memory_id: str) -> HardDeletePlan: ...
    def hard_delete(self, memory_id: str, *, expected: frozenset[tuple[str, int]] | None,
                    code: str | None) -> list[str]: ...


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
      prompt text, the published summary, `entry_cwd` or `branch`; newest
      `last_active_at` first.
    - `record_call`: maps a harness's tool-call id to `key`'s session
      (replacing an earlier mapping of the same id), then drops every
      mapping recorded more than `retention_s` seconds before `at`, in the
      same transaction. A call id that is not a harness id (see
      `is_call_id`), a malformed `at` or a non-positive `retention_s` is a
      `ValueError`, and nothing is written.
    - `session_for_call`: the session a call id was mapped to, or None --
      also for an impossible call id and for a stored row whose session id
      is invalid. Read-only.
    - `bound`: registered rows whose project exists and is not global, newest
      `last_active_at` first; bad rows skipped. Read-only.
    - `publish_summary`: sets `summary = text`, `summary_at = at` on a bound
      row whose `last_active_at` still equals `expected_last_active_at`,
      leaving `last_active_at` alone; anything else -- an unknown, unbound,
      pending or moved session, or no store -- is `SessionMoved` and nothing
      is written. The other writes keep a published summary as it is.
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
    def bound(self) -> list[Session]: ...
    def publish_summary(self, key: SessionKey, text: str, *, expected_last_active_at: str,
                        at: str) -> None: ...
