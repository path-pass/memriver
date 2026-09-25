"""The application layer: MemoryService and MaintenanceService, and what they share."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memriver_core.content_policy.protocol import ContentPolicy


class LazyPolicy:
    """Builds a `ContentPolicy` from its factory once, on first use.

    Shared by MemoryService and MaintenanceService so a read-only caller (the
    Stop hook, doctor, a maintenance read) never pays for loading and
    compiling the scanner rules.
    """

    def __init__(self, factory: Callable[[], ContentPolicy]) -> None:
        self._factory = factory
        self._policy: ContentPolicy | None = None

    def get(self) -> ContentPolicy:
        if self._policy is None:
            self._policy = self._factory()
        return self._policy
