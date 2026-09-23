from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .project import Project

ResolutionState = Literal["registered", "none", "degraded"]


@dataclass(frozen=True)
class Resolution:
    """Which project a directory belongs to.

    `project` only when registered; `diagnostic` (fixed wording, may name a
    registered root) only when degraded.
    """

    state: ResolutionState
    project: Project | None = None
    diagnostic: str | None = None
