from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NewType, get_args

from ulid import ULID

# Claude Code's auto-memory taxonomy, adopted verbatim (docs/memory-model.md):
#   user       who the user is: role, expertise, preferences
#   feedback   guidance on how to work: corrections and confirmed approaches
#   project    ongoing work, goals, constraints not derivable from the code
#   reference  pointers to external resources: URLs, dashboards, tickets
MemoryType = Literal["user", "feedback", "project", "reference"]

# Provenance tier of an entry, graded by how trustworthy its SOURCE MATERIAL is
# (not by which code path wrote it):
#   user               the user stated it explicitly
#   agent              an agent judged it worth keeping while working (default)
#   untrusted-derived  derived from content that entered the context from
#                      outside: web pages, third-party code, tool output, logs
# Only "untrusted-derived" is unused today; it is reserved for the background
# distillation pipeline and for the promotion gate that keeps tainted memories
# out of shared storage. The vocabulary is fixed now because `trust` is part of
# the on-disk frontmatter, and widening it later would mean migrating files.
Trust = Literal["user", "agent", "untrusted-derived"]


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ids are either server-generated ULIDs (fallback) or sanitized kebab slugs;
# both shapes are safe as file stems, everything else is refused before globbing
ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}|[a-z0-9][a-z0-9-]{0,63}")


def sanitize_name(proposal: str) -> str | None:
    """Agent-proposed entry name -> permanent id, or None when unsalvageable.

    The agent only ever contributes a human-readable hint; shape, charset and
    length are the server's (docs/memory-model.md). Non-ASCII proposals fall
    back to None -> the caller uses a ULID instead.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", proposal.lower()).strip("-")[:64].rstrip("-")
    return slug if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug) else None


ProjectId = NewType("ProjectId", str)


@dataclass(frozen=True)
class Scope:
    project_id: ProjectId | None

    @classmethod
    def global_(cls) -> Scope:
        return cls(project_id=None)

    @classmethod
    def project(cls, project_id: ProjectId) -> Scope:
        return cls(project_id=project_id)

    @classmethod
    def parse(cls, raw: str) -> Scope:
        if raw == "global":
            return cls.global_()
        if raw.startswith("project:"):
            pid = raw.split(":", 1)[1]
            if not pid:
                raise ValueError(f"invalid scope: {raw!r}")
            return cls.project(ProjectId(pid))
        raise ValueError(f"invalid scope: {raw!r}")

    def to_storage(self) -> str:
        return "global" if self.project_id is None else f"project:{self.project_id}"


@dataclass
class Memory:
    id: str
    type: str
    scope: Scope
    sync: bool
    created: str
    updated: str
    source: dict
    trust: str
    description: str
    body: str

    @classmethod
    def new(cls, *, body: str, type: str, scope: Scope, source: dict,
            trust: str = "agent", sync: bool = True,
            id: str | None = None, description: str = "") -> Memory:
        if type not in get_args(MemoryType):
            raise ValueError(f"invalid memory type: {type!r}")
        if trust not in get_args(Trust):
            raise ValueError(f"invalid trust: {trust!r}")
        timestamp = now()
        return cls(id=id if id is not None else str(ULID()), type=type,
                   scope=scope, sync=sync, created=timestamp, updated=timestamp,
                   source=dict(source), trust=trust, description=description.strip(),
                   body=body.strip())


@dataclass(frozen=True)
class AccessContext:
    project_id: ProjectId | None

    def visible_scopes(self) -> tuple[Scope, ...]:
        if self.project_id is None:
            return (Scope.global_(),)
        return (Scope.global_(), Scope.project(self.project_id))


@dataclass
class SearchHit:
    id: str
    scope: Scope
    type: str
    snippet: str


@dataclass
class IndexListing:
    entries: tuple[Memory, ...]
