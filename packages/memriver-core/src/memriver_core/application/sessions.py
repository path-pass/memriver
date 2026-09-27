"""SessionService: harness sessions (spec §5) -- start, prompts, end, Stop, tool calls.

SessionStore owns the session rows; this service registers sessions from
their entry directory, builds the context a stored row grants, answers the
save-watermark and pending questions a memory write asks, and hands the CLI
its session listing. Nothing here touches a file, a table or settings: every
limit and every directory question is injected.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from memriver_core.application.contexts import (
    NONE_HEADER,
    PENDING_HEADER,
    PENDING_NO_CANDIDATE_HEADER,
    SESSION_NONE_HEADER,
    SESSION_PROJECT_GONE_HEADER,
    UNIDENTIFIED_HEADER,
    degraded_context,
    no_project_context,
    registered_context,
    unavailable_context,
)
from memriver_core.models import (
    Project,
    ProjectContext,
    PromptEntry,
    ReadWriteSet,
    Session,
    SessionKey,
    now,
    single_line,
)
from memriver_core.models.errors import (
    ContentRejected,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)

if TYPE_CHECKING:
    from memriver_core.content_policy.protocol import ContentPolicy
    from memriver_core.repository.protocol import ProjectStore, SessionStore

# a new session with no row: these sources start one afresh, so it registers
# at once; resume/compact continue one memriver never saw, so it waits for
# the user (spec §5.2)
_FRESH_SOURCES = frozenset({"startup", "clear", "fork"})
_ENTRY_UNRESOLVABLE = "working directory could not be resolved"
_WORKTREE_UNMAPPED = "the git worktree could not be mapped to its main working tree"
_SESSION_PROJECT_MISSING = "session project missing"


class SessionService:
    def __init__(self, session_store: SessionStore, project_store: ProjectStore,
                 content_policy_factory: Callable[[], ContentPolicy], *,
                 canonical_directory: Callable[[str], str | None],
                 main_tree_path: Callable[[str], str | None],
                 current_branch: Callable[[str], str | None],
                 root_is_intact: Callable[[str], bool], header_field_chars: int,
                 session_prompt_chars: int, session_recent_prompts: int,
                 session_prompt_scan_max_bytes: int, stop_nudge_min_prompts: int,
                 stop_nudge_interval_prompts: int, session_search_limit_default: int,
                 session_search_limit_max: int, tool_call_retention_s: int,
                 summary_max_chars: int) -> None:
        self._session_store = session_store
        self._project_store = project_store
        # the directory questions a session registration asks (spec §5.1),
        # injected so this layer runs no git and reads no filesystem
        self._canonical_directory = canonical_directory
        self._main_tree_path = main_tree_path
        self._current_branch = current_branch
        self._root_is_intact = root_is_intact
        # built on first use: a hook that records no prompt never pays for
        # loading and compiling the scanner rules
        self._content_policy_factory = content_policy_factory
        self._content_policy: ContentPolicy | None = None
        self._header_field_chars = header_field_chars
        self._session_prompt_chars = session_prompt_chars
        self._session_recent_prompts = session_recent_prompts
        self._session_prompt_scan_max_bytes = session_prompt_scan_max_bytes
        self._stop_nudge_min_prompts = stop_nudge_min_prompts
        self._stop_nudge_interval_prompts = stop_nudge_interval_prompts
        self._session_search_limit_default = session_search_limit_default
        self._session_search_limit_max = session_search_limit_max
        self._tool_call_retention_s = tool_call_retention_s
        self._summary_max_chars = summary_max_chars

    def _policy(self) -> ContentPolicy:
        if self._content_policy is None:
            self._content_policy = self._content_policy_factory()
        return self._content_policy

    def store_exists(self) -> bool:
        """False only for a store known to be absent (nothing to route, nothing to
        write). One that cannot be checked counts as present: its operation then
        reports it as unavailable."""
        try:
            return self._session_store.store_exists()
        except StorageFailure:
            return True

    def start_session(self, key: SessionKey, *, source: str, entry_dir: str,
                      transcript_path: str | None) -> ProjectContext:
        """SessionStart: the stored row's context, registering the session when it has none."""
        # first: without a store no directory question (git included) is asked
        if not self.store_exists():
            return self._storeless_context(key)
        try:
            stored = self._session_store.get(key)
            if stored is not None:
                touched = self._session_store.touch(key, now(),
                                                    transcript_path=transcript_path)
                return self._context_of(touched or stored)
            seed = self._new_session(key, entry_dir, transcript_path,
                                     fresh=source in _FRESH_SOURCES)
            if isinstance(seed, ProjectContext):
                return seed
            # the stored winner answers, never this call's own computation
            winner = self._session_store.register(seed)
            if winner is None:
                return self._storeless_context(key)
            return self._context_of(winner)
        except StorageFailure:
            return unavailable_context(key)

    def observe_prompt(self, key: SessionKey, *, prompt: str, entry_dir: str,
                       transcript_path: str | None) -> tuple[ProjectContext, bool]:
        """UserPromptSubmit: count and record one prompt; True when this call created the row."""
        # first: without a store neither the scanner nor git runs
        if not self.store_exists():
            return self._storeless_context(key), False
        entry = self._prompt_entry(prompt, now())
        try:
            seed = self._session_store.get(key)
            if seed is None:
                pending = self._new_session(key, entry_dir, transcript_path, fresh=False)
                if isinstance(pending, ProjectContext):
                    return pending, False
                seed = pending
            added = self._session_store.add_prompt(key, entry, seed=seed,
                                                   keep_recent=self._session_recent_prompts)
            if added is None:
                return self._storeless_context(key), False
            session, created = added
            return self._context_of(session), created
        except StorageFailure:
            return unavailable_context(key), False

    def end_session(self, key: SessionKey) -> None:
        self._session_store.end(key, now())

    def stop_decision(self, key: SessionKey) -> bool:
        """Stop: True when the session is due a save nudge (the nudge is recorded)."""
        try:
            return self._session_store.nudge_if_due(
                key, now(), min_prompts=self._stop_nudge_min_prompts,
                interval=self._stop_nudge_interval_prompts)
        except StorageFailure:
            return False

    def record_tool_call(self, key: SessionKey, call_id: str) -> None:
        """Claude Code's PreToolUse: remember which session made this call (spec U15).

        Best effort: a failure only leaves the MCP server on its fallback.
        """
        try:
            self._session_store.record_call(key, call_id, now(),
                                            retention_s=self._tool_call_retention_s)
        except StorageFailure:
            pass

    def session_key_for_call(self, harness: str, call_id: str) -> SessionKey | None:
        """The session a recorded tool call belongs to; None when none is known."""
        try:
            return self._session_store.session_for_call(harness, call_id)
        except StorageFailure:
            return None

    def session_context(self, key: SessionKey | None) -> ProjectContext:
        """The context a session's stored row grants. Never resolves a directory."""
        try:
            stored = None if key is None else self._session_store.get(key)
            if stored is None:
                return no_project_context("unidentified", UNIDENTIFIED_HEADER,
                                          self._project_store.global_project_id(), key)
            return self._context_of(stored)
        except StorageFailure:
            return unavailable_context(key)

    def confirm_session(self, key: SessionKey) -> ProjectContext:
        """The user confirmed a pending session: register it with its candidate (or none)."""
        stored = self._session_store.get(key)
        # a candidate without a root would confirm to any root-NULL project; the
        # root's integrity is a filesystem question, asked outside the store's
        # transaction
        if stored is not None and stored.status == "pending" \
                and stored.candidate_id is not None \
                and (stored.candidate_root is None
                     or not self._root_is_intact(stored.candidate_root)):
            raise ProjectUnavailable(reason="candidate-changed")
        confirmed = self._session_store.confirm(key)
        if confirmed is None:
            raise ProjectUnavailable(reason="unidentified")
        return self._context_of(confirmed)

    def register_session(self, key: SessionKey) -> ProjectContext:
        """On request: give a session with no project the one covering where it started.

        Resolved from the stored entry directory exactly like a first
        registration (spec §5.1, U14); a row that already has a project, or
        nothing covering the entry, answers unchanged.
        """
        # first: without a store no directory question (git included) is asked
        if not self.store_exists():
            return self._storeless_context(key)
        stored = self._session_store.get(key)
        if stored is None:
            raise ProjectUnavailable(reason="unidentified")
        if stored.project_id is not None:
            return self._context_of(stored)
        if stored.candidate_id is not None:
            raise ProjectUnavailable(reason="pending")      # session_confirm's to decide
        # outside any transaction: git may run here
        resolved = self._resolve_entry(key, stored.entry_cwd)
        if isinstance(resolved, ProjectContext):
            return resolved
        _, project = resolved
        if project is None:
            return self._context_of(stored)
        assigned = self._session_store.assign_project(key, project.id)
        if assigned is None:
            return self._storeless_context(key)
        return self._context_of(assigned)

    def search_sessions(self, query: str, context: ProjectContext,
                        limit: int | None = None) -> list[Session]:
        project_id = context.read_write_set.project_id
        if project_id is None:
            return []
        limit = self._session_search_limit_default if limit is None else limit
        return self._session_store.search(
            project_id, query, max(1, min(limit, self._session_search_limit_max)))

    def list_sessions(self, *, project_id: str | None = None, query: str = "",
                      limit: int | None = None) -> list[Session]:
        """The human read: every project's sessions (or one's), pending ones included.

        ProjectNotFound for an unknown project id, like the other human reads.
        """
        if project_id is not None:
            self._project_store.read(project_id)
        return self._session_store.search(project_id, query,
                                          sys.maxsize if limit is None else limit)

    def bound_sessions(self) -> list[Session]:
        """Sessions bound to a project (the ones a summary is published for), newest first."""
        return self._session_store.bound()

    def publish_summary(self, key: SessionKey, text: str, *,
                        expected_last_active_at: str) -> None:
        """Publish a session's summary (spec §4.3).

        The text passes the content policy and the summary length limit first
        (`ContentRejected`); the session must still be bound and still have
        `expected_last_active_at`, else `SessionMoved`. Nothing is written on
        any refusal.
        """
        self._policy().check(text, self._summary_max_chars)
        self._session_store.publish_summary(key, text,
                                            expected_last_active_at=expected_last_active_at,
                                            at=now())

    def pending_candidate(self, context: ProjectContext) -> Project | None:
        """The project a pending session would be confirmed to, if it has one."""
        stored = self._stored_session(context)
        if stored is None or stored.status != "pending" or stored.candidate_id is None:
            return None
        try:
            return self._project_store.read(stored.candidate_id)
        except ProjectNotFound:
            return None

    def entry_of(self, context: ProjectContext) -> str | None:
        """The directory the session was first seen in, for the pending notice."""
        stored = self._stored_session(context)
        return None if stored is None else stored.entry_cwd

    # --- what a memory write asks (MemoryService's injected callbacks) ---

    def refuse_pending(self, context: ProjectContext) -> None:
        """ProjectUnavailable when the context is a pending session's: nothing is saved
        before the user confirms it."""
        if context.state == "pending":
            # no candidate: there is nothing to confirm, only a project to init
            stored = self._stored_session(context)
            raise ProjectUnavailable(
                reason="pending-no-candidate" if stored is not None
                and stored.candidate_id is None else "pending")

    def mark_saved(self, context: ProjectContext) -> None:
        """The save watermark (spec §3.3): best effort, never fails the save."""
        if context.session_key is not None:
            try:
                self._session_store.mark_saved(context.session_key)
            except StorageFailure:
                pass

    def _stored_session(self, context: ProjectContext) -> Session | None:
        if context.session_key is None:
            return None
        return self._session_store.get(context.session_key)

    def _context_of(self, session: Session) -> ProjectContext:
        """What a stored row grants: its project, none, or pending (read global only)."""
        global_project_id = self._project_store.global_project_id()
        if session.status == "pending":
            header = PENDING_HEADER if session.candidate_id is not None \
                else PENDING_NO_CANDIDATE_HEADER
            return no_project_context("pending", header, global_project_id, session.key)
        if session.project_id is None:
            return no_project_context("none", SESSION_NONE_HEADER, global_project_id,
                                      session.key)
        try:
            project = self._project_store.read(session.project_id)
        except ProjectNotFound:
            project = None
        if project is None or project.id == global_project_id:
            return ProjectContext(
                "degraded", SESSION_PROJECT_GONE_HEADER,
                ReadWriteSet(project_id=None, global_project_id=global_project_id),
                diagnostic=_SESSION_PROJECT_MISSING, session_key=session.key)
        return registered_context(project, global_project_id,
                                  header_field_chars=self._header_field_chars,
                                  session_key=session.key)

    def _storeless_context(self, key: SessionKey) -> ProjectContext:
        # no store: nothing was written, and no project exists to grant
        return no_project_context("none", NONE_HEADER, None, key)

    def _new_session(self, key: SessionKey, entry_dir: str, transcript_path: str | None, *,
                     fresh: bool) -> Session | ProjectContext:
        """The row a new session gets from its entry directory (spec §5.1, §5.2).

        A context instead when the directory cannot be resolved: nothing is
        registered, and the next event tries again.
        """
        resolved = self._resolve_entry(key, entry_dir)
        if isinstance(resolved, ProjectContext):
            return resolved
        entry, project = resolved
        at = now()
        return Session(
            key=key, status="registered" if fresh else "pending",
            origin="start" if fresh else "first-seen",
            project_id=project.id if fresh and project is not None else None,
            candidate_id=None if fresh or project is None else project.id,
            candidate_root=None if fresh or project is None else project.root,
            entry_cwd=entry, branch=self._current_branch(entry),
            transcript_path=transcript_path, started_at=at, last_active_at=at, ended_at=None,
            prompt_count=0, last_write_prompt_count=0, last_nudge_prompt_count=0,
            first_prompt=None, recent_prompts=())

    def _resolve_entry(self, key: SessionKey,
                       entry_dir: str) -> tuple[str, Project | None] | ProjectContext:
        """A registration directory's canonical spelling and project (spec §5.1).

        The degraded context instead when it cannot be resolved.
        """
        # only the canonical spelling is registered and walked: an alias (a
        # symlink) belongs where it points, never where it is written
        entry = self._canonical_directory(entry_dir)
        mapped = None if entry is None else self._main_tree_path(entry)
        if entry is None or mapped is None:
            return degraded_context(
                _ENTRY_UNRESOLVABLE if entry is None else _WORKTREE_UNMAPPED,
                self._project_store.global_project_id(),
                header_field_chars=self._header_field_chars, session_key=key)
        resolution = self._project_store.resolve(entry, logical=mapped)
        if resolution.state == "degraded":
            return degraded_context(resolution.diagnostic or "",
                                    self._project_store.global_project_id(),
                                    header_field_chars=self._header_field_chars,
                                    session_key=key)
        return entry, resolution.project if resolution.state == "registered" else None

    def _prompt_entry(self, prompt: object, at: str) -> PromptEntry:
        """One prompt as it may be stored: its first line-folded characters, or why not.

        Built before any transaction; the text never reaches an error or a log.
        """
        try:
            encoded = prompt.encode("utf-8") if isinstance(prompt, str) else None
        except UnicodeEncodeError:          # a lone surrogate
            encoded = None
        if encoded is None or single_line(prompt) == "":
            return PromptEntry(at, omitted="invalid")
        if len(encoded) > self._session_prompt_scan_max_bytes:
            return PromptEntry(at, omitted="too-large")
        try:
            self._policy().check(prompt, max_chars=len(prompt))
        except ContentRejected:
            return PromptEntry(at, omitted="secret")
        except Exception:  # noqa: BLE001 - any scanner fault omits the text, never fails the hook
            return PromptEntry(at, omitted="scan-error")
        return PromptEntry(at, text=single_line(prompt)[:self._session_prompt_chars])
