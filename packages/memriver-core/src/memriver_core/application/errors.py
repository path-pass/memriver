from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memriver_core.models import Memory


class MemoryError(Exception): ...            # base (namespaced; no builtins clash in-package)


class MemoryNotFound(MemoryError): ...       # replaces EntryNotFound


class UnreadableMemory(MemoryError): ...     # stored item cannot be decoded / mismatched


class NameTaken(MemoryError):                # carries .existing: Memory | None
    def __init__(self, message: str, existing: Memory | None = None) -> None:
        super().__init__(message)
        self.existing = existing            # None => cross-scope refusal (no echo allowed)


class ContentRejected(MemoryError): ...      # from ContentPolicy (today's GateError; keeps
                                              #  the non-echoing rejection message)


class InvalidScope(MemoryError): ...


class ProjectUnavailable(MemoryError): ...


class StorageFailure(MemoryError): ...       # wraps OSError-class failures; message must
                                              #  never contain paths or OS error text
