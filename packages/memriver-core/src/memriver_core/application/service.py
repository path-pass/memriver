"""The application facade: orchestrates the two stores, the content policy and the limits.

MemoryStore owns single-memory actions and ProjectStore owns collections;
this facade only sequences policy checks, builds read/write sets and renders
the index. Nothing here knows about files, frontmatter, git, or
configuration.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from memriver_core.models import ID_RE, Memory, Project, ReadWriteSet, single_line
from memriver_core.models.errors import (
    ContentRejected,
    IdCollision,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)

if TYPE_CHECKING:
    from memriver_core.content_policy.protocol import ContentPolicy
    from memriver_core.repository.protocol import MemoryStore, ProjectStore

# 'harness' is persisted verbatim into the stored memory, so without this it
# is a policy-free channel for secrets or megabytes of text. The shape check
# caps size and charset; the content policy then rejects the values that still
# look like credentials. Neither error echoes the rejected value.
_HARNESS_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# What MemoryService.index returns when nothing is visible -- the single
# source transports compare against.
EMPTY_INDEX = "(no memories yet)"

# the index is injected into every session: one short cue per entry
_CUE_CHARS = 60


def _index_line(memory: Memory, tag: str) -> str:
    # stored memories are hand-editable, so an empty body must not break the
    # index, and every stored field is normalized on its own before the line
    # is composed so one field's characters never land in another's budget
    raw_cue = memory.description or (memory.body.splitlines() or [""])[0]
    cue = single_line(raw_cue)[:_CUE_CHARS]
    return (f"- [{memory.type}{tag}] {single_line(memory.id)}: {cue} "
            f"({single_line(memory.updated[:10])})")


class MemoryService:
    def __init__(self, memory_store: MemoryStore, project_store: ProjectStore,
                 content_policy: ContentPolicy, *, max_body_chars: int,
                 metadata_max_chars: int, search_limit_default: int,
                 search_limit_max: int, index_budget_lines: int) -> None:
        self._memory_store = memory_store
        self._project_store = project_store
        self._content_policy = content_policy
        self._max_body_chars = max_body_chars
        # metadata keeps its own budget so that lowering the configured body
        # limit does not silently tighten harness/description acceptance
        self._metadata_max_chars = metadata_max_chars
        self._search_limit_default = search_limit_default
        self._search_limit_max = search_limit_max
        self._index_budget_lines = index_budget_lines

    # --- sessions and projects ---

    def read_write_set(self, project_id: str | None) -> ReadWriteSet:
        """The read/write set a session may act in; only an existing, non-global project is kept."""
        global_project_id = self._project_store.global_project_id()
        kept = None
        if project_id is not None and project_id != global_project_id \
                and ID_RE.fullmatch(project_id):
            try:
                self._project_store.read(project_id)
                kept = project_id
            except ProjectNotFound:
                kept = None
        return ReadWriteSet(project_id=kept, global_project_id=global_project_id)

    def global_project_id(self) -> str | None:
        return self._project_store.global_project_id()

    def ensure_global(self) -> str:
        # the store takes its lock and re-reads the manifest itself, so a
        # global created by a peer in between is returned, not duplicated
        try:
            return self._project_store.ensure_global()
        except IdCollision as err:
            raise StorageFailure from err

    def create_project(self, name: str) -> Project:
        project = Project.new(name)
        try:
            self._project_store.create(project)
        except IdCollision as err:
            raise StorageFailure from err
        return project

    def read_project(self, project_id: str) -> Project:
        return self._project_store.read(project_id)

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
        self._content_policy.check(harness, self._metadata_max_chars)
        self._content_policy.check(content, self._max_body_chars)
        # description is persisted verbatim too, and only checked when
        # non-empty since it is optional and the policy refuses ""
        if description.strip():
            self._content_policy.check(description, self._metadata_max_chars)
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

    def update(self, memory_id: str, content: str, read_write_set: ReadWriteSet,
               description: str | None = None) -> Memory:
        self._content_policy.check(content, self._max_body_chars)
        if description is not None and description.strip():
            self._content_policy.check(description, self._metadata_max_chars)
        return self._memory_store.update(memory_id, read_write_set, body=content,
                                         description=description)

    def delete(self, memory_id: str, read_write_set: ReadWriteSet) -> None:
        self._memory_store.delete(memory_id, read_write_set)

    # --- collections ---

    def normalize_search_limit(self, limit: int | None) -> int:
        """The one clamp for an agent-supplied limit: default when None, then 1..max."""
        limit = self._search_limit_default if limit is None else limit
        return max(1, min(limit, self._search_limit_max))

    def search(self, project_id: str, query: str, read_write_set: ReadWriteSet,
               limit: int | None = None) -> list[Memory]:
        # the store answers exactly what it is asked for; clamping is ours
        return self._project_store.search(project_id, read_write_set, query=query,
                                          limit=self.normalize_search_limit(limit))

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
            lines += [_index_line(m, tag) for m in memories[:max(room, 0)]]
        if total > len(lines):
            lines.append(f"… ({total - len(lines)} more entries omitted; use memory_search)")
        return "\n".join(lines)
