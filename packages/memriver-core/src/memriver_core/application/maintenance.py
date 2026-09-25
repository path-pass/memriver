"""The maintenance facade: everything the dream run reads and writes, nothing an agent reaches.

The second public core facade, an explicit exception to the single-facade
rule (like the store purge): MemoryService, the agent and human facade,
never gains global content writes -- creating or rewriting a global entry,
or the maintenance run's soft deletes of one; they exist only here, composed
(bootstrap.build_maintenance_service) for the maintenance run alone -- the
MCP server never builds it. (The human CLI's management delete of a global
entry by id is MemoryService.delete_global, a separate rule.) No read touches
last_read_at: maintenance never counts as use. Nothing here touches a file, a
table or settings: every limit is injected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from memriver_core.models import Candidate, Change, Memory, Project, SourceRef
from memriver_core.models.errors import ContentRejected

from . import LazyPolicy

if TYPE_CHECKING:
    from memriver_core.content_policy.protocol import ContentPolicy
    from memriver_core.repository.protocol import (
        MaintenanceStore,
        ProjectStore,
        SessionStore,
    )


class MaintenanceService:
    def __init__(self, maintenance_store: MaintenanceStore, project_store: ProjectStore,
                 session_store: SessionStore,
                 content_policy_factory: Callable[[], ContentPolicy], *,
                 max_body_chars: int, metadata_max_chars: int) -> None:
        self._maintenance_store = maintenance_store
        self._project_store = project_store
        self._session_store = session_store
        self._policy_cache = LazyPolicy(content_policy_factory)
        self._max_body_chars = max_body_chars
        self._metadata_max_chars = metadata_max_chars

    def _rule_of(self, text: str) -> str | None:
        """The content-policy rule `text` breaks, or None.

        Size is never the reason (the budget is the text itself), and an empty
        text breaks nothing: an optional description may be empty.
        """
        if not text.strip():
            return None
        try:
            self._policy_cache.get().check(text, len(text))
        except ContentRejected as err:
            return err.rule_id or "content-policy"
        return None

    # --- reads (none touches last_read_at) ---

    def projects(self) -> list[Project]:
        return self._project_store.list_projects()

    def global_project_id(self) -> str | None:
        return self._project_store.global_project_id()

    def memories(self, project_id: str) -> list[Memory]:
        return self._maintenance_store.memories(project_id)

    def sources_of(self, memory_id: str) -> list[SourceRef]:
        return self._maintenance_store.sources_of(memory_id)

    def derived_from(self, memory_id: str) -> list[str]:
        return self._maintenance_store.derived_from(memory_id)

    def ttl_candidates(self, now: str, ttl_days: int, multiplier_max: int,
                       limit: int) -> list[Candidate]:
        return self._maintenance_store.ttl_candidates(now=now, ttl_days=ttl_days,
                                                      multiplier_max=multiplier_max,
                                                      limit=limit)

    def fingerprint_of(self, scope: str) -> str | None:
        return self._maintenance_store.fingerprint_of(scope)

    def changes(self, limit: int) -> list[Change]:
        return self._maintenance_store.changes(limit)

    def passes_policy(self, memory: Memory) -> bool:
        """Whether a memory may be sent to an executor: description and body pass."""
        return self._rule_of(memory.description) is None and self._rule_of(memory.body) is None

    def text_passes_policy(self, text: str) -> bool:
        """Whether one text (a transcript record, a cue) may leave the store."""
        return self._rule_of(text) is None
