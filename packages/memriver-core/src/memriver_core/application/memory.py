"""MemoryService: single memories, search, the index and the management reads.

MemoryStore owns single-memory actions and ProjectStore owns collections;
this service sequences policy checks and renders the index. The session
facts a write needs (a pending session is refused; a save marks the session)
come through two callbacks bootstrap injects, so this service never reads the
session store. Nothing here touches a file, a table or settings: every limit
is injected.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from memriver_core.models import Memory, Project, ProjectContext, now, single_line
from memriver_core.models.changes import Change, Create, Op, SoftDelete, Update
from memriver_core.models.errors import (
    ContentRejected,
    IdCollision,
    MemoryNotFound,
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


class MemoryService:
    def __init__(self, memory_store: MemoryStore, project_store: ProjectStore,
                 content_policy_factory: Callable[[], ContentPolicy], *,
                 refuse_pending: Callable[[ProjectContext], None],
                 mark_saved: Callable[[ProjectContext], None], max_body_chars: int,
                 metadata_max_chars: int, search_limit_default: int, search_limit_max: int,
                 index_budget_lines: int, index_cue_chars: int) -> None:
        self._memory_store = memory_store
        self._project_store = project_store
        # built on first use: a read-only caller (the Stop hook, doctor, the
        # human views) never pays for loading and compiling the scanner rules
        self._content_policy_factory = content_policy_factory
        self._content_policy: ContentPolicy | None = None
        # SessionService's, injected by bootstrap: a write from a pending
        # session is refused, and a save moves the session's watermark
        self._refuse_pending = refuse_pending
        self._mark_saved = mark_saved
        self._max_body_chars = max_body_chars
        # metadata keeps its own budget so that lowering the configured body
        # limit does not silently tighten harness/description acceptance
        self._metadata_max_chars = metadata_max_chars
        self._search_limit_default = search_limit_default
        self._search_limit_max = search_limit_max
        self._index_budget_lines = index_budget_lines
        self._index_cue_chars = index_cue_chars

    def _policy(self) -> ContentPolicy:
        if self._content_policy is None:
            self._content_policy = self._content_policy_factory()
        return self._content_policy

    # --- single memories ---

    def record(self, *, content: str, type: str, sync: bool, harness: str, description: str,
               context: ProjectContext, changed_by: str = "mcp") -> Memory:
        """The agent write: one Create in the context's project; `harness` is `changed_via`."""
        self._refuse_pending(context)
        read_write_set = context.read_write_set
        if read_write_set.project_id is None:
            # no reason: the transport resolved the project, so it says why
            # there is none
            raise ProjectUnavailable()
        if not _HARNESS_RE.fullmatch(harness):
            raise ContentRejected("invalid harness identifier "
                                  "(allowed: letters, digits, ., _, -, max 64 chars)",
                                  rule_id="invalid-harness")
        policy = self._policy()
        policy.check(harness, self._metadata_max_chars)
        # checked here first so an agent reads the policy's own words; the
        # kernel checks the resulting state again inside its transaction.
        # description is only checked when non-empty: it is optional, and
        # the policy refuses ""
        policy.check(content, self._max_body_chars)
        if description.strip():
            policy.check(description, self._metadata_max_chars)
        create = Create(read_write_set.project_id, type, description, content, sync=sync)
        try:
            memory = self._memory_store.write(create, restriction=read_write_set,
                                              changed_by=changed_by, changed_via=harness,
                                              check=self._check_state)
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
               expected_version: int, description: str | None = None,
               changed_by: str = "mcp", changed_via: str | None = None) -> Memory:
        """The agent edit; the memory as this call wrote it. Text equal to the current
        text is a checked no-op: the restriction, existence, deleted state and version
        are checked in the write transaction, and the memory as checked is returned with
        no version or change."""
        self._refuse_pending(context)
        policy = self._policy()
        policy.check(content, self._max_body_chars)
        if description is not None and description.strip():
            policy.check(description, self._metadata_max_chars)
        edit = Update(memory_id, expected_version, description=description, body=content)
        memory = self._memory_store.write(edit, restriction=context.read_write_set,
                                          changed_by=changed_by, changed_via=changed_via,
                                          check=self._check_state)
        self._mark_saved(context)
        return memory

    def delete(self, memory_id: str, context: ProjectContext, *, expected_version: int,
               changed_by: str = "mcp", changed_via: str | None = None) -> int:
        """The agent soft delete; the new version. Never marks the session saved."""
        self._refuse_pending(context)
        memory = self._memory_store.write(SoftDelete(memory_id, expected_version),
                                          restriction=context.read_write_set,
                                          changed_by=changed_by, changed_via=changed_via,
                                          check=self._check_state)
        return memory.version

    def _check_state(self, description: str, body: str) -> str | None:
        """The content policy on one resulting state (the kernel's `check`): a rule id or None."""
        policy = self._policy()
        try:
            policy.check(body, self._max_body_chars)
            if description.strip():
                policy.check(description, self._metadata_max_chars)
        except ContentRejected as err:
            return err.rule_id or "rejected"
        return None

    def apply(self, ops: Sequence[Op], *, changed_by: str,
              changed_via: str | None = None) -> Change:
        """The management write path (spec §4.1): every op, or none, as one change.

        Never reachable from MCP and never marks a session saved. `changed_by`
        and `changed_via` come from the calling entry point, never from a
        model's output.
        """
        try:
            return self._memory_store.apply(ops, changed_by=changed_by, changed_via=changed_via,
                                            check=self._check_state)
        except IdCollision as err:
            raise StorageFailure from err

    def delete_global(self, memory_id: str, *, expected_version: int,
                      changed_by: str = "human") -> int:
        """Soft-delete a live global memory by id through `apply`; the new version.

        `MemoryNotFound` when the id is not a live global memory.
        """
        memory = self._memory_store.read_any(memory_id, include_deleted=False)
        if memory.project_id != self._project_store.global_project_id():
            raise MemoryNotFound(memory_id)
        change = self.apply([SoftDelete(memory_id, expected_version)], changed_by=changed_by)
        return change.steps[0].after_version

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
