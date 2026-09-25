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

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING

from memriver_core.models import (
    HARNESS_RE,
    Candidate,
    Change,
    ChangeGroup,
    CreateOp,
    Memory,
    Project,
    SoftDeleteOp,
    SourceRef,
    UndoResult,
    UpdateOp,
    new_id,
    single_line,
)
from memriver_core.models import now as _now
from memriver_core.models.errors import ContentRejected, IdCollision, StorageFailure

from . import LazyPolicy

if TYPE_CHECKING:
    from memriver_core.content_policy.protocol import ContentPolicy
    from memriver_core.repository.protocol import (
        MaintenanceStore,
        ProjectStore,
        SessionStore,
    )


# kind -> (the operation it allows, the fewest sources it needs) -- spec §4.2
_SHAPES: dict[str, tuple[tuple[type, ...], int]] = {
    "merge": ((CreateOp,), 2), "rewrite": ((UpdateOp,), 1),
    "extract": ((CreateOp, UpdateOp), 1), "unsafe": ((SoftDeleteOp,), 0)}


def _check_shape(group: ChangeGroup) -> None:
    """The kind rules, enforced by core rather than trusted to its caller."""
    if group.kind not in _SHAPES or len(group.ops) != 1:
        raise ValueError("a change group is one merge, rewrite, extract or unsafe operation")
    allowed, fewest = _SHAPES[group.kind]
    op = group.ops[0]
    if not isinstance(op, allowed) or len(getattr(op, "sources", ())) < fewest:
        raise ValueError(f"a {group.kind} group has the wrong operation or too few sources")


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
        text breaks nothing: an optional description may be empty. Emptiness is
        decided the same way the scanner decides it -- control characters do
        not count as content -- so a control-characters-only text reads as
        empty here too, rather than surfacing as a generic rejection.
        """
        if not single_line(text):
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

    # --- writes (each one short transaction) ---

    def apply_group(self, group: ChangeGroup) -> str:
        """Apply one change group atomically; its change id.

        ValueError when the group breaks the kind rules; ContentRejected when
        the harness, a body, a non-empty description or the reason fails the
        content policy or the size limits; GroupConflict when a precondition
        no longer holds. Either way nothing is written.
        """
        _check_shape(group)
        if not HARNESS_RE.fullmatch(group.harness):
            raise ContentRejected("invalid harness identifier "
                                  "(allowed: letters, digits, ., _, -, max 64 chars)")
        policy = self._policy_cache.get()
        policy.check(group.harness, self._metadata_max_chars)
        policy.check(group.reason, self._metadata_max_chars)
        for op in group.ops:
            if isinstance(op, SoftDeleteOp):
                continue
            policy.check(op.body, self._max_body_chars)
            # optional, so only a description with content is checked -- empty
            # decided the way _rule_of decides it
            if single_line(op.description):
                policy.check(op.description, self._metadata_max_chars)
        change_id = new_id()
        created_ids = tuple(new_id() for op in group.ops if isinstance(op, CreateOp))
        try:
            self._maintenance_store.apply_group(
                dataclasses.replace(group, reason=single_line(group.reason)),
                change_id=change_id, created_ids=created_ids, now=_now())
        except IdCollision as err:
            raise StorageFailure from err
        return change_id

    def set_fingerprint(self, scope: str, fingerprint: str, now: str) -> None:
        self._maintenance_store.set_fingerprint(scope, fingerprint, now)

    def change(self, change_id: str) -> Change | None:
        return self._maintenance_store.change(change_id)

    def undo(self, change_id: str) -> UndoResult:
        return self._maintenance_store.undo(change_id, now=_now())
