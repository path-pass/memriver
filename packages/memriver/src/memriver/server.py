from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, NoReturn

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from memriver_core import (
    ContentRejected,
    GlobalReadOnly,
    MemoryNotFound,
    ProjectUnavailable,
    VersionConflict,
)
from memriver_core.bootstrap import build_service
from memriver_core.models import ID_RE, Memory, ProjectContext, SessionKey
from memriver_core.settings import SEARCH_SNIPPET_CHARS, Settings

from .protocol_text import INSTRUCTIONS, SESSION_INSTRUCTIONS, UNTRUSTED_DATA_NOTICE
from .views import session_item

logger = logging.getLogger("memriver")

# read, update, delete and write map the same core errors to different
# client-visible strings, so the exception type alone cannot decide the
# response -- every call site passes the operation it is translating for.
# "list" covers memory_index/memory_search/session_search: none names a single
# entry, so any failure there is reported the same way.
Operation = Literal["read", "write", "update", "delete", "list", "confirm"]

# the harnesses whose every call names its own session (spec §7.1); any other
# registration answers for the directory it started in (§7.2)
_SESSION_HARNESSES = ("claude-code", "codex")

_COULD_NOT_READ_STORE = "could not read the memory store"

# The global project is readable in every read/write set and writable in none, so
# one refusal covers update and delete: it tells the agent to stop rather than
# look for another way in.
_GLOBAL_READ_ONLY = ("global memories are read-only to agents; no change was made. Tell "
                     "the user; do not retry through another entry or edit the store directly.")

# Why there is no writable project, and what the agent should ask for -- keyed
# by the project context state, never by a path.
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

# The same states for a context that came from a session row: its project was
# fixed when the session registered and never re-resolves, so the way out is a
# new session, not the directory the session happens to be in.
_SESSION_NO_PROJECT = {
    "none": ("no writable project: this session was registered with no project. No memory "
             "was saved. Ask the user to run memriver project init, then start a new session "
             "to save there; do not run it yourself."),
    "degraded": ("no writable project: this session's project no longer exists or became "
                 "global. No memory was saved. Ask the user to start a new session."),
}


# Why a session may not use a project, keyed by ProjectUnavailable.reason or,
# where the reason is empty, by the context state the call ran under.
_SESSION_REFUSAL = {
    "pending": ("this session is awaiting the user's confirmation; ask the user, then call "
                "session_confirm"),
    "unidentified": ("this session is not registered with memriver: its hooks may not be "
                     "installed (memriver install) or trusted (Codex asks on start), or the "
                     "harness sent no session id; ask the user to fix that, then start a new "
                     "session"),
    "candidate-changed": ("the proposed project changed since memriver proposed it; ask the "
                          "user to start a new session"),
}

_SESSION_TOOLS_UNAVAILABLE = "session tools are not available for this harness registration"


def _map_error(operation: Operation, err: Exception, *, memory_id: str | None = None,
               context_state: str | None = None, session_keyed: bool = False) -> str:
    """Application error -> the tool's client-visible message. Tools never leak one raw.

    Every client-visible string for a storage-boundary error is written here,
    from (operation, error type, structured fields): a second backend raising
    the same error produces the same response byte for byte, and cannot leak
    a path, an errno or a driver message into one.
    """
    if operation == "list":
        return _COULD_NOT_READ_STORE
    if isinstance(err, GlobalReadOnly):
        return _GLOBAL_READ_ONLY
    if isinstance(err, ProjectUnavailable):
        refusal = _SESSION_REFUSAL.get(err.reason) or _SESSION_REFUSAL.get(context_state or "")
        if refusal is not None:
            return refusal
    if operation == "confirm":
        return "could not confirm this session; ask the user to run memriver doctor"
    if operation == "write":
        if isinstance(err, ProjectUnavailable):
            state = context_state or "none"
            if session_keyed and state in _SESSION_NO_PROJECT:
                return _SESSION_NO_PROJECT[state]
            return _NO_PROJECT.get(state, _NO_PROJECT["none"])
        # a codec failure (a lone surrogate) is a ValueError whose message is
        # the codec's, not policy copy
        if isinstance(err, ContentRejected | ValueError) and not isinstance(err, UnicodeError):
            # policy copy is authored in the core and never echoes the value;
            # ValueError reaches here from the model constructors or from a
            # store's pre-write validation of the row it is about to write
            return str(err)
        return "could not write entry"
    if isinstance(err, ContentRejected):
        return str(err)
    # the id is the caller's string: echo it only when it is a well-formed id,
    # so newlines, injected text or unencodable characters never come back
    valid = memory_id is not None and ID_RE.fullmatch(memory_id)
    suffix = f": {memory_id}" if valid else ""
    if isinstance(err, MemoryNotFound):
        return f"no such entry{suffix}"
    if isinstance(err, VersionConflict) and operation in ("update", "delete"):
        subject = f"entry {memory_id}" if valid else "entry"
        return (f"{subject} changed since you read it; no change was made. "
                "Call memory_read again and retry with its version.")
    if operation == "read":
        return f"could not read entry{suffix}"
    if operation == "update":
        return f"could not update entry{suffix}"
    return f"could not delete entry{suffix}"


