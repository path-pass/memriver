from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .memory import Memory, Scope

DiagnosticsState = Literal["uninitialized", "empty", "healthy", "degraded"]


@dataclass(frozen=True)
class InspectedMemory:
    memory: Memory
    location_hint: str


@dataclass(frozen=True)
class StoreFinding:
    kind: str
    scope: Scope | None
    location_hint: str
    memory_id: str | None
    reason: str


@dataclass(frozen=True)
class StoreReport:
    initialized: bool
    entries: tuple[InspectedMemory, ...]
    findings: tuple[StoreFinding, ...]


@dataclass(frozen=True)
class DiagnosticFinding:
    kind: str
    memory_ids: tuple[str, ...]
    scopes: tuple[Scope, ...]
    location_hints: tuple[str, ...]
    reason: str
    suggestion: str


@dataclass(frozen=True)
class DiagnosticsReport:
    state: DiagnosticsState
    findings: tuple[DiagnosticFinding, ...]
