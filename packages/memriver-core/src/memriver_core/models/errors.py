"""The stable error taxonomy shared by every backend and every transport.

Two kinds of error live here, and they differ in who owns the words:

- **Storage-boundary errors** -- `MemoryNotFound`, `ProjectNotFound`,
  `IdCollision`, `StorageFailure`, `VersionConflict`, `BindingRefused`,
  `ProjectUnavailable` -- carry structured *fields* only. Their `str()` is a
  developer-facing line for logs and must never reach a client: a transport
  composes client copy from the operation plus these fields, so a second
  backend cannot change a byte of what a client sees, nor leak SQL, driver or
  path detail through a message it happened to author.
- **Application/policy errors** -- `ContentRejected`, `GlobalReadOnly` --
  carry a message authored inside the core, where the wording *is* the rule
  being explained and is written to be client-safe (it never echoes the
  rejected value). Transports may forward these verbatim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .changes import HardDeletePlan


class MemoryError(Exception): ...            # base (namespaced; no builtins clash in-package)


class MemoryNotFound(MemoryError):
    """No memory the caller may read answers to `memory_id`.

    Absent, another project's, and orphaned (its project does not exist) are
    one answer, so a caller never reads another project's entry. A row that
    is present but cannot be read or decoded is `StorageFailure` instead:
    damage is reported as damage, not disguised as absence.
    """

    def __init__(self, memory_id: str) -> None:
        super().__init__(f"memory not found: {memory_id}")
        self.memory_id = memory_id


class ProjectNotFound(MemoryError):
    """No project answers to `project_id`."""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"project not found: {project_id}")
        self.project_id = project_id


class IdCollision(MemoryError):
    """A freshly generated id is already taken; nothing was written.

    Raised by a store's atomic create and caught by the application facade,
    which converts it to StorageFailure on this, its first occurrence: it
    never reaches a transport, so it is not part of the public facade.
    """

    def __init__(self, identifier: str) -> None:
        super().__init__(f"id collision: {identifier}")
        self.identifier = identifier


class ContentRejected(MemoryError):
    """From the content policy; the message is the rule, the fields name it.

    `rule_id` is the policy's id for what matched -- a vendored rule id, or
    "empty", "too-large", "invalid-harness" -- never the matched text.
    `memory_id` names the memory whose resulting state was refused (None for
    a memory being created, or a text checked outside any memory). Raised
    without a message, the message is composed from the rule id alone.
    """

    def __init__(self, message: str = "", *, rule_id: str = "",
                 memory_id: str | None = None) -> None:
        super().__init__(message or f"content rejected ({rule_id}); no change was made")
        self.rule_id = rule_id
        self.memory_id = memory_id


class ProjectUnavailable(MemoryError):
    """No project this session may use; nothing was written.

    Fields only: `reason` names the cause where one applies (for example
    "candidate-changed"), "" where the caller's context already says why.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(f"project unavailable: {reason}")
        self.reason = reason


class GlobalReadOnly(MemoryError):
    """The global project is read-only to agents; no mutation reaches it."""

    def __init__(self) -> None:
        super().__init__("global memories are read-only to agents; no change was made")


class StorageFailure(MemoryError):
    """The backend failed for an infrastructure reason.

    Deliberately fieldless: paths, errno text, SQL, and driver messages must
    not cross this boundary in any form. The originating exception stays on
    `__cause__` for logs.
    """

    def __init__(self) -> None:
        super().__init__("storage failure")


class VersionConflict(MemoryError):
    """The memory is readable and writable, but not at the version the caller read.

    Nothing was written. The caller reads again and redoes its edit; the
    store never replays the caller's text onto the newer row.
    """

    def __init__(self, memory_id: str) -> None:
        super().__init__(f"version conflict: {memory_id}")
        self.memory_id = memory_id


BINDING_REASONS = frozenset({
    "not-a-directory", "covers-home", "covers-store", "inside-store", "bound-elsewhere",
    "unverifiable", "is-global", "has-directory", "plan-changed", "binding-changed",
    "no-such-project",
})


class BindingRefused(MemoryError):
    """A directory could not be planned, bound or unbound; nothing was written.

    Fields only: `reason` is one of BINDING_REASONS, `project_id` names the
    other project for "bound-elsewhere". The CLI owns every sentence.
    """

    def __init__(self, reason: str, project_id: str | None = None) -> None:
        if reason not in BINDING_REASONS:
            raise ValueError(f"unknown binding reason: {reason!r}")
        super().__init__(f"binding refused: {reason}")
        self.reason = reason
        self.project_id = project_id


BATCH_CONFLICT_REASONS = frozenset({
    "version", "deleted", "same-state", "source", "cycle", "read-since", "missing",
})


class BatchConflict(MemoryError):
    """One operation of an `apply` failed its check inside the transaction; nothing was written.

    Fields only: `index` is the operation's position in the batch,
    `memory_id` the memory it names (None for a create), `reason` one of
    BATCH_CONFLICT_REASONS.
    """

    def __init__(self, index: int, memory_id: str | None, reason: str) -> None:
        if reason not in BATCH_CONFLICT_REASONS:
            raise ValueError(f"unknown batch conflict reason: {reason!r}")
        super().__init__(f"batch conflict: {reason} at operation {index}")
        self.index = index
        self.memory_id = memory_id
        self.reason = reason


class StoreNeedsUpgrade(MemoryError):
    """The store's schema is older than this version reads; nothing was read or written.

    Fields only: `version` is the store's schema version. Only the offline
    rebuild (`memriver upgrade`) changes such a file.
    """

    def __init__(self, version: int) -> None:
        super().__init__(f"store needs upgrade: schema {version}")
        self.version = version


UNDO_REFUSED_REASONS = frozenset({"not-found", "hard-deleted", "changed"})


class UndoRefused(MemoryError):
    """A change cannot be undone; nothing was written.

    Fields only: `reason` is one of UNDO_REFUSED_REASONS; `memory_ids` names
    the memories that changed since, for "changed".
    """

    def __init__(self, reason: str, memory_ids: tuple[str, ...] = ()) -> None:
        if reason not in UNDO_REFUSED_REASONS:
            raise ValueError(f"unknown undo refusal: {reason!r}")
        super().__init__(f"undo refused: {reason}")
        self.reason = reason
        self.memory_ids = memory_ids


class PlanChanged(MemoryError):
    """A hard delete's plan differs from the one confirmed; nothing was deleted.

    Fields only: `plan` is the plan as it stands now, for the caller to show again.
    """

    def __init__(self, plan: HardDeletePlan) -> None:
        super().__init__(f"plan changed: {plan.target}")
        self.plan = plan


class SessionMoved(MemoryError):
    """The session is unknown, not bound to a project, or active since it was read.

    Nothing was written; the caller reads the session again. Deliberately fieldless.
    """

    def __init__(self) -> None:
        super().__init__("session moved")


UPGRADE_REASONS = frozenset({
    "upgrade-running", "counts", "invariant", "foreign-keys", "schema",
})


class UpgradeRefused(MemoryError):
    """The store was not rebuilt as schema v4; the live file is exactly as it was.

    Fields only: `reason` is one of UPGRADE_REASONS -- another upgrade holds the
    upgrade lock ("upgrade-running"), or the new file failed one verification
    check (the other four). The CLI owns every sentence.
    """

    def __init__(self, reason: str) -> None:
        if reason not in UPGRADE_REASONS:
            raise ValueError(f"unknown upgrade reason: {reason!r}")
        super().__init__(f"upgrade refused: {reason}")
        self.reason = reason
