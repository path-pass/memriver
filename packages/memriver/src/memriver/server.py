from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from memriver_core import (
    ContentRejected,
    GlobalReadOnly,
    InvalidScope,
    MemoryNotFound,
    NameTaken,
    ProjectUnavailable,
    StorageFailure,
    UnreadableMemory,
)
from memriver_core.bootstrap import build_service
from memriver_core.config import Settings

from .project_context import ProjectResolution, resolve
from .protocol_text import INSTRUCTIONS

# read, update, delete and write map the same application errors to different
# client-visible strings, so the exception type alone cannot decide the
# response -- every call site passes the operation it is translating for.
# "list" covers memory_index/memory_search/memory_dream: none of them names a
# single entry, so there is no id or scope to react to -- any failure there is
# reported the same way, regardless of the exception's type or fields.
Operation = Literal["read", "write", "update", "delete", "list"]

_COULD_NOT_READ_STORE = "could not read the memory store"

# The global scope is readable from every context and writable from none, so
# one refusal covers create, update and delete: it tells the agent to stop
# rather than look for another way in.
_GLOBAL_READ_ONLY = ("global memories are read-only to agents; no change was made. Tell "
                     "the user; do not retry through another entry or edit the store directly.")

# Why there is no project, and what the agent should ask for -- keyed by the
# resolution state, never by a path: the header line is the only place a
# directory is ever named.
_NO_PROJECT = {
    "none": ("no writable project: this directory is not registered. No memory was saved. "
             "Ask the user to choose a project root and run memriver project init; do not "
             "run it yourself."),
    "degraded": ("no writable project: the project registry is invalid. No memory was "
                 "saved. Ask the user to run memriver project explain."),
}


def _map_error(operation: Operation, err: Exception, *,
               entry_id: str | None = None,
               resolution: ProjectResolution | None = None) -> dict:
    """Application error -> the tool's error dict. Tools never raise.

    Every string a client sees for a storage-boundary error is written here,
    from (operation, error type, structured fields). The repository supplies
    the data and none of the words, so a second backend raising the same
    error with the same fields produces the same response byte for byte --
    and cannot leak a path, an errno, or a driver message into one.
    """
    if operation == "list":
        return {"error": _COULD_NOT_READ_STORE}
    if isinstance(err, GlobalReadOnly):
        # one refusal for every mutating operation: the rule is the scope's,
        # not the operation's
        return {"error": _GLOBAL_READ_ONLY}
    if operation == "write":
        if isinstance(err, NameTaken):
            old = err.existing
            if old.scope.project_id is None:
                # readable from here, but no memory_update can ever land on
                # it: echoing the entry would only invite a refused retry
                return {"error": f"name {err.memory_id!r} is already used by a "
                                 "read-only global memory; choose another name"}
            return {"error": f"name {err.memory_id!r} already exists; "
                             "memory_update it, or choose a more precise name "
                             "if this is a different fact",
                    "existing": {"id": old.id, "type": old.type,
                                 "scope": old.scope.to_storage(),
                                 "updated": old.updated,
                                 "snippet": old.body[:120],
                                 "description": old.description}}
        if isinstance(err, UnreadableMemory):
            # the name is occupied by something the backend cannot decode:
            # the write is refused without describing what sits there
            return {"error": f"name {err.memory_id!r} is taken by a file that "
                             "is not a readable entry"}
        if isinstance(err, ProjectUnavailable):
            # the core's message is one generic line for every context; the
            # transport is what resolved the project, so it is what says why
            # there is none and what to ask the user for
            state = resolution.state if resolution is not None else "none"
            return {"error": _NO_PROJECT.get(state, _NO_PROJECT["none"])}
        if isinstance(err, ContentRejected | InvalidScope | ValueError):
            # policy/scope copy is authored in the core, where the wording is
            # the rule itself, and is already client-safe (it never echoes the
            # rejected value); ValueError still reaches here from the model
            # constructors, exactly as it did before the core split
            return {"error": str(err)}
        # e.g. a full disk or a permission error from the atomic write;
        # the OS message may carry the store path, so it is never echoed
        return {"error": "could not write entry"}
    if isinstance(err, MemoryNotFound):
        return {"error": f"no such entry: {entry_id}"}
    if operation == "delete":
        # e.g. a permission error unlinking the file, or a hand-written file
        # that does not parse as an entry; the OS message may carry the
        # store's absolute path, so it is never echoed to the client
        return {"error": f"could not delete entry: {entry_id}"}
    if isinstance(err, ContentRejected):
        return {"error": str(err)}
    if operation == "update" and isinstance(err, StorageFailure):
        # update's read-modify-write can fail on either half; a StorageFailure
        # here may be the write side (e.g. a full disk), not a corrupt source
        # file, so it must not be reported as "unreadable" like read's is
        return {"error": f"could not update entry: {entry_id}"}
    return {"error": f"unreadable entry file: {entry_id}"}


