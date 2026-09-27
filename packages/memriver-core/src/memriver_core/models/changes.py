"""Operations, changes and history (spec §3, §4): what `MemoryService.apply` takes and returns.

Every creation or state change of a memory is one operation of one change; a
change keeps a step per memory it touched, a memory keeps every version it
ever had. Values only: the rules live in the store and the services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OpName = Literal["create", "update", "soft_delete", "restore"]


@dataclass(frozen=True)
class SourceRef:
    """One cited version of another memory."""

    memory_id: str
    version: int


@dataclass(frozen=True)
class Create:
    project_id: str
    type: str
    description: str
    body: str
    sources: tuple[SourceRef, ...] = ()
    # ignored when there are sources: the store derives both from them (spec §3.3)
    trust: str = "agent"
    sync: bool = True


@dataclass(frozen=True)
class Update:
    """None keeps a value; `sources` replaces the whole set."""

    memory_id: str
    expected_version: int
    description: str | None = None
    body: str | None = None
    sources: tuple[SourceRef, ...] | None = None


@dataclass(frozen=True)
class SoftDelete:
    """With `unread_since`, also refused when the memory was read at or after that time."""

    memory_id: str
    expected_version: int
    unread_since: str | None = None


@dataclass(frozen=True)
class Restore:
    """Makes `to_version`'s whole recorded state the new current version."""

    memory_id: str
    expected_version: int
    to_version: int


Op = Create | Update | SoftDelete | Restore


@dataclass(frozen=True)
class Step:
    step: int
    memory_id: str
    op: OpName
    before_version: int | None
    after_version: int


@dataclass(frozen=True)
class Change:
    change_id: str
    at: str
    changed_by: str
    changed_via: str | None
    step_count: int
    undoes: str | None
    steps: tuple[Step, ...]          # the steps still stored (fewer after a hard delete)


@dataclass(frozen=True)
class MemoryVersion:
    memory_id: str
    version: int
    type: str
    trust: str
    sync: bool
    description: str
    body: str
    deleted: bool
    sources: tuple[SourceRef, ...]
    change: Change | None            # None = imported by the migration (steps omitted: ())


@dataclass(frozen=True)
class Usage:
    reads: int
    last_read_at: str | None


@dataclass(frozen=True)
class Citation:
    memory_id: str                    # the citing memory
    version: int                      # the citing version
    source_version: int               # the cited version of the queried memory
    current: bool                     # the citing version is its memory's current version


@dataclass(frozen=True)
class PolicyHit:
    memory_id: str
    version: int
    rule_id: str
    current: bool


@dataclass(frozen=True)
class PlanCitation:
    citing_id: str
    citing_version: int
    cited_id: str
    cited_version: int
    citing_current: bool


@dataclass(frozen=True)
class HardDeleteItem:
    memory_id: str
    project_id: str
    version: int
    deleted: bool
    citations: tuple[PlanCitation, ...]   # why it is in the plan (empty for the target)


@dataclass(frozen=True)
class HardDeletePlan:
    target: str
    items: tuple[HardDeleteItem, ...]     # target first, then by id
    code: str                              # 16 hex chars, spec §4.4

    @property
    def expected(self) -> frozenset[tuple[str, int]]:
        """The (memory_id, current version) set an interactive confirmation shows."""
        return frozenset((item.memory_id, item.version) for item in self.items)
