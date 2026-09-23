from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .memory import Memory

DiagnosticsState = Literal["uninitialized", "empty", "healthy", "degraded"]


@dataclass(frozen=True)
class InspectedMemory:
    memory: Memory
    location_hint: str


@dataclass(frozen=True)
class StoreFinding:
    kind: str
    project_id: str | None
    location_hint: str
    memory_id: str | None
    reason: str


@dataclass(frozen=True)
class StoreReport:
    initialized: bool
    entries: tuple[InspectedMemory, ...]
    projects: tuple[str, ...]
    findings: tuple[StoreFinding, ...]


@dataclass(frozen=True)
class DiagnosticFinding:
    kind: str
    memory_ids: tuple[str, ...]
    project_ids: tuple[str, ...]
    location_hints: tuple[str, ...]
    reason: str
    suggestion: str


@dataclass(frozen=True)
class DiagnosticsReport:
    state: DiagnosticsState
    findings: tuple[DiagnosticFinding, ...]
    # a finding outranks "uninitialized" in `state`, so whether the manifest
    # exists is carried on its own
    initialized: bool = True
