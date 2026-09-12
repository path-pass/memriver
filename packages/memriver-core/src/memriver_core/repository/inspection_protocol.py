from __future__ import annotations

from typing import Protocol

from memriver_core.models import StoreReport


class StoreInspector(Protocol):
    def inspect(self) -> StoreReport: ...
