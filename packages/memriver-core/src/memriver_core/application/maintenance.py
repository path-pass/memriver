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
    ID_RE,
    Candidate,
    Change,
    ChangeGroup,
    CreateOp,
    DreamRun,
    Memory,
    Project,
    Review,
    RunStatus,
    RunTrigger,
    Session,
    SessionKey,
    SoftDeleteOp,
    SourceRef,
    SummaryInput,
    SummaryProgress,
    SummaryStatus,
    UndoResult,
    UpdateOp,
    new_id,
    single_line,
    timestamp_shift,
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
    """The kind rules, enforced by core rather than trusted to its caller.

    Every id the group carries -- its project, the operation's own target or
    project, and each source -- is checked against the public id shape here,
    before any of it reaches the store: a malformed id must never surface as
    a `GroupConflict.ids` entry, as if it named a real, merely-stale row. A
    source repeated within the one operation is rejected the same way,
    rather than left for the store's own duplicate check to report as a
    conflict.
    """
    if group.kind not in _SHAPES or len(group.ops) != 1:
        raise ValueError("a change group is one merge, rewrite, extract or unsafe operation")
    allowed, fewest = _SHAPES[group.kind]
    op = group.ops[0]
    if not isinstance(op, allowed) or len(getattr(op, "sources", ())) < fewest:
        raise ValueError(f"a {group.kind} group has the wrong operation or too few sources")
    sources = getattr(op, "sources", ())
    source_ids = tuple(source_id for source_id, _ in sources)
    target_id = op.project_id if isinstance(op, CreateOp) else op.id
    if not all(ID_RE.fullmatch(identifier)
              for identifier in (group.project_id, target_id, *source_ids)):
        raise ValueError("a change group names a malformed project, target or source id")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("a change group repeats a source within one operation")


class MaintenanceService:
    def __init__(self, maintenance_store: MaintenanceStore, project_store: ProjectStore,
                 session_store: SessionStore,
                 content_policy_factory: Callable[[], ContentPolicy], *,
                 max_body_chars: int, metadata_max_chars: int,
                 summary_max_chars: int) -> None:
        self._maintenance_store = maintenance_store
        self._project_store = project_store
        self._session_store = session_store
        self._policy_cache = LazyPolicy(content_policy_factory)
        self._max_body_chars = max_body_chars
        self._metadata_max_chars = metadata_max_chars
        self._summary_max_chars = summary_max_chars

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

    def sessions_due_for_summary(self, now: str, idle_minutes: int,
                                 limit: int) -> list[Session]:
        return self._session_store.due_for_summary(
            timestamp_shift(now, minutes=-idle_minutes), limit)

    # --- writes (each one short transaction) ---

    def write_summary(self, key: SessionKey, *, expected_last_active_at: str,
                      summary: str | None, status: SummaryStatus,
                      summary_input: SummaryInput) -> SummaryStatus | None:
        """Store one summary outcome; the outcome actually stored, or None when the
        session moved on meanwhile and nothing was written.

        Only an "ok" outcome keeps text, and only text the content policy and
        the summary limit accept; anything else is stored as "omitted".
        """
        if status == "ok":
            try:
                self._policy_cache.get().check(summary or "", self._summary_max_chars)
            except ContentRejected:
                status, summary = "omitted", None
        else:
            summary = None
        written = self._session_store.write_summary(
            key, expected_last_active_at=expected_last_active_at, summary=summary,
            status=status, summary_input=summary_input, at=_now())
        return status if written is True else None

    def write_summary_progress(self, key: SessionKey, *, expected_last_active_at: str,
                               progress: SummaryProgress | None) -> bool:
        """Store or clear a long session's checkpoint. Every partial summary it holds
        passes the content policy first (ContentRejected otherwise, nothing stored)."""
        for partial in () if progress is None else progress.partials:
            self._policy_cache.get().check(partial, len(partial))
        return self._session_store.write_summary_progress(
            key, expected_last_active_at=expected_last_active_at, progress=progress,
            at=_now()) is True

    def mark_summary_attempt(self, key: SessionKey) -> None:
        self._session_store.mark_summary_attempt(key, _now())

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

    def retire(self, memory_id: str, *, judged_version: int, ttl_days: int,
              multiplier_max: int, now: str, review: Review) -> str | None:
        """The TTL soft delete after a model review: its change id, or None when the
        row moved meanwhile and nothing was written.

        ValueError when `review.decision` is not `"delete"`, or when the review does
        not name `(memory_id, judged_version)` -- nothing written either way."""
        if review.decision != "delete":
            raise ValueError("a retirement records a delete decision")
        self._policy_cache.get().check(review.reason, self._metadata_max_chars)
        change_id = new_id()
        retired = self._maintenance_store.retire(
            memory_id, judged_version=judged_version, ttl_days=ttl_days,
            multiplier_max=multiplier_max, now=now,
            review=dataclasses.replace(review, reason=single_line(review.reason)),
            change_id=change_id)
        return change_id if retired else None

    def record_review(self, review: Review) -> bool:
        """A keep or uncertain decision; it changes no memory (maintenance is not use).
        False when the judged version is no longer the active one: nothing recorded."""
        if review.decision not in ("keep", "uncertain"):
            raise ValueError("record_review records keep or uncertain")
        self._policy_cache.get().check(review.reason, self._metadata_max_chars)
        return self._maintenance_store.record_review(
            dataclasses.replace(review, reason=single_line(review.reason)))

    def quarantine_secrets(self, run_id: str, now: str, limit: int) -> list[Change]:
        """The safety re-scan: soft-delete every active memory the current policy refuses.

        Global included, no model involved. Each hit is its own transaction,
        re-checked at the version scanned; its change reason is the rule id,
        never the matched text (the before-image keeps the row for undo).
        """
        changes: list[Change] = []
        for memory in self._maintenance_store.active_memories():
            if len(changes) >= limit:
                break
            rule = self._rule_of(memory.description) or self._rule_of(memory.body)
            if rule is None:
                continue
            change = self._maintenance_store.quarantine(
                memory.id, expected_version=memory.version, run_id=run_id, rule_id=rule,
                change_id=new_id(), now=now)
            if change is not None:
                changes.append(change)
        return changes

    def start_run(self, trigger: RunTrigger, executor: str | None, now: str) -> str:
        """Record a run as running; any run still marked running died, and is failed."""
        run_id = new_id()
        self._maintenance_store.start_run(DreamRun(
            run_id=run_id, started_at=now, finished_at=None, trigger=trigger,
            executor=executor, status="running", report={}))
        return run_id

    def record_skipped_run(self, trigger: RunTrigger, executor: str | None, now: str) -> str:
        """A run that found the lock held: recorded, and the live run left alone."""
        run_id = new_id()
        self._maintenance_store.insert_run(DreamRun(
            run_id=run_id, started_at=now, finished_at=now, trigger=trigger,
            executor=executor, status="skipped", report={}))
        return run_id

    def finish_run(self, run_id: str, status: RunStatus, report: dict, now: str) -> None:
        self._maintenance_store.finish_run(run_id, status=status, report=report,
                                           finished_at=now)

    def runs(self, limit: int) -> list[DreamRun]:
        return self._maintenance_store.runs(limit)

    def run(self, run_id: str) -> DreamRun | None:
        return self._maintenance_store.run(run_id)

    def changes_of_run(self, run_id: str) -> list[Change]:
        """The groups a run committed, from the change log itself: a run killed after
        committing some still shows them, with their undo commands."""
        return self._maintenance_store.changes_of_run(run_id)