# The error types _map_error gives a specific message of their own; anything
# else (StorageFailure included) falls through to one of the generic
# messages above and is logged below, so a lock timeout, corruption and a
# genuine bug stay distinguishable in the log even though the client sees
# the same text either way. ValueError is deliberately not here: per
# _map_error it is only a named, expected refusal on the write path (and
# only when it is not a UnicodeError) -- see `_fail`.
_NAMED_ERRORS: tuple[type[Exception], ...] = (
    ContentRejected, GlobalReadOnly, MemoryNotFound, ProjectUnavailable, VersionConflict,
)


def _fail(operation: Operation, err: Exception, *, memory_id: str | None = None,
          context_state: str | None = None, session_keyed: bool = False) -> NoReturn:
    """Map `err` to its client message and raise it as the tool's failure.

    An exception outside `_NAMED_ERRORS` also logs one WARNING line naming
    only the operation and the exception's type -- never its message,
    arguments or any path/value it might carry. A `ValueError` mirrors
    `_map_error`'s own write-only, non-Unicode carve-out instead of being
    exempt everywhere: on `read`/`update`/`delete`/`list` (e.g. the pre-write
    round-trip check in `SqliteMemoryStore.update`) and a `UnicodeError` even
    on `write` both fall through to a generic message, so they log like any
    other unnamed exception.
    """
    message = _map_error(operation, err, memory_id=memory_id, context_state=context_state,
                         session_keyed=session_keyed)
    write_value_error = (operation == "write" and isinstance(err, ValueError)
                         and not isinstance(err, UnicodeError))
    expected = isinstance(err, _NAMED_ERRORS) or write_value_error
    if not expected:
        tool = "session_confirm" if operation == "confirm" else f"memory_{operation}"
        logger.warning("%s failed: %s", tool, type(err).__name__)
    # An expected refusal (a named core error, or the write path's non-Unicode
    # ValueError) is routine agent behaviour, not an operational problem: it
    # would otherwise print an ERROR line to stderr for every MemoryNotFound,
    # VersionConflict or content refusal. FastMCP logs each ToolError at
    # `log_level` (default ERROR); DEBUG here keeps that noise out of normal
    # operation while an unexpected failure still logs at FastMCP's default,
    # alongside memriver's own WARNING above.
    log_level = logging.DEBUG if expected else logging.ERROR
    raise ToolError(message, log_level=log_level) from None


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


def _session_key(harness: str, ctx: Context) -> SessionKey | None:
    """The calling session, read from this call alone; None when it names none validly."""
    if harness == "claude-code":
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    else:
        # FastMCP hands `_meta` over as a model object; Codex nests its
        # turn metadata as an object, and anything else (a JSON string) is
        # not taken apart
        meta = ctx.request_context.meta if ctx.request_context is not None else None
        data = meta.model_dump() if meta is not None else {}
        nested = data.get("x-codex-turn-metadata")
        session_id = nested.get("session_id") if isinstance(nested, dict) else None
    if session_id is None:
        return None
    try:
        return SessionKey(harness, session_id)
    except ValueError:
        return None


_SESSION_SEARCH_DESCRIPTION = (
    "Find this project's recorded sessions (newest activity first) by a word in their "
    "prompts, branch or entry directory; an empty query lists them. Each carries a "
    "resume_command to show the user; whether to run it is the user's decision. Prompt "
    "texts are quoted from the sessions. " + UNTRUSTED_DATA_NOTICE)