def build_server(root: Path, project_dir: Path,
                 settings: Settings | None = None) -> FastMCP:
    """Build the MCP server bound to `root`/`project_dir`.

    `root` stays an explicit argument -- callers that already resolved it (the
    CLI, the tests) must not have it re-read from the environment here. Only
    the behaviour knobs come from `settings`; when it is None a bare
    `Settings()` supplies them from the environment and the built-in defaults
    -- unlike `load_settings`, this does NOT read `<root>/config.toml`. A
    caller that wants the config file honoured must call `load_settings`
    itself and pass the result in as `settings`.
    """
    settings = settings if settings is not None else Settings()
    service = build_service(settings, root=root)
    # resolved once, at build time: every tool answers for the same project
    # for the life of the server, and the header cannot drift between calls
    resolution = resolve(root, project_dir)
    ctx = resolution.context()

    mcp = FastMCP("memriver", instructions=INSTRUCTIONS)

    # Tools stay async so their bodies run inline on the event loop thread;
    # they are local and fast, with no await points, so calls serialize
    # without extra locking.

    @mcp.tool
    async def memory_index() -> str:
        """The session's project on the first line, then a compact index of every
        visible memory (global + project)."""
        try:
            return resolution.header() + "\n" + service.index(ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("list", err)["error"]

    @mcp.tool
    async def memory_read(entry_id: str) -> dict:
        """Read one memory entry in full by name."""
        try:
            m = service.read(entry_id, ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("read", err, entry_id=entry_id)
        return {"id": m.id, "type": m.type, "scope": m.scope.to_storage(),
                "body": m.body, "created": m.created, "updated": m.updated,
                "trust": m.trust, "description": m.description}

    @mcp.tool
    async def memory_search(query: str, limit: int | None = None) -> list[dict]:
        """Search memories relevant to a task (global + current project)."""
        try:
            return [{"id": h.id, "scope": h.scope.to_storage(), "type": h.type,
                     "snippet": h.snippet}
                    for h in service.search(query, ctx, limit)]
        except Exception as err:  # noqa: BLE001
            return [_map_error("list", err)]

    @mcp.tool
    async def memory_write(content: str,
                           type: Literal["user", "feedback", "project", "reference"],
                           name: str = "", sync: bool = True,
                           harness: str = "unknown",
                           description: str = "") -> dict:
        """Save one durable fact to the current project's memory.
        Global memories are read-only to agents.
        type: user = who the user is; feedback = how they want you to work;
        project = ongoing work/constraints; reference = external resources.
        name: short kebab-case name proposal; it becomes the permanent id.
        description: one-line recall cue shown in the index; when should a
        future session remember this?"""
        try:
            m = service.create(content=content, type=type, name=name, sync=sync,
                               harness=harness, description=description, ctx=ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("write", err, resolution=resolution)
        return {"id": m.id, "scope": m.scope.to_storage()}

    @mcp.tool
    async def memory_update(entry_id: str, content: str,
                            description: str | None = None) -> dict:
        """Rewrite an existing memory in place; the name and type stay.
        Global entries are read-only; the call is refused.
        description: omit to keep the existing one; pass a string to replace
        it, or "" to clear it."""
        try:
            m = service.update(entry_id, content, ctx, description=description)
        except Exception as err:  # noqa: BLE001
            return _map_error("update", err, entry_id=entry_id)
        return {"id": m.id, "updated": m.updated}

    @mcp.tool
    async def memory_delete(entry_id: str) -> dict:
        """Delete a memory that is no longer true or no longer wanted.
        Global entries are read-only; the call is refused."""
        try:
            service.delete(entry_id, ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("delete", err, entry_id=entry_id)
        return {"deleted": entry_id}

    @mcp.tool
    async def memory_dream(limit: int = 3) -> dict:
        """Maintenance review queue: the current project's entries only, least
        recently confirmed true first.

        For DEDICATED memory-hygiene sessions only -- do not call this during
        regular task work. For each returned entry, verify it against reality:
        still true -> memory_update with the unchanged body (records the
        confirmation); outdated -> memory_update with the corrected body;
        no longer true or wanted -> memory_delete."""
        try:
            return {"project": resolution.header(), "entries": [
                {"id": m.id, "type": m.type, "scope": m.scope.to_storage(),
                 "description": m.description, "body": m.body,
                 "created": m.created, "updated": m.updated, "trust": m.trust}
                for m in service.dream(ctx, limit=limit)]}
        except Exception as err:  # noqa: BLE001
            return _map_error("list", err)

    return mcp
