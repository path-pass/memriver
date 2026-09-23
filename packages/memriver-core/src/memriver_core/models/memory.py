from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from .helpers import ID_RE, new_id, now

# Claude Code's auto-memory taxonomy, adopted verbatim (docs/memory-model.md):
#   user       who the user is: role, expertise, preferences
#   feedback   guidance on how to work: corrections and confirmed approaches
#   project    ongoing work, goals, constraints not derivable from the code
#   reference  pointers to external resources: URLs, dashboards, tickets
# The `project` type names a kind of fact; it is unrelated to the Project a
# memory belongs to.
MemoryType = Literal["user", "feedback", "project", "reference"]

# Provenance tier of an entry, graded by how trustworthy its SOURCE MATERIAL is
# (not by which code path wrote it):
#   user               the user stated it explicitly
#   agent              an agent judged it worth keeping while working (default)
#   untrusted-derived  derived from content that entered the context from
#                      outside: web pages, third-party code, tool output, logs
Trust = Literal["user", "agent", "untrusted-derived"]


@dataclass
class Memory:
    id: str
    project_id: str
    type: str
    source: dict
    trust: str
    sync: bool
    created: str
    updated: str
    description: str
    body: str

    @classmethod
    def new(cls, *, body: str, type: str, project_id: str, source: dict,
            trust: str = "agent", sync: bool = True, description: str = "") -> Memory:
        if type not in get_args(MemoryType):
            raise ValueError(f"invalid memory type: {type!r}")
        if trust not in get_args(Trust):
            raise ValueError(f"invalid trust: {trust!r}")
        if not ID_RE.fullmatch(project_id):
            raise ValueError("invalid project id")
        timestamp = now()
        return cls(id=new_id(), project_id=project_id, type=type, source=dict(source),
                   trust=trust, sync=sync, created=timestamp, updated=timestamp,
                   description=description.strip(), body=body.strip())