def build_server(root: Path, project_dir: Path, settings: Settings | None = None, *,
                 harness: str | None = None) -> FastMCP:
    """Build the MCP server for one harness registration.

    `harness` "claude-code" or "codex" routes every call through the calling
    session's stored row; anything else (Cursor, Kiro, none) answers for
    `project_dir`, resolved once at build time. The value is also each
    write's `source.harness` ("unknown" when absent).

    `root` stays an explicit argument -- callers that already resolved it (the
    CLI, the tests) must not have it re-read from the environment here. Only
    the behaviour knobs come from `settings`; when it is None a bare
    `Settings()` supplies them from the environment and the built-in defaults.
    """
    settings = settings if settings is not None else Settings()
    service = build_service(settings, root=root)
    source_harness = harness or "unknown"
    session_mode = harness in _SESSION_HARNESSES

    if session_mode:
        instructions = INSTRUCTIONS + "\n\n" + SESSION_INSTRUCTIONS

        def context_of(ctx: Context) -> ProjectContext:
            # per call, from the call itself: parallel calls never share a
            # "current session", and nothing is cached between them
            return service.session_context(_session_key(harness, ctx))
    else:
        instructions = INSTRUCTIONS
        # resolved once, at build time: every tool answers for the same
        # project for the life of the server, and the header cannot drift
        directory_context = service.open_project_context(str(project_dir))

        def context_of(ctx: Context) -> ProjectContext:
            return directory_context

    mcp = FastMCP("memriver", instructions=instructions)

    # Tools are plain `def`: FastMCP runs each in a worker thread, so a slow
    # SQLite wait for another process's write lock cannot stall the event
    # loop. Shared state is safe under that: `service` and a directory-mode
    # context are built once above and only read; a session-mode context is
    # a local of each call; each call opens its own SQLite connection; and
    # the service's lazy content-policy build can race two callers, but
    # Python serializes the scanner module's import and an attribute
    # assignment never exposes a half-built object, so no lock is needed.

    @mcp.tool
    def memory_index(ctx: Context) -> str:
        """The project context's project on the first line, then a compact index of
        the current project's memories followed by global's."""
        try:
            context = context_of(ctx)
            return context.header + "\n" + service.index(context)
        except Exception as err:  # noqa: BLE001
            _fail("list", err)

    @mcp.tool
    def memory_read(memory_id: str, ctx: Context) -> dict:
        """Read one memory in full by id, including the version that
        memory_update and memory_delete must name."""
        try:
            return _full(service.read(memory_id, context_of(ctx)))
        except Exception as err:  # noqa: BLE001
            _fail("read", err, memory_id=memory_id)

    @mcp.tool
    def memory_search(query: str, ctx: Context, limit: int | None = None) -> list[dict]:
        """Search the current project's memories, then global's. `limit` caps the
        whole answer: project hits first, global fills what is left."""
        try:
            context = context_of(ctx)
            global_project_id = context.read_write_set.global_project_id
            return [_hit(m, "global" if m.project_id == global_project_id else "project")
                    for m in service.search(query, context, limit)]
        except Exception as err:  # noqa: BLE001
            _fail("list", err)

    @mcp.tool
    def memory_write(content: str,
                     type: Literal["user", "feedback", "project", "reference"],
                     ctx: Context, sync: bool = True, description: str = "") -> dict:
        """Save one durable fact to the current project's memory; memriver assigns the id.
        Global memories are read-only to agents.
        type: user = who the user is; feedback = how they want you to work;
        project = ongoing work/constraints; reference = external resources.
        description: one-line summary shown in the index; when should a future
        session recall this?"""
        context = None
        try:
            context = context_of(ctx)
            memory = service.record(content=content, type=type, sync=sync,
                                    harness=source_harness, description=description,
                                    context=context)
        except Exception as err:  # noqa: BLE001
            _fail("write", err, context_state=None if context is None else context.state,
                  session_keyed=context is not None and context.session_key is not None)
        return {"id": memory.id, "project_id": memory.project_id}

    @mcp.tool
    def memory_update(memory_id: str, expected_version: int, content: str, ctx: Context,
                      description: str | None = None) -> dict:
        """Rewrite a memory's content in place; id, project and type stay.
        expected_version: the version memory_read returned; if the memory changed
        since, the call is refused -- read it again and redo the edit.
        Global entries are read-only; the call is refused.
        description: omit to keep the existing one; pass a string to replace
        it, or "" to clear it."""
        try:
            memory = service.update(memory_id, content, context_of(ctx),
                                    expected_version=expected_version, description=description)
        except Exception as err:  # noqa: BLE001
            _fail("update", err, memory_id=memory_id)
        return {"id": memory.id, "updated": memory.updated, "version": memory.version}

    @mcp.tool
    def memory_delete(memory_id: str, expected_version: int, ctx: Context) -> dict:
        """Delete a memory that is no longer true or no longer wanted.
        expected_version: the version memory_read returned.
        Global entries are read-only; the call is refused."""
        try:
            service.delete(memory_id, context_of(ctx), expected_version=expected_version)
        except Exception as err:  # noqa: BLE001
            _fail("delete", err, memory_id=memory_id)
        return {"deleted": memory_id}

    @mcp.tool(description=_SESSION_SEARCH_DESCRIPTION)
    def session_search(ctx: Context, query: str = "", limit: int | None = None) -> list[dict]:
        if not session_mode:
            raise ToolError(_SESSION_TOOLS_UNAVAILABLE, log_level=logging.DEBUG)
        try:
            return [session_item(session)
                    for session in service.search_sessions(query, context_of(ctx), limit)]
        except Exception as err:  # noqa: BLE001
            _fail("list", err)

    @mcp.tool
    def session_confirm(ctx: Context) -> dict:
        """Register this session to the project memriver proposed for it. Call only
        after the user agreed; returns the session's new project header."""
        if not session_mode:
            raise ToolError(_SESSION_TOOLS_UNAVAILABLE, log_level=logging.DEBUG)
        try:
            key = _session_key(harness, ctx)
            if key is None:
                raise ProjectUnavailable(reason="unidentified")
            return {"header": service.confirm_session(key).header}
        except Exception as err:  # noqa: BLE001
            _fail("confirm", err)

    return mcp
