from __future__ import annotations

from dataclasses import dataclass

from .helpers import new_id, single_line


def project_name(raw: str, max_chars: int) -> str:
    """A readable project name: one line, non-empty, capped. Never an identity.

    The cap is a settings constant injected by the caller: models import the
    standard library only.
    """
    name = single_line(raw)
    if not name:
        raise ValueError("project name is empty")
    if len(name) > max_chars:
        raise ValueError(f"project name is longer than {max_chars} characters")
    return name


@dataclass(frozen=True)
class Project:
    """A named collection of memories, bound to at most one directory.

    Membership lives on Memory.project_id. `root` is the canonical directory a
    harness resolves to this project, or None (global, or unbound).
    """

    id: str
    name: str
    root: str | None = None

    @classmethod
    def new(cls, name: str, *, max_chars: int) -> Project:
        return cls(id=new_id(), name=project_name(name, max_chars))


@dataclass(frozen=True)
class RootPlan:
    """A directory checked for binding, as shown to the user before confirmation.

    `already_bound` is for display only; execution decides on the current row.
    """

    root: str
    store: str
    nested: tuple[Project, ...]
    already_bound: bool


@dataclass(frozen=True)
class UnbindPlan:
    """The exact binding a confirmation showed, and the store it was read from."""

    project_id: str
    root: str
    store: str
