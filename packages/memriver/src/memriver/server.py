from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from memriver_core import (
    ContentRejected,
    InvalidScope,
    MemoryNotFound,
    NameTaken,
    ProjectUnavailable,
    UnreadableMemory,
)
from memriver_core.bootstrap import build_service
from memriver_core.config import Settings

from .project_context import build_context
from .protocol_text import INSTRUCTIONS

# read, update, delete and write map the same application errors to different
# client-visible strings, so the exception type alone cannot decide the
# response -- every call site passes the operation it is translating for.
Operation = Literal["read", "write", "update", "delete"]


def _map_error(operation: Operation, err: Exception, *,
               entry_id: str | None = None,
               project_dir: Path | None = None) -> dict:
    """Application error -> the tool's error dict. Tools never raise.

    Every string a client sees for a storage-boundary error is written here,
    from (operation, error type, structured fields). The repository supplies
    the data and none of the words, so a second backend raising the same
    error with the same fields produces the same response byte for byte --
    and cannot leak a path, an errno, or a driver message into one.
    """
    if operation == "write":
        if isinstance(err, NameTaken):
            if err.existing is None:
                # the collision lives in another scope; its content and type
                # must not leak across that boundary, so nothing is echoed
                return {"error": f"name {err.memory_id!r} is already used "
                                 "elsewhere in the store; choose another name"}
            old = err.existing
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
            # the core is path-free on purpose: project_dir belongs to the
            # transport, which is what resolved it in the first place
            return {"error": f"not inside a git project: {project_dir}"}
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
    return {"error": f"unreadable entry file: {entry_id}"}


def build_server(root: Path, project_dir: Path,
                 settings: Settings | None = None) -> FastMCP:
    # `root` stays an explicit argument -- callers that already resolved it (the
    # CLI, the tests) must not have it re-read from the environment here. Only
    # the behaviour knobs come from `settings`; when it is None the environment
    # and the built-in defaults supply them.
    settings = settings if settings is not None else Settings()
    service = build_service(settings, root=root)
    ctx = build_context(project_dir)

    mcp = FastMCP("memriver", instructions=INSTRUCTIONS)

    # Tools stay async so their bodies run inline on the event loop thread;
    # they are local and fast, with no await points, so calls serialize
    # without extra locking.

    @mcp.tool
    async def memory_index() -> str:
        """List all active memories (global + current project) as a compact index."""
        return service.index(ctx)

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
        return [{"id": h.id, "scope": h.scope.to_storage(), "type": h.type,
                 "snippet": h.snippet}
                for h in service.search(query, ctx, limit)]

    @mcp.tool
    async def memory_write(content: str,
                           type: Literal["user", "feedback", "project", "reference"],
                           name: str = "", scope: str = "project",
                           sync: bool = True, harness: str = "unknown",
                           description: str = "") -> dict:
        """Save one durable fact to shared memory.
        type: user = who the user is; feedback = how they want you to work;
        project = ongoing work/constraints; reference = external resources.
        name: short kebab-case name proposal; it becomes the permanent id.
        scope: 'project' (default) or 'global' (cross-project user facts only).
        description: one-line recall cue shown in the index; when should a
        future session remember this?"""
        try:
            m = service.create(content=content, type=type, name=name, scope=scope,
                               sync=sync, harness=harness, description=description,
                               ctx=ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("write", err, project_dir=project_dir)
        return {"id": m.id, "scope": m.scope.to_storage()}

    @mcp.tool
    async def memory_update(entry_id: str, content: str,
                            description: str | None = None) -> dict:
        """Rewrite an existing memory in place; the name and type stay.
        description: omit to keep the existing one; pass a string to replace
        it, or "" to clear it."""
        try:
            m = service.update(entry_id, content, ctx, description=description)
        except Exception as err:  # noqa: BLE001
            return _map_error("update", err, entry_id=entry_id)
        return {"id": m.id, "updated": m.updated}

    @mcp.tool
    async def memory_delete(entry_id: str) -> dict:
        """Delete a memory that is no longer true or no longer wanted."""
        try:
            service.delete(entry_id, ctx)
        except Exception as err:  # noqa: BLE001
            return _map_error("delete", err, entry_id=entry_id)
        return {"deleted": entry_id}

    @mcp.tool
    async def memory_dream(limit: int = 3) -> dict:
        """Maintenance review queue: the entries least recently confirmed true.

        For DEDICATED memory-hygiene sessions only -- do not call this during
        regular task work. For each returned entry, verify it against reality:
        still true -> memory_update with the unchanged body (records the
        confirmation); outdated -> memory_update with the corrected body;
        no longer true or wanted -> memory_delete."""
        return {"entries": [
            {"id": m.id, "type": m.type, "scope": m.scope.to_storage(),
             "description": m.description, "body": m.body,
             "created": m.created, "updated": m.updated, "trust": m.trust}
            for m in service.dream(ctx, limit=limit)]}

    return mcp
