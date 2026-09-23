"""The application facade: orchestrates the stores, the content policy and the limits.

MemoryStore owns single-memory actions and ProjectStore owns collections and
directories; this facade sequences policy checks, builds sessions and
read/write sets, renders the index and the project header, and hands the
CLI its planning and management reads. Nothing here touches a file, a table
or settings: every limit is injected.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from memriver_core.models import (
    DiagnosticsReport,
    Memory,
    Project,
    ReadWriteSet,
    Resolution,
    RootPlan,
    Session,
    UnbindPlan,
    single_line,
)
from memriver_core.models.errors import (
    ContentRejected,
    IdCollision,
    ProjectUnavailable,
    StorageFailure,
)

if TYPE_CHECKING:
    from memriver_core.content_policy.protocol import ContentPolicy
    from memriver_core.repository.protocol import MemoryStore, ProjectStore


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

# The session header's fixed lines; the registered and degraded lines are
# composed per session in open_session.
NONE_HEADER = "project: none — global is read-only; ask the user to run memriver project init"
STORE_UNREADABLE_HEADER = ("project: unavailable — the memory store could not be read; "
                           "ask the user to run memriver doctor")


class MemoryService:
    def __init__(self, memory_store: MemoryStore, project_store: ProjectStore,
                 content_policy_factory: Callable[[], ContentPolicy],
                 diagnostics: Diagnostics, *, max_body_chars: int,
                 metadata_max_chars: int, search_limit_default: int, search_limit_max: int,
                 index_budget_lines: int, index_cue_chars: int, header_field_chars: int,
                 project_name_max_chars: int) -> None:
        self._memory_store = memory_store
        self._project_store = project_store
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

    def _policy(self) -> ContentPolicy:
        if self._content_policy is None:
            self._content_policy = self._content_policy_factory()
        return self._content_policy

    def _field(self, value: str) -> str:
        """One stored value as it may appear in the agent-facing header."""
        return single_line(value)[:self._header_field_chars]

    # --- sessions ---

    def open_session(self, start: str) -> Session:
        """The header, state and read/write set for one directory. Never raises for a
        store problem: an unreadable store is an empty, clearly labelled session."""
        try:
            resolution = self._project_store.resolve(start)
            global_project_id = self._project_store.global_project_id()
        except StorageFailure:
            return Session("unavailable", STORE_UNREADABLE_HEADER,
                           ReadWriteSet(project_id=None, global_project_id=None))
        project = resolution.project
        if resolution.state == "registered" and project is not None \
                and project.id != global_project_id:
            return Session(
                "registered",
                f"project: {self._field(project.name)} [{project.id}] "
                f"(root {self._field(project.root or '')})",
                ReadWriteSet(project_id=project.id, global_project_id=global_project_id),
                project)
        read_write_set = ReadWriteSet(project_id=None, global_project_id=global_project_id)
        if resolution.state == "degraded":
            diagnostic = resolution.diagnostic or ""
            return Session(
                "degraded",
                f"project: unavailable — this directory could not be matched to one project "
                f"({self._field(diagnostic)}); ask the user to run memriver project explain",
                read_write_set, diagnostic=diagnostic)
        return Session("none", NONE_HEADER, read_write_set)

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

    def record(self, *, content: str, type: str, sync: bool, harness: str,
               description: str, read_write_set: ReadWriteSet) -> Memory:
        if read_write_set.project_id is None:
            # path-free on purpose, and one message for every cause: the
            # transport resolved the project, so it says why there is none
            raise ProjectUnavailable("no writable project in this session")
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
        return memory

    def read(self, memory_id: str, read_write_set: ReadWriteSet) -> Memory:
        return self._memory_store.read(memory_id, read_write_set)

    def update(self, memory_id: str, content: str, read_write_set: ReadWriteSet, *,
               expected_version: int, description: str | None = None) -> Memory:
        policy = self._policy()
        policy.check(content, self._max_body_chars)
        if description is not None and description.strip():
            policy.check(description, self._metadata_max_chars)
        return self._memory_store.update(memory_id, read_write_set,
                                         expected_version=expected_version, body=content,
                                         description=description)

    def delete(self, memory_id: str, read_write_set: ReadWriteSet, *, expected_version: int,
               hard: bool = False) -> int:
        return self._memory_store.delete(memory_id, read_write_set,
                                         expected_version=expected_version, hard=hard)

    # --- collections ---

    def normalize_search_limit(self, limit: int | None) -> int:
        """The one clamp for an agent-supplied limit: default when None, then 1..max."""
        limit = self._search_limit_default if limit is None else limit
        return max(1, min(limit, self._search_limit_max))

    def search(self, query: str, read_write_set: ReadWriteSet,
               limit: int | None = None) -> list[Memory]:
        """The current project first, then global, in one budget.

        One clamp for the whole answer, then two single-project searches; a
        spent budget skips global rather than being clamped back to one.
        """
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

    def index(self, read_write_set: ReadWriteSet) -> str:
        """The current project's entries, then global's, in one line budget."""
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
        projects = self._project_store.list_projects() if project_id is None \
            else [self._project_store.read(project_id)]
        return [(project, self._project_store.search(project.id, None, query=None, limit=None))
                for project in projects]

    def search_all(self, query: str, project_id: str | None = None,
                   limit: int | None = None) -> list[Memory]:
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
