"""Records the maintenance run (dream) reads and writes: provenance, reviews, change groups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .memory import Memory

ChangeKind = Literal["merge", "rewrite", "extract", "retire", "secret", "unsafe"]
Decision = Literal["keep", "delete", "uncertain"]
RunTrigger = Literal["schedule", "manual"]
RunStatus = Literal["running", "completed", "failed", "skipped"]


@dataclass(frozen=True)
class SourceRef:
    """One source a derived entry was built from, as it stood at `source_version`."""

    source_id: str
    source_version: int
    source_project: str
    snapshot: dict          # {"type", "description", "body"} of the source at source_version


@dataclass(frozen=True)
class Review:
    """The latest TTL decision on one memory."""

    memory_id: str
    memory_version: int
    decided_at: str
    decision: Decision
    reason: str
    uncertain_streak: int
    next_review_at: str
    run_id: str
    executor: str
    prompt_version: str


@dataclass(frozen=True)
class ChangeRow:
    """One memory a change group touched: its before-image and effective source set
    (both None: the group created it) and the version the group left it at."""

    id: str
    before: dict | None
    before_sources: tuple[SourceRef, ...] | None
    after_version: int


@dataclass(frozen=True)
class Change:
    """One applied change group, as the change log holds it."""

    change_id: str
    run_id: str
    kind: ChangeKind
    project_id: str
    applied_at: str
    rows: tuple[ChangeRow, ...]
    reason: str
    undone_at: str | None


@dataclass(frozen=True)
class DreamRun:
    """One maintenance run as recorded: `report` holds per phase its counts and the
    items the report prints -- ids, kinds and outcomes, never a body, summary,
    prompt or secret."""

    run_id: str
    started_at: str
    finished_at: str | None
    trigger: RunTrigger
    executor: str | None      # None: no executor configured (the safety re-scan only)
    status: RunStatus
    report: dict


@dataclass(frozen=True)
class Candidate:
    """A memory past its effective TTL and not covered by a review, with its latest
    review (None: never reviewed) and its recorded read count."""

    memory: Memory
    review: Review | None
    reads: int


def effective_ttl_days(ttl_days: int, reads: int, multiplier_max: int) -> int:
    """Every recorded read lengthens a memory's TTL by one base TTL, up to the cap."""
    return ttl_days * min(1 + reads, multiplier_max)


@dataclass(frozen=True)
class CreateOp:
    """Create one derived memory in `project_id` from `(source_id, source_version)` pairs."""

    project_id: str
    type: str
    description: str
    body: str
    sources: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class UpdateOp:
    """Rewrite one memory at `expected_version`. `sources` are the sources this update
    newly consumes; the ones the memory already had are carried forward by core."""

    id: str
    expected_version: int
    description: str
    body: str
    sources: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SoftDeleteOp:
    id: str
    expected_version: int


Operation = CreateOp | UpdateOp | SoftDeleteOp


@dataclass(frozen=True)
class ChangeGroup:
    """One operation applied, and undone, as a unit: the unit of the change log."""

    run_id: str
    kind: ChangeKind          # merge | rewrite | extract | unsafe (retire, secret: own writes)
    project_id: str           # the project it was planned for; global for extract
    reason: str               # the model's one-line reason
    harness: str              # source_harness of every row it writes
    ops: tuple[Operation, ...]


@dataclass(frozen=True)
class UndoResult:
    change_id: str
    status: Literal["undone", "not-found", "already-undone"]
    ids: tuple[str, ...]      # the rows restored (empty unless undone)
