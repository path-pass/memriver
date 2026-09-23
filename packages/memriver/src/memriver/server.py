from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from memriver_core import (
    ContentRejected,
    GlobalReadOnly,
    MemoryNotFound,
    ProjectUnavailable,
)
from memriver_core.bootstrap import build_service
from memriver_core.config import Settings
from memriver_core.models import ID_RE, Memory

from .project_context import resolve
from .protocol_text import INSTRUCTIONS
from .session import open_session

# read, update, delete and write map the same application errors to different
# client-visible strings, so the exception type alone cannot decide the
# response -- every call site passes the operation it is translating for.
# "list" covers memory_index/memory_search: neither names a single entry, so
# any failure there is reported the same way.
Operation = Literal["read", "write", "update", "delete", "list"]

_COULD_NOT_READ_STORE = "could not read the memory store"

# The global project is readable from every context and writable from none, so
# one refusal covers update and delete: it tells the agent to stop rather than
# look for another way in.
_GLOBAL_READ_ONLY = ("global memories are read-only to agents; no change was made. Tell "
                     "the user; do not retry through another entry or edit the store directly.")

# Why there is no writable project, and what the agent should ask for -- keyed
# by the session state, never by a path.
_NO_PROJECT = {
    "none": ("no writable project: this directory is not registered. No memory was saved. "
             "Ask the user to choose a project root and run memriver project init; do not "
             "run it yourself."),
    "degraded": ("no writable project: the project registry is invalid. No memory was "
                 "saved. Ask the user to run memriver project explain."),
    "missing": ("no writable project: the registered project does not exist in the store. "
                "No memory was saved. Ask the user to run memriver project explain."),
    "unavailable": ("no writable project: the memory store could not be read. No memory "
                    "was saved. Ask the user to run memriver doctor."),
}
# a server that started registered outlives a hand-deleted project file: the
# directory is still registered, so the refusal is the missing-project one
_NO_PROJECT["registered"] = _NO_PROJECT["missing"]

_SNIPPET_CHARS = 60


def _map_error(operation: Operation, err: Exception, *, memory_id: str | None = None,
               session_state: str | None = None) -> dict:
    """Application error -> the tool's error dict. Tools never raise.

    Every client-visible string for a storage-boundary error is written here,
    from (operation, error type, structured fields): a second backend raising
    the same error produces the same response byte for byte, and cannot leak
    a path, an errno or a driver message into one.
    """
    if operation == "list":
        return {"error": _COULD_NOT_READ_STORE}
    if isinstance(err, GlobalReadOnly):
        return {"error": _GLOBAL_READ_ONLY}
    if operation == "write":
        if isinstance(err, ProjectUnavailable):
            return {"error": _NO_PROJECT.get(session_state or "none", _NO_PROJECT["none"])}
        # a codec failure (a lone surrogate) is a ValueError whose message is
        # the codec's, not policy copy
        if isinstance(err, ContentRejected | ValueError) and not isinstance(err, UnicodeError):
            # policy copy is authored in the core and never echoes the value;
            # ValueError reaches here from the model constructors
            return {"error": str(err)}
        return {"error": "could not write entry"}
    if isinstance(err, ContentRejected):
        return {"error": str(err)}
    # the id is the caller's string: echo it only when it is a well-formed id,
    # so newlines, injected text or unencodable characters never come back
    suffix = f": {memory_id}" if memory_id is not None and ID_RE.fullmatch(memory_id) else ""
    if isinstance(err, MemoryNotFound):
        return {"error": f"no such entry{suffix}"}
    if operation == "read":
        return {"error": f"could not read entry{suffix}"}
    if operation == "update":
        return {"error": f"could not update entry{suffix}"}
    return {"error": f"could not delete entry{suffix}"}


def _full(memory: Memory) -> dict:
    return {"id": memory.id, "project_id": memory.project_id, "type": memory.type,
            "source": memory.source, "trust": memory.trust, "sync": memory.sync,
            "created": memory.created, "updated": memory.updated,
            "description": memory.description, "body": memory.body}


