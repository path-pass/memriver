from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .project import Project
from .read_write_set import ReadWriteSet

SessionState = Literal["registered", "none", "degraded", "unavailable"]


@dataclass(frozen=True)
class Session:
    """One resolved directory as every surface states it: header, rights, project."""

    state: SessionState
    header: str
    read_write_set: ReadWriteSet
    project: Project | None = None
    diagnostic: str | None = None
