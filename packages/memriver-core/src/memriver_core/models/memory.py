from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

from .helpers import ID_RE, new_id

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


def now() -> str:
    # microseconds, not seconds: `updated` is the recency sort key, and a
    # same-second update must still advance it
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_strictly_after(previous: str) -> str:
    """`now()`, forced past `previous` when the clock has not moved on.

    Resolution alone does not guarantee an advance: the clock's own tick can
    be coarser than two consecutive writes, and it can step backwards. Since
    `updated` is the recency sort key, a rewrite that landed on -- or before
    -- the value it replaces would let the older body sort as the newer one.
    A `previous` outside the canonical form carries no comparable instant, so
    there the plain clock reading is all there is; diagnostics reports that
    value separately.
    """
    stamp = now()
    try:
        earliest = (datetime.strptime(previous, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
                    + timedelta(microseconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, OverflowError):
        return stamp
    # both are the same fixed-width form, so lexicographic order is chronological
    return max(stamp, earliest)


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
