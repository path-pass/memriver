from __future__ import annotations

from dataclasses import dataclass

from .helpers import new_id, single_line

# the same budget the project header gives any other single field
PROJECT_NAME_MAX_CHARS = 120


def project_name(raw: str) -> str:
    """A readable project name: one line, non-empty, capped. Never an identity."""
    name = single_line(raw)
    if not name:
        raise ValueError("project name is empty")
    if len(name) > PROJECT_NAME_MAX_CHARS:
        raise ValueError(f"project name is longer than {PROJECT_NAME_MAX_CHARS} characters")
    return name


@dataclass(frozen=True)
class Project:
    """A named collection of memories. Membership lives on Memory.project_id."""

    id: str
    name: str

    @classmethod
    def new(cls, name: str) -> Project:
        return cls(id=new_id(), name=project_name(name))
