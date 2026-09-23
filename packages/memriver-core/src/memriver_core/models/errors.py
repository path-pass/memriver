"""The stable error taxonomy shared by every backend and every transport.

Two kinds of error live here, and they differ in who owns the words:

- **Storage-boundary errors** -- `MemoryNotFound`, `ProjectNotFound`,
  `IdCollision`, `StorageFailure` -- carry structured *fields* only. Their `str()` is a
  developer-facing line for logs and must never reach a client: a transport
  composes client copy from the operation plus these fields, so a second
  backend cannot change a byte of what a client sees, nor leak SQL, driver or
  path detail through a message it happened to author.
- **Application/policy errors** -- `ContentRejected`, `ProjectUnavailable`,
  `GlobalReadOnly` -- carry a message authored inside the core, where the
  wording *is* the rule being explained and is written to be client-safe (it
  never echoes the rejected value). Transports may forward these verbatim.
"""

from __future__ import annotations


class MemoryError(Exception): ...            # base (namespaced; no builtins clash in-package)


class MemoryNotFound(MemoryError):
    """No memory the caller may read answers to `memory_id`.

    Absent, another project's, and orphaned (its project does not exist) are
    one answer, so a caller never reads another project's entry. A file that
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


class ContentRejected(MemoryError): ...      # from ContentPolicy; the message is the rule


class ProjectUnavailable(MemoryError): ...   # no writable project in this context


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
