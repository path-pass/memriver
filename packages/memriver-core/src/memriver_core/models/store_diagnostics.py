from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .memory import Memory

DiagnosticsState = Literal["uninitialized", "empty", "healthy", "degraded"]


@dataclass(frozen=True)
class InspectedMemory:
    memory: Memory
    location_hint: str


RootState = Literal["ok", "missing", "not-canonical", "unverifiable", "unbound"]


@dataclass(frozen=True)
class InspectedProject:
    id: str
    name: str
    root: str | None
    is_global: bool
    root_state: RootState
    active_memories: int
    deleted_memories: int


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
    projects: tuple[InspectedProject, ...]
    findings: tuple[StoreFinding, ...]
    # (derived_id, source_id) for every active memory's effective source set (spec
    # §3.2's "effective set" -- the set recorded at the greatest version not above
    # the memory's current one); lets a policy tell a dream-kept original apart
    # from an unrelated near-duplicate without touching SQL itself
    sources: frozenset[tuple[str, str]] = frozenset()


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
    # a finding outranks "uninitialized" in `state`, so whether the global
    # project exists is carried on its own
    initialized: bool = True
    # the inspector's projects, passed through so doctor renders them from
    # the one diagnostics entry
    projects: tuple[InspectedProject, ...] = ()
