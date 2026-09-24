from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .project import Project
from .read_write_set import ReadWriteSet
from .session import SessionKey

ProjectContextState = Literal["registered", "none", "degraded", "unavailable", "pending",
                              "unidentified"]


@dataclass(frozen=True)
class ProjectContext:
    """One resolved directory or session as every surface states it: header, rights, project.

    `session_key` is set when the context came from a session, so a save can
    move that session's watermark without the caller naming it twice.
    """

    state: ProjectContextState
    header: str
    read_write_set: ReadWriteSet
    project: Project | None = None
    diagnostic: str | None = None
    session_key: SessionKey | None = None
