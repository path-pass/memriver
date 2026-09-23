"""Where a registry resolution and the core store become one session.

The MCP server and the SessionStart hook both build their header and access
context here and nowhere else, so the same resolved directory always yields
the same header and the same readable projects. This does not make the two
agree on *which* directory they resolve: the server resolves its own start
directory once, the hook resolves the harness's directory on every call, and
they can still name different projects in one session (a documented, unfixed
limitation). The shared rendering only guarantees that each surface states
its own project accurately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from memriver_core import ProjectNotFound, StorageFailure
from memriver_core.models import AccessContext, Project

from .project_context import ProjectResolution, header_field

SessionState = Literal["registered", "missing", "none", "degraded", "unavailable"]

NONE_HEADER = "project: none — global is read-only; ask the user to run memriver project init"
STORE_UNREADABLE_HEADER = ("project: unavailable — the memory store could not be read; "
                           "ask the user to run memriver doctor")


@dataclass(frozen=True)
class Session:
    header: str
    ctx: AccessContext
    state: SessionState
    # the resolved Project, only in the "registered" state
    project: Project | None = None


def open_session(service, resolution: ProjectResolution) -> Session:
    """The header line and access context for one resolved directory. Never raises
    for a store problem: an unreadable store is an empty, clearly labelled session."""
    try:
        if resolution.state == "registered":
            ctx = service.access_context(resolution.project_id)
            project = None
            if ctx.project_id is not None:
                try:
                    project = service.read_project(ctx.project_id)
                except ProjectNotFound:
                    # removed between the two reads: missing, and no write right
                    ctx = AccessContext(project_id=None,
                                        global_project_id=ctx.global_project_id)
            if project is None:
                return Session(
                    f"project: unavailable — registered project "
                    f"{header_field(resolution.project_id or '')} does not exist; "
                    "ask the user to run memriver project explain",
                    ctx, "missing")
            return Session(
                f"project: {header_field(project.name)} [{project.id}] "
                f"(root {header_field(resolution.root or '')})",
                ctx, "registered", project)
        ctx = service.access_context(None)
    except StorageFailure:
        return Session(STORE_UNREADABLE_HEADER,
                       AccessContext(project_id=None, global_project_id=None), "unavailable")
    if resolution.state == "none":
        return Session(NONE_HEADER, ctx, "none")
    return Session(
        f"project: unavailable — registry invalid ({header_field(resolution.diagnostic or '')}); "
        "ask the user to run memriver project explain",
        ctx, "degraded")
