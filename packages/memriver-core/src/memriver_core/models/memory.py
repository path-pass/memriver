from __future__ import annotations

import json
import re
from collections.abc import Iterable
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

# a tag name is injected into the index and typed back into memory_index(tag=...):
# lowercase ascii words joined by hyphens, at most 32 characters
TAG_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TAG_NAME_MAX_CHARS = 32


def checked_level(level: object) -> int | None:
    """`level` as stored: None (not judged) or an integer of at least 1 (1 = standing);
    ValueError for anything else, a boolean included."""
    if level is None or (type(level) is int and level >= 1):
        return level
    raise ValueError("a level is None or an integer of at least 1")


def checked_tags(tags: Iterable[object]) -> tuple[str, ...]:
    """`tags` as stored: distinct valid names, sorted; ValueError for a bad or repeated one."""
    names = tuple(tags)
    if not all(isinstance(name, str) and len(name) <= TAG_NAME_MAX_CHARS
               and TAG_NAME_RE.fullmatch(name) for name in names):
        raise ValueError("a tag name is 1 to 32 of a-z, 0-9 and single inner hyphens")
    if len(set(names)) != len(names):
        raise ValueError("a memory carries a tag name at most once")
    return tuple(sorted(names))


def tags_to_json(tags: tuple[str, ...]) -> str:
    return json.dumps(list(tags), separators=(",", ":"))


def tags_from_json(text: object) -> tuple[str, ...]:
    """A stored tags column; ValueError for one memriver could not have written."""
    if not isinstance(text, str):
        raise ValueError("tags is not text")  # noqa: TRY004 - a row check, as memory_from_row
    try:
        names = json.loads(text)
    except ValueError:
        raise ValueError("tags is not JSON") from None
    if not isinstance(names, list):
        raise ValueError("tags is not a JSON array")  # noqa: TRY004
    return checked_tags(names)


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
    # optimistic concurrency: every update and delete moves it on, and a
    # write names the version it read (spec §5.1)
    version: int = 1
    # set only on the management reads; agent-facing paths never return a
    # deleted memory (spec §4)
    deleted_at: str | None = None
    # last successful memory_read, moved forward by MemoryStore.touch_read;
    # None = never read since the v2 upgrade. System metadata, not shown to
    # agents: memory_read's eleven fields are unchanged (spec §4)
    last_read_at: str | None = None
    # metadata a management write sets and every version records; never shown to agents
    # as fields (memory_read's eleven are unchanged): 1 = a standing rule, larger is
    # lower, None = not judged; tags are sorted names
    level: int | None = None
    tags: tuple[str, ...] = ()

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
