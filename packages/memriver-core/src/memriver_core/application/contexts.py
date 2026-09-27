"""Project contexts: the header, state and read/write set a caller answers for.

Shared by ProjectService (a directory's context) and SessionService (a
session row's context), so both build the same header for the same facts.
Plain functions over models: no store, no service.
"""

from __future__ import annotations

from memriver_core.models import (
    Project,
    ProjectContext,
    ReadWriteSet,
    SessionKey,
    single_line,
)

# The project context header's fixed lines; the registered and degraded lines
# are composed per project context below.
NONE_HEADER = "project: none — global is read-only; ask the user to run memriver project init"
STORE_UNREADABLE_HEADER = ("project: unavailable — the memory store could not be read; "
                           "ask the user to run memriver doctor")
PENDING_HEADER = ("project: awaiting confirmation — this session is not registered; "
                  "ask the user, then call session_confirm")
# pending with no candidate: confirming would register the session with no
# project (session_register can assign one later), so the header offers
# session_register rather than session_confirm
PENDING_NO_CANDIDATE_HEADER = (
    "project: none — this session is not registered, and the directory it was first "
    "observed in is not in any registered project, so global is read-only; to save, ask "
    "the user to run memriver project init there, then call session_register")
UNIDENTIFIED_HEADER = ("project: none — this session is not registered with memriver: its "
                       "hooks may not be installed (memriver install) or trusted (Codex asks "
                       "on start), or the harness sent no session id; ask the user to fix "
                       "that, then start a new session")
# a session row's project never re-resolves on its own: one registered with
# no project gets one only through session_register, from where it started,
# and one whose project is gone needs a new session -- never the directory the
# session happens to be in
SESSION_NONE_HEADER = ("project: none — this session was registered with no project, so global "
                       "is read-only; to save, ask the user to run memriver project init where "
                       "this session started, then call session_register")
SESSION_PROJECT_GONE_HEADER = ("project: unavailable — this session's project no longer exists "
                               "or became global, so global is read-only; to save, ask the "
                               "user to start a new session")


def header_field(value: str, max_chars: int) -> str:
    """One stored value as it may appear in the agent-facing header."""
    return single_line(value)[:max_chars]


def registered_context(project: Project, global_project_id: str | None, *,
                       header_field_chars: int,
                       session_key: SessionKey | None = None) -> ProjectContext:
    return ProjectContext(
        "registered",
        f"project: {header_field(project.name, header_field_chars)} [{project.id}] "
        f"(root {header_field(project.root or '', header_field_chars)})",
        ReadWriteSet(project_id=project.id, global_project_id=global_project_id),
        project, session_key=session_key)


def degraded_context(diagnostic: str, global_project_id: str | None, *,
                     header_field_chars: int,
                     session_key: SessionKey | None = None) -> ProjectContext:
    return ProjectContext(
        "degraded",
        f"project: unavailable — this directory could not be matched to one project "
        f"({header_field(diagnostic, header_field_chars)}); ask the user to run memriver "
        f"project explain",
        ReadWriteSet(project_id=None, global_project_id=global_project_id),
        diagnostic=diagnostic, session_key=session_key)


def no_project_context(state: str, header: str, global_project_id: str | None,
                       session_key: SessionKey | None = None) -> ProjectContext:
    """A context that may read global only."""
    return ProjectContext(state, header,
                          ReadWriteSet(project_id=None, global_project_id=global_project_id),
                          session_key=session_key)


def unavailable_context(session_key: SessionKey | None) -> ProjectContext:
    return ProjectContext("unavailable", STORE_UNREADABLE_HEADER,
                          ReadWriteSet(project_id=None, global_project_id=None),
                          session_key=session_key)
