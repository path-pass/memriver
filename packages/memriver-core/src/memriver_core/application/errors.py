"""The stable error taxonomy shared by every backend and every transport.

Two kinds of error live here, and they differ in who owns the words:

- **Storage-boundary errors** -- `MemoryNotFound`, `UnreadableMemory`,
  `NameTaken`, `StorageFailure` -- carry structured *fields* only. Their
  `str()` is a developer-facing line for logs and must never reach a client:
  a transport composes client copy from the operation plus these fields. That
  is what makes a backend swap invisible -- a second implementation cannot
  change a byte of what a client sees, nor leak SQL, driver, or path detail
  through a message it happened to author.
- **Application/policy errors** -- `ContentRejected`, `InvalidScope`,
  `ProjectUnavailable`, `GlobalReadOnly` -- carry a message authored inside
  the core, where the wording *is* the rule being explained and is written to
  be client-safe (it never echoes the rejected value). Transports may forward
  these verbatim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memriver_core.models import Memory


class MemoryError(Exception): ...            # base (namespaced; no builtins clash in-package)


class MemoryNotFound(MemoryError):
    """No memory answers to `memory_id` in the caller's visible scopes."""

    def __init__(self, memory_id: str) -> None:
        super().__init__(f"memory not found: {memory_id}")
        self.memory_id = memory_id


class UnreadableMemory(MemoryError):
    """`memory_id` is occupied, but the stored item cannot be decoded."""

    def __init__(self, memory_id: str) -> None:
        super().__init__(f"unreadable memory: {memory_id}")
        self.memory_id = memory_id


class NameTaken(MemoryError):
    """`memory_id` is already in use; `existing` is the memory holding it.

    Always populated: a name reservation searches the caller's visible scopes
    and nothing else, so whatever holds the name is by construction something
    the caller may already read. There is no refusal left that has to withhold
    the colliding memory, and no caller may omit it.
    """

    def __init__(self, memory_id: str, existing: Memory) -> None:
        super().__init__(f"name taken: {memory_id}")
        self.memory_id = memory_id
        self.existing = existing


class ContentRejected(MemoryError): ...      # from ContentPolicy; the message is the rule


class InvalidScope(MemoryError): ...


class ProjectUnavailable(MemoryError): ...


class GlobalReadOnly(MemoryError):
    """The global scope is read-only to agents; no mutation reaches it.

    Raised by the repository for any create/update/delete whose target is a
    global entry. The message is the rule, written client-safe, so transports
    forward it. (The service never aims at global: without a scope input it
    writes the context project or raises ProjectUnavailable.)
    """

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