def _hit(memory: Memory, collection: str) -> dict:
    body = memory.body
    snippet = body if len(body) <= _SNIPPET_CHARS else body[:_SNIPPET_CHARS] + "…"
    return {"id": memory.id, "collection": collection, "type": memory.type,
            "description": memory.description, "snippet": snippet}


def build_server(root: Path, project_dir: Path,
                 settings: Settings | None = None) -> FastMCP:
    """Build the MCP server bound to `root`/`project_dir`.

    `root` stays an explicit argument -- callers that already resolved it (the
    CLI, the tests) must not have it re-read from the environment here. Only
    the behaviour knobs come from `settings`; when it is None a bare
    `Settings()` supplies them from the environment and the built-in defaults.
    """
    settings = settings if settings is not None else Settings()
    service = build_service(settings, root=root)
    # resolved once, at build time: every tool answers for the same project for
    # the life of the server, and the header cannot drift between calls. The
    # hook resolves on every call, so the two can still disagree (documented).
    session = open_session(service, resolve(root, project_dir))
    ctx = session.ctx

    mcp = FastMCP("memriver", instructions=INSTRUCTIONS)

    # Tools stay async so their bodies run inline on the event loop thread;
    # they are local and fast, with no await points, so calls serialize
    # without extra locking.

    @mcp.tool
    async def memory_index() -> str:
        """The session's project on the first line, then a compact index of the
        current project's memories followed by global's."""
        try:
            return session.header + "\n" + service.index(ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("list", err)["error"]

    @mcp.tool
    async def memory_read(memory_id: str) -> dict:
        """Read one memory in full by id."""
        try:
            return _full(service.read(memory_id, ctx))
        except Exception as err:  # noqa: BLE001
            return _map_error("read", err, memory_id=memory_id)

    @mcp.tool
    async def memory_search(query: str, limit: int | None = None) -> list[dict]:
        """Search the current project's memories, then global's. `limit` caps the
        whole answer: project hits first, global fills what is left."""
        try:
            # one clamp for the whole answer, then two single-project searches;
            # a spent budget skips global rather than being clamped back to 1
            remaining = service.normalize_search_limit(limit)
            hits: list[dict] = []
            for collection, project_id in (("project", ctx.project_id),
                                           ("global", ctx.global_project_id)):
                if project_id is None or remaining == 0:
                    continue
                found = service.search(project_id, query, ctx, remaining)
                hits += [_hit(m, collection) for m in found]
                remaining -= len(found)
            return hits
        except Exception as err:  # noqa: BLE001
            return [_map_error("list", err)]

    @mcp.tool
    async def memory_write(content: str,
                           type: Literal["user", "feedback", "project", "reference"],
                           sync: bool = True, harness: str = "unknown",
                           description: str = "") -> dict:
        """Save one durable fact to the current project's memory; memriver assigns the id.
        Global memories are read-only to agents.
        type: user = who the user is; feedback = how they want you to work;
        project = ongoing work/constraints; reference = external resources.
        description: one-line summary shown in the index; when should a future
        session recall this?"""
        try:
            memory = service.record(content=content, type=type, sync=sync, harness=harness,
                                    description=description, ctx=ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("write", err, session_state=session.state)
        return {"id": memory.id, "project_id": memory.project_id}

    @mcp.tool
    async def memory_update(memory_id: str, content: str,
                            description: str | None = None) -> dict:
        """Rewrite a memory's content in place; id, project and type stay.
        Global entries are read-only; the call is refused.
        description: omit to keep the existing one; pass a string to replace
        it, or "" to clear it."""
        try:
            memory = service.update(memory_id, content, ctx, description=description)
        except Exception as err:  # noqa: BLE001
            return _map_error("update", err, memory_id=memory_id)
        return {"id": memory.id, "updated": memory.updated}

    @mcp.tool
    async def memory_delete(memory_id: str) -> dict:
        """Delete a memory that is no longer true or no longer wanted.
        Global entries are read-only; the call is refused."""
        try:
            service.delete(memory_id, ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("delete", err, memory_id=memory_id)
        return {"deleted": memory_id}

    return mcp
