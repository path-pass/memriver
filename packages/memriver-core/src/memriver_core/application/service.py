"""The application facade: orchestrates the stores, the content policy and the limits.

MemoryStore owns single-memory actions and ProjectStore owns collections and
directories; this facade sequences policy checks, builds project contexts and
read/write sets, renders the index and the project header, and hands the
CLI its planning and management reads. Nothing here touches a file, a table
or settings: every limit is injected.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from memriver_core.models import (
    DiagnosticsReport,
    Memory,
    Project,
    ProjectContext,
    PromptEntry,
    ReadWriteSet,
    Resolution,
    RootPlan,
    Session,
    SessionKey,
    UnbindPlan,
    now,
    single_line,
)
from memriver_core.models.errors import (
    ContentRejected,
    IdCollision,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)

if TYPE_CHECKING:
    from memriver_core.content_policy.protocol import ContentPolicy
    from memriver_core.repository.protocol import (
        MemoryStore,
        ProjectStore,
        SessionStore,
    )


class Diagnostics(Protocol):
    """What the facade needs from the diagnostics service bootstrap composes.

    A structural type rather than an import of DiagnosticsService: only
    bootstrap may name a composed service (architecture rule).
    """

    def run(self, *, now: str | None, stale_days: int,
            jaccard_threshold: float) -> DiagnosticsReport: ...

# 'harness' is persisted verbatim into the stored memory, so without this it
# is a policy-free channel for secrets or megabytes of text. The shape check
# caps size and charset; the content policy then rejects the values that still
# look like credentials. Neither error echoes the rejected value.
_HARNESS_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# What MemoryService.index returns when nothing is visible -- the single
# source transports compare against.
EMPTY_INDEX = "(no memories yet)"

# The project context header's fixed lines; the registered and degraded lines
# are composed per project context in open_project_context.
NONE_HEADER = "project: none — global is read-only; ask the user to run memriver project init"
STORE_UNREADABLE_HEADER = ("project: unavailable — the memory store could not be read; "
                           "ask the user to run memriver doctor")
PENDING_HEADER = ("project: awaiting confirmation — this session is not registered; "
                  "ask the user, then call session_confirm")
# pending with no candidate: confirming would register no project for good, so
# the header does not offer session_confirm
PENDING_NO_CANDIDATE_HEADER = (
    "project: none — this session is not registered, and the directory it was first "
    "observed in is not in any registered project, so global is read-only; to save, ask "
    "the user to run memriver project init there, then start a new session")
UNIDENTIFIED_HEADER = ("project: none — this session is not registered with memriver: its "
                       "hooks may not be installed (memriver install) or trusted (Codex asks "
                       "on start), or the harness sent no session id; ask the user to fix "
                       "that, then start a new session")
# a session row's project never re-resolves, so these two point at a new
# session rather than at the directory the session happens to be in
SESSION_NONE_HEADER = ("project: none — this session was registered with no project, so global "
                       "is read-only; to save, ask the user to run memriver project init, then "
                       "start a new session")
SESSION_PROJECT_GONE_HEADER = ("project: unavailable — this session's project no longer exists "
                               "or became global, so global is read-only; to save, ask the "
                               "user to start a new session")

# a new session with no row: these sources start one afresh, so it registers
# at once; resume/compact continue one memriver never saw, so it waits for
# the user (spec §5.2)
_FRESH_SOURCES = frozenset({"startup", "clear", "fork"})
_ENTRY_UNRESOLVABLE = "working directory could not be resolved"
_WORKTREE_UNMAPPED = "the git worktree could not be mapped to its main working tree"
_SESSION_PROJECT_MISSING = "session project missing"


class MemoryService:
    def __init__(self, memory_store: MemoryStore, project_store: ProjectStore,
                 content_policy_factory: Callable[[], ContentPolicy],
                 diagnostics: Diagnostics, *, session_store: SessionStore,
                 canonical_directory: Callable[[str], str | None],
                 main_tree_path: Callable[[str], str | None],
                 current_branch: Callable[[str], str | None],
                 root_is_intact: Callable[[str], bool], max_body_chars: int,
                 metadata_max_chars: int, search_limit_default: int, search_limit_max: int,
                 index_budget_lines: int, index_cue_chars: int, header_field_chars: int,
                 project_name_max_chars: int, session_prompt_chars: int,
                 session_recent_prompts: int, session_prompt_scan_max_bytes: int,
                 stop_nudge_min_prompts: int, stop_nudge_interval_prompts: int,
                 session_search_limit_default: int, session_search_limit_max: int) -> None:
        self._memory_store = memory_store
        self._project_store = project_store
        self._session_store = session_store
        # the directory questions a session registration asks (spec §5.1),
        # injected so this layer runs no git and reads no filesystem
        self._canonical_directory = canonical_directory
        self._main_tree_path = main_tree_path
        self._current_branch = current_branch
        self._root_is_intact = root_is_intact
        # built on first use: a read-only caller (the Stop hook, doctor, the
        # human views) never pays for loading and compiling the scanner rules
        self._content_policy_factory = content_policy_factory
        self._content_policy: ContentPolicy | None = None
        self._diagnostics = diagnostics
        self._max_body_chars = max_body_chars
        # metadata keeps its own budget so that lowering the configured body
        # limit does not silently tighten harness/description acceptance
        self._metadata_max_chars = metadata_max_chars
        self._search_limit_default = search_limit_default
        self._search_limit_max = search_limit_max
        self._index_budget_lines = index_budget_lines
        self._index_cue_chars = index_cue_chars
        self._header_field_chars = header_field_chars
        self._project_name_max_chars = project_name_max_chars
        self._session_prompt_chars = session_prompt_chars
        self._session_recent_prompts = session_recent_prompts
        self._session_prompt_scan_max_bytes = session_prompt_scan_max_bytes
        self._stop_nudge_min_prompts = stop_nudge_min_prompts
        self._stop_nudge_interval_prompts = stop_nudge_interval_prompts
        self._session_search_limit_default = session_search_limit_default
        self._session_search_limit_max = session_search_limit_max

    def _policy(self) -> ContentPolicy:
        if self._content_policy is None:
            self._content_policy = self._content_policy_factory()
        return self._content_policy

    def _field(self, value: str) -> str:
        """One stored value as it may appear in the agent-facing header."""
        return single_line(value)[:self._header_field_chars]

    # --- project contexts ---

    def open_project_context(self, start: str) -> ProjectContext:
        """The header, state and read/write set for one directory. Never raises for a
        store problem: an unreadable store is an empty, clearly labelled project context."""
        try:
            resolution = self._project_store.resolve(start)
            global_project_id = self._project_store.global_project_id()
        except StorageFailure:
            return _unavailable_context(None)
        project = resolution.project
        if resolution.state == "registered" and project is not None \
                and project.id != global_project_id:
            return self._registered_context(project, global_project_id)
        if resolution.state == "degraded":
            return self._degraded_context(resolution.diagnostic or "", global_project_id)
        return _no_project_context("none", NONE_HEADER, global_project_id)

    def _registered_context(self, project: Project, global_project_id: str | None,
                            session_key: SessionKey | None = None) -> ProjectContext:
        return ProjectContext(
            "registered",
            f"project: {self._field(project.name)} [{project.id}] "
            f"(root {self._field(project.root or '')})",
            ReadWriteSet(project_id=project.id, global_project_id=global_project_id),
            project, session_key=session_key)

    def _degraded_context(self, diagnostic: str, global_project_id: str | None,
                          session_key: SessionKey | None = None) -> ProjectContext:
        return ProjectContext(
            "degraded",
            f"project: unavailable — this directory could not be matched to one project "
            f"({self._field(diagnostic)}); ask the user to run memriver project explain",
            ReadWriteSet(project_id=None, global_project_id=global_project_id),
            diagnostic=diagnostic, session_key=session_key)

    # --- sessions (spec §5) ---

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
            return _unavailable_context(key)

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
            return _unavailable_context(key), False

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

    def session_context(self, key: SessionKey | None) -> ProjectContext:
        """The context a session's stored row grants. Never resolves a directory."""
        try:
            stored = None if key is None else self._session_store.get(key)
            if stored is None:
                return _no_project_context("unidentified", UNIDENTIFIED_HEADER,
                                           self._project_store.global_project_id(), key)
            return self._context_of(stored)
        except StorageFailure:
            return _unavailable_context(key)

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
        """The human read: every project's sessions (or one's), pending ones included."""
        return self._session_store.search(project_id, query,
                                          sys.maxsize if limit is None else limit)

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
            return _no_project_context("pending", header, global_project_id, session.key)
        if session.project_id is None:
            return _no_project_context("none", SESSION_NONE_HEADER, global_project_id,
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
        return self._registered_context(project, global_project_id, session.key)

    def _storeless_context(self, key: SessionKey) -> ProjectContext:
        # no store: nothing was written, and no project exists to grant
        return _no_project_context("none", NONE_HEADER, None, key)

    def _new_session(self, key: SessionKey, entry_dir: str, transcript_path: str | None, *,
                     fresh: bool) -> Session | ProjectContext:
        """The row a new session gets from its entry directory (spec §5.1, §5.2).

        A context instead when the directory cannot be resolved: nothing is
        registered, and the next event tries again.
        """
        # only the canonical spelling is registered and walked: an alias (a
        # symlink) belongs where it points, never where it is written
        entry = self._canonical_directory(entry_dir)
        mapped = None if entry is None else self._main_tree_path(entry)
        if entry is None or mapped is None:
            return self._degraded_context(
                _ENTRY_UNRESOLVABLE if entry is None else _WORKTREE_UNMAPPED,
                self._project_store.global_project_id(), key)
        resolution = self._project_store.resolve(entry, logical=mapped)
        if resolution.state == "degraded":
            return self._degraded_context(resolution.diagnostic or "",
                                          self._project_store.global_project_id(), key)
        project = resolution.project if resolution.state == "registered" else None
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

    # --- projects ---

    def global_project_id(self) -> str | None:
        return self._project_store.global_project_id()

    def ensure_global(self) -> str:
        try:
            return self._project_store.ensure_global()
        except IdCollision as err:
            raise StorageFailure from err

    def read_project(self, project_id: str) -> Project:
        return self._project_store.read(project_id)

    def list_projects(self) -> list[Project]:
        return self._project_store.list_projects()

    def plan_root(self, directory: str, project_id: str | None = None) -> RootPlan:
        return self._project_store.plan_root(directory, project_id)

    def init_project(self, name: str, plan: RootPlan) -> Project:
        project = Project.new(name, max_chars=self._project_name_max_chars)
        try:
            self._project_store.create(project, plan)
        except IdCollision as err:
            raise StorageFailure from err
        return Project(id=project.id, name=project.name, root=plan.root)

    def adopt(self, project_id: str, plan: RootPlan) -> None:
        self._project_store.bind(project_id, plan)

    def plan_unbind(self, project_id: str, root: str,
                    cwd: str) -> tuple[UnbindPlan, Resolution]:
        return self._project_store.plan_unbind(project_id, root, cwd)

    def unbind(self, plan: UnbindPlan) -> None:
        self._project_store.unbind(plan)

    # --- single memories ---

    def _refuse_pending(self, context: ProjectContext) -> None:
        if context.state == "pending":
            raise ProjectUnavailable(reason="pending")

    def _mark_saved(self, context: ProjectContext) -> None:
        """The save watermark (spec §3.3): best effort, never fails the save."""
        if context.session_key is not None:
            try:
                self._session_store.mark_saved(context.session_key)
            except StorageFailure:
                pass

    def record(self, *, content: str, type: str, sync: bool, harness: str,
               description: str, context: ProjectContext) -> Memory:
        self._refuse_pending(context)
        read_write_set = context.read_write_set
        if read_write_set.project_id is None:
            # no reason: the transport resolved the project, so it says why
            # there is none
            raise ProjectUnavailable()
        if not _HARNESS_RE.fullmatch(harness):
            raise ContentRejected("invalid harness identifier "
                                  "(allowed: letters, digits, ., _, -, max 64 chars)")
        policy = self._policy()
        policy.check(harness, self._metadata_max_chars)
        policy.check(content, self._max_body_chars)
        # description is persisted verbatim too, and only checked when
        # non-empty since it is optional and the policy refuses ""
        if description.strip():
            policy.check(description, self._metadata_max_chars)
        memory = Memory.new(body=content, type=type, project_id=read_write_set.project_id,
                            sync=sync, description=description,
                            source={"harness": harness, "method": "agent"})
        try:
            self._memory_store.record(memory, read_write_set)
        except IdCollision as err:
            raise StorageFailure from err
        self._mark_saved(context)
        return memory

    def read(self, memory_id: str, context: ProjectContext) -> Memory:
        memory = self._memory_store.read(memory_id, context.read_write_set)
        try:
            self._memory_store.touch_read(memory.id, now())
        except StorageFailure:
            pass                            # best effort (spec §3.3): never fails the read
        return memory

    def update(self, memory_id: str, content: str, context: ProjectContext, *,
               expected_version: int, description: str | None = None) -> Memory:
        self._refuse_pending(context)
        policy = self._policy()
        policy.check(content, self._max_body_chars)
        if description is not None and description.strip():
            policy.check(description, self._metadata_max_chars)
        memory = self._memory_store.update(memory_id, context.read_write_set,
                                           expected_version=expected_version, body=content,
                                           description=description)
        self._mark_saved(context)
        return memory

    def delete(self, memory_id: str, context: ProjectContext, *, expected_version: int,
               hard: bool = False) -> int:
        self._refuse_pending(context)
        return self._memory_store.delete(memory_id, context.read_write_set,
                                         expected_version=expected_version, hard=hard)

    # --- collections ---

    def normalize_search_limit(self, limit: int | None) -> int:
        """The one clamp for an agent-supplied limit: default when None, then 1..max."""
        limit = self._search_limit_default if limit is None else limit
        return max(1, min(limit, self._search_limit_max))

    def search(self, query: str, context: ProjectContext,
               limit: int | None = None) -> list[Memory]:
        """The current project first, then global, in one budget.

        One clamp for the whole answer, then two single-project searches; a
        spent budget skips global rather than being clamped back to one.
        """
        read_write_set = context.read_write_set
        remaining = self.normalize_search_limit(limit)
        hits: list[Memory] = []
        for project_id in (read_write_set.project_id, read_write_set.global_project_id):
            if project_id is None or remaining == 0:
                continue
            found = self._project_store.search(project_id, read_write_set, query=query,
                                               limit=remaining)
            hits += found
            remaining -= len(found)
        return hits

    def _index_line(self, memory: Memory, tag: str) -> str:
        # an empty body must not break the index, and every stored field is
        # normalized on its own before the line is composed so one field's
        # characters never land in another's budget
        raw_cue = memory.description or (memory.body.splitlines() or [""])[0]
        cue = single_line(raw_cue)[:self._index_cue_chars]
        return (f"- [{memory.type}{tag}] {single_line(memory.id)}: {cue} "
                f"({single_line(memory.updated[:10])})")

    def index(self, context: ProjectContext) -> str:
        """The current project's entries, then global's, in one line budget."""
        read_write_set = context.read_write_set
        sections = [(project_id, tag) for project_id, tag in
                    ((read_write_set.project_id, ""),
                     (read_write_set.global_project_id, ", global"))
                    if project_id is not None]
        listed = [(tag, self._project_store.search(project_id, read_write_set,
                                                   query=None, limit=None))
                  for project_id, tag in sections]
        total = sum(len(memories) for _, memories in listed)
        if total == 0:
            return EMPTY_INDEX
        lines: list[str] = []
        for tag, memories in listed:
            room = self._index_budget_lines - len(lines)
            lines += [self._index_line(m, tag) for m in memories[:max(room, 0)]]
        if total > len(lines):
            lines.append(f"… ({total - len(lines)} more entries omitted; use memory_search)")
        return "\n".join(lines)

    # --- management reads (the human CLI; never reachable from MCP) ---

    def show(self, memory_id: str, *, include_deleted: bool = False) -> Memory:
        return self._memory_store.read_any(memory_id, include_deleted=include_deleted)

    def list_memories(self, project_id: str | None = None) -> list[tuple[Project, list[Memory]]]:
        """Every project (or one) with its active memories, for the human views.

        Each project is read in its own transaction, so a multi-project listing
        or export is not a single cross-project snapshot.
        """
        projects = self._project_store.list_projects() if project_id is None \
            else [self._project_store.read(project_id)]
        return [(project, self._project_store.search(project.id, None, query=None, limit=None))
                for project in projects]

    def search_all(self, query: str, project_id: str | None = None,
                   limit: int | None = None) -> list[Memory]:
        """Matches across every project (or one), newest first.

        Each project is read in its own transaction, so a multi-project search
        is not a single cross-project snapshot.
        """
        projects = self._project_store.list_projects() if project_id is None \
            else [self._project_store.read(project_id)]
        hits = [m for project in projects
                for m in self._project_store.search(project.id, None, query=query, limit=None)]
        hits.sort(key=lambda m: (m.updated, m.id), reverse=True)
        return hits if limit is None else hits[:limit]

    def diagnose(self, *, now: str | None = None, stale_days: int = 90,
                 jaccard_threshold: float = 0.6) -> DiagnosticsReport:
        return self._diagnostics.run(now=now, stale_days=stale_days,
                                     jaccard_threshold=jaccard_threshold)


def _no_project_context(state: str, header: str, global_project_id: str | None,
                        session_key: SessionKey | None = None) -> ProjectContext:
    """A context that may read global only."""
    return ProjectContext(state, header,
                          ReadWriteSet(project_id=None, global_project_id=global_project_id),
                          session_key=session_key)


def _unavailable_context(session_key: SessionKey | None) -> ProjectContext:
    return ProjectContext("unavailable", STORE_UNREADABLE_HEADER,
                          ReadWriteSet(project_id=None, global_project_id=None),
                          session_key=session_key)
