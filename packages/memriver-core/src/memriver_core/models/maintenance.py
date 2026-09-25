"""Records the maintenance run (dream) reads and writes: provenance, reviews, change groups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
