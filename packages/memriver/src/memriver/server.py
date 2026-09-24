from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from memriver_core import (
    ContentRejected,
    GlobalReadOnly,
    MemoryNotFound,
    ProjectUnavailable,
    VersionConflict,
)
from memriver_core.bootstrap import build_service
from memriver_core.models import ID_RE, Memory
from memriver_core.settings import SEARCH_SNIPPET_CHARS, Settings

from .protocol_text import INSTRUCTIONS

# read, update, delete and write map the same core errors to different
# client-visible strings, so the exception type alone cannot decide the
# response -- every call site passes the operation it is translating for.
# "list" covers memory_index/memory_search: neither names a single entry, so
# any failure there is reported the same way.
Operation = Literal["read", "write", "update", "delete", "list"]

_COULD_NOT_READ_STORE = "could not read the memory store"

# The global project is readable in every read/write set and writable in none, so
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
    "degraded": ("no writable project: this directory could not be matched to one project. "
                 "No memory was saved. Ask the user to run memriver project explain."),
    "unavailable": ("no writable project: the memory store could not be read. No memory "
                    "was saved. Ask the user to run memriver doctor."),
    # a server that started registered outlives a project removed from the
    # store behind its back
    "registered": ("no writable project: the registered project could not be found in the "
                   "store. No memory was saved. Ask the user to run memriver project explain."),
}


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
            # ValueError reaches here from the model constructors or from a
            # store's pre-write validation of the row it is about to write
            return {"error": str(err)}
        return {"error": "could not write entry"}
    if isinstance(err, ContentRejected):
        return {"error": str(err)}
    # the id is the caller's string: echo it only when it is a well-formed id,
    # so newlines, injected text or unencodable characters never come back
    valid = memory_id is not None and ID_RE.fullmatch(memory_id)
    suffix = f": {memory_id}" if valid else ""
    if isinstance(err, MemoryNotFound):
        return {"error": f"no such entry{suffix}"}
    if isinstance(err, VersionConflict) and operation in ("update", "delete"):
        subject = f"entry {memory_id}" if valid else "entry"
        return {"error": f"{subject} changed since you read it; no change was made. "
                         "Call memory_read again and retry with its version."}
    if operation == "read":
        return {"error": f"could not read entry{suffix}"}
    if operation == "update":
        return {"error": f"could not update entry{suffix}"}
    return {"error": f"could not delete entry{suffix}"}


def _full(memory: Memory) -> dict:
    # every field an agent may know; deletion state never leaves the core
    return {"id": memory.id, "project_id": memory.project_id, "type": memory.type,
            "source": memory.source, "trust": memory.trust, "sync": memory.sync,
            "created": memory.created, "updated": memory.updated,
            "description": memory.description, "body": memory.body,
            "version": memory.version}


def _hit(memory: Memory, collection: str) -> dict:
    body = memory.body
    snippet = body if len(body) <= SEARCH_SNIPPET_CHARS else body[:SEARCH_SNIPPET_CHARS] + "…"
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
    session = service.open_session(str(project_dir))
    read_write_set = session.read_write_set

    mcp = FastMCP("memriver", instructions=INSTRUCTIONS)

    # Tools stay async so their bodies run inline on the event loop thread;
    # they are local and fast, with no await points, so calls serialize
    # without extra locking.

    @mcp.tool
    async def memory_index() -> str:
        """The session's project on the first line, then a compact index of the
        current project's memories followed by global's."""
        try:
            return session.header + "\n" + service.index(read_write_set)
        except Exception as err:  # noqa: BLE001
            return _map_error("list", err)["error"]

    @mcp.tool
    async def memory_read(memory_id: str) -> dict:
        """Read one memory in full by id, including the version that
        memory_update and memory_delete must name."""
        try:
            return _full(service.read(memory_id, read_write_set))
        except Exception as err:  # noqa: BLE001
            return _map_error("read", err, memory_id=memory_id)

    @mcp.tool
    async def memory_search(query: str, limit: int | None = None) -> list[dict]:
        """Search the current project's memories, then global's. `limit` caps the
        whole answer: project hits first, global fills what is left."""
        try:
            return [_hit(m, "global" if m.project_id == read_write_set.global_project_id
                         else "project")
                    for m in service.search(query, read_write_set, limit)]
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
                                    description=description, read_write_set=read_write_set)
        except Exception as err:  # noqa: BLE001
            return _map_error("write", err, session_state=session.state)
        return {"id": memory.id, "project_id": memory.project_id}

    @mcp.tool
    async def memory_update(memory_id: str, expected_version: int, content: str,
                            description: str | None = None) -> dict:
        """Rewrite a memory's content in place; id, project and type stay.
        expected_version: the version memory_read returned; if the memory changed
        since, the call is refused -- read it again and redo the edit.
        Global entries are read-only; the call is refused.
        description: omit to keep the existing one; pass a string to replace
        it, or "" to clear it."""
        try:
            memory = service.update(memory_id, content, read_write_set,
                                    expected_version=expected_version, description=description)
        except Exception as err:  # noqa: BLE001
            return _map_error("update", err, memory_id=memory_id)
        return {"id": memory.id, "updated": memory.updated, "version": memory.version}

    @mcp.tool
    async def memory_delete(memory_id: str, expected_version: int) -> dict:
        """Delete a memory that is no longer true or no longer wanted.
        expected_version: the version memory_read returned.
        Global entries are read-only; the call is refused."""
        try:
            service.delete(memory_id, read_write_set, expected_version=expected_version)
        except Exception as err:  # noqa: BLE001
            return _map_error("delete", err, memory_id=memory_id)
        return {"deleted": memory_id}

    return mcp
