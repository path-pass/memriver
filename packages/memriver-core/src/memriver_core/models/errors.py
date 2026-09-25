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
    """From ContentPolicy; the message is the rule.

    `rule_id` names the secret rule that matched, when one did -- an id from
    the vendored ruleset, never the matched text; None for an empty or
    oversized value.
    """

    def __init__(self, message: str, *, rule_id: str | None = None) -> None:
        super().__init__(message)
        self.rule_id = rule_id


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


class MemoryReferenced(MemoryError):
    """A hard delete named a memory that derived entries cite as a source; nothing was deleted.

    Fields only: `derived_ids` are the citing entries, active or deleted. The
    CLI owns the sentence that tells the user to delete them first.
    """

    def __init__(self, memory_id: str, derived_ids: tuple[str, ...]) -> None:
        super().__init__(f"memory referenced: {memory_id}")
        self.memory_id = memory_id
        self.derived_ids = derived_ids


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


class GroupConflict(MemoryError):
    """A change group's precondition failed inside its transaction; nothing was written.

    Fields only: `ids` are the rows (or projects) that failed a check;
    `change_id` is None because nothing was applied.
    """

    def __init__(self, change_id: str | None, ids: tuple[str, ...]) -> None:
        super().__init__(f"group conflict: {', '.join(ids)}")
        self.change_id = change_id
        self.ids = ids


class UndoConflict(MemoryError):
    """A row of the change group moved since it was applied; nothing was restored.

    Fields only: `ids` are the rows no longer at the version the group left.
    """

    def __init__(self, change_id: str, ids: tuple[str, ...]) -> None:
        super().__init__(f"undo conflict: {change_id}")
        self.change_id = change_id
        self.ids = ids
