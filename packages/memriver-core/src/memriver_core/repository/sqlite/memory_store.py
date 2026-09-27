"""`MemoryStore` over the SQLite database: current state, permanent history, the change log.

A `memories` row is a memory's current state; every state it ever had is a
`memory_versions` row with its complete `memory_sources` set; every write is
one `changes` row with one `change_steps` row per memory it touched. All of
it is written by `apply_ops`, inside one write transaction, so a refused
change leaves nothing behind.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

from memriver_core.models import (
    ID_RE,
    Memory,
    MemoryType,
    ReadWriteSet,
    Trust,
    is_timestamp,
    new_id,
    now,
    now_strictly_after,
)
from memriver_core.models.changes import (
    Change,
    Citation,
    Create,
    HardDeleteItem,
    HardDeletePlan,
    MemoryVersion,
    Op,
    OpName,
    PlanCitation,
    PolicyHit,
    Restore,
    SoftDelete,
    SourceRef,
    Step,
    Update,
    Usage,
)
from memriver_core.models.errors import (
    BatchConflict,
    ContentRejected,
    GlobalReadOnly,
    IdCollision,
    MemoryNotFound,
    PlanChanged,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
    UndoRefused,
    VersionConflict,
)

from .database import MEMORY_COLUMNS, Database, memory_from_row, memory_to_row

# the content policy on one resulting state: check(description, body) -> a rule id or None
Check = Callable[[str, str], str | None]

_JOINED_COLUMNS = ", ".join(f"m.{column.strip()}" for column in MEMORY_COLUMNS.split(","))
# joined to its project, so an orphan (a writer ignored foreign keys) answers
# like any other hidden id
_SELECT_JOINED = ("SELECT " + _JOINED_COLUMNS
                  + " FROM memories m JOIN projects p ON p.id = m.project_id WHERE m.id = ?")
# deleted rows are excluded in SQL, before any decoding: a damaged deleted row
# must answer exactly like an absent id, never as damage
_ACTIVE_ONLY = " AND m.deleted_at IS NULL"
_PLACEHOLDERS = ", ".join("?" for _ in MEMORY_COLUMNS.split(","))
_SELECT_ALL = ("SELECT " + _JOINED_COLUMNS
              + " FROM memories m JOIN projects p ON p.id = m.project_id")


def _addressable(memory_id: object) -> bool:
    return isinstance(memory_id, str) and bool(ID_RE.fullmatch(memory_id))


def _joined(conn: sqlite3.Connection | None, memory_id: str, *, include_deleted: bool,
            readable: frozenset[str] | None) -> Memory | None:
    """The row, decoded; None when absent or outside `readable` (None: any project).

    Authorization runs in SQL, before the driver decodes a single column: a
    damaged or undecodable row of a project the caller may not read answers
    like an absent id, never as damage.
    """
    if conn is None or (readable is not None and not readable):
        return None
    query = _SELECT_JOINED if include_deleted else _SELECT_JOINED + _ACTIVE_ONLY
    params: tuple[str, ...] = (memory_id,)
    if readable is not None:
        query += f" AND m.project_id IN ({', '.join('?' for _ in readable)})"
        params += tuple(readable)
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    try:
        return memory_from_row(row)
    except ValueError as err:
        raise StorageFailure from err       # damage is reported as damage, not absence


def _writable(conn: sqlite3.Connection, memory_id: str,
              read_write_set: ReadWriteSet) -> Memory:
    """Locate and authorize one agent target inside the write transaction."""
    memory = _joined(conn, memory_id, include_deleted=False,
                     readable=read_write_set.readable())
    if memory is None:
        raise MemoryNotFound(memory_id)
    if memory.project_id == read_write_set.global_project_id:
        # global is readable, so naming the rule reveals nothing; this is
        # only an early refusal from the cached id -- the database can be
        # replaced or restored under a running server, so the row's own
        # is_global, read just below, is the one that actually decides
        raise GlobalReadOnly()
    # `_joined` already required a project row to exist (it is a join)
    if conn.execute("SELECT is_global FROM projects WHERE id = ?",
                    (memory.project_id,)).fetchone()[0]:
        raise GlobalReadOnly()
    if memory.project_id not in read_write_set.writable():
        raise MemoryNotFound(memory_id)
    return memory


_TRUST_ORDER = ("untrusted-derived", "agent", "user")         # lowest first

# does a walk along source rows of any version, from (memory_id, version), reach the id?
_REACHES = """WITH RECURSIVE reach(id) AS (
  SELECT source_id FROM memory_sources WHERE memory_id = ? AND version = ?
  UNION
  SELECT s.source_id FROM memory_sources s JOIN reach r ON s.memory_id = r.id)
SELECT 1 FROM reach WHERE id = ? LIMIT 1"""


@dataclass(frozen=True)
class _State:
    """Everything one version records."""

    type: str
    trust: str
    sync: bool
    description: str
    body: str
    deleted: bool
    sources: tuple[SourceRef, ...]


@dataclass(frozen=True)
class _Batch:
    """One change being written, inside its write transaction."""

    conn: sqlite3.Connection
    restriction: ReadWriteSet | None        # None: the management path
    check: Check
    change_id: str
    # (version, deleted) before this change of every memory it has written so
    # far, None for one it created: what a newly cited source is admitted against
    before: dict[str, tuple[int, bool] | None] = field(default_factory=dict)
    # (index, memory_id, version) of every new version with sources: the cycle check
    cited: list[tuple[int, str, int]] = field(default_factory=list)


def apply_ops(conn: sqlite3.Connection, ops: Sequence[Op], *,
              restriction: ReadWriteSet | None, changed_by: str, changed_via: str | None,
              check: Check, undoes: str | None = None) -> Change:
    """Check and write `ops` as one change, inside the caller's write transaction.

    Every refusal raises before the caller commits, so a refused change
    leaves no change row, step, version or source behind.
    """
    ops = tuple(ops)
    if not ops:
        raise ValueError("a change needs at least one operation")
    if not all(isinstance(op, Op) for op in ops):
        raise TypeError("not an operation")
    if not (isinstance(changed_by, str) and 1 <= len(changed_by) <= 32):
        raise ValueError("changed_by is not 1 to 32 characters")
    if changed_via is not None and not (isinstance(changed_via, str)
                                        and 1 <= len(changed_via) <= 64):
        raise ValueError("changed_via is not 1 to 64 characters")
    named = [op.memory_id for op in ops if not isinstance(op, Create)]
    if len(set(named)) != len(named):
        raise ValueError("a change touches a memory at most once")
    change_id = new_id()
    if conn.execute("SELECT 1 FROM changes WHERE change_id = ?", (change_id,)).fetchone():
        raise IdCollision(change_id)
    at = now()
    # first: every version and step below references the change row
    conn.execute("INSERT INTO changes (change_id, at, changed_by, changed_via, step_count, "
                 "undoes) VALUES (?, ?, ?, ?, ?, ?)",
                 (change_id, at, changed_by, changed_via, len(ops), undoes))
    batch = _Batch(conn, restriction, check, change_id)
    steps = tuple(_apply_one(batch, index, op, changed_by=changed_by, changed_via=changed_via)
                  for index, op in enumerate(ops))
    _refuse_cycles(batch)
    conn.executemany("INSERT INTO change_steps (change_id, step, memory_id, op, before_version, "
                     "after_version) VALUES (?, ?, ?, ?, ?, ?)",
                     [(change_id, s.step, s.memory_id, s.op, s.before_version, s.after_version)
                      for s in steps])
    return Change(change_id, at, changed_by, changed_via, len(ops), undoes, steps)


def _apply_one(batch: _Batch, index: int, op: Op, *, changed_by: str,
               changed_via: str | None) -> Step:
    if isinstance(op, Create):
        return _create(batch, index, op, changed_by=changed_by, changed_via=changed_via)
    if isinstance(op, Update):
        return _update(batch, index, op)
    if isinstance(op, SoftDelete):
        return _soft_delete(batch, index, op)
    return _restore(batch, index, op)


def _create(batch: _Batch, index: int, op: Create, *, changed_by: str,
            changed_via: str | None) -> Step:
    _authorize_create(batch, op.project_id)
    sources = _admit(batch, index, None, (), op.sources)
    # with sources, trust and sync come from them alone (spec §3.3)
    trust, sync = _derived(batch, index, None, "user", True, sources) if sources \
        else (op.trust, op.sync)
    memory = Memory.new(body=op.body, type=op.type, project_id=op.project_id,
                        source={"harness": changed_via or "unknown", "method": changed_by},
                        trust=trust, sync=sync, description=op.description)
    row = memory_to_row(memory)
    # a row the read path would reject is never committed
    memory_from_row(row)
    rule = batch.check(memory.description, memory.body)
    if rule is not None:
        raise ContentRejected(rule_id=rule)
    # deleted rows keep their id: an id is never reused
    if batch.conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory.id,)).fetchone():
        raise IdCollision(memory.id)
    batch.conn.execute(f"INSERT INTO memories ({MEMORY_COLUMNS}) VALUES ({_PLACEHOLDERS})", row)
    batch.before[memory.id] = None
    _insert_version(batch, index, memory.id, 1,
                    _State(memory.type, memory.trust, memory.sync, memory.description,
                           memory.body, False, sources))
    return Step(index + 1, memory.id, "create", None, 1)


def _authorize_create(batch: _Batch, project_id: str) -> None:
    """The project a create lands in: exists, and on the agent path is writable, not global."""
    restriction = batch.restriction
    if restriction is not None:
        if project_id == restriction.global_project_id:
            raise GlobalReadOnly()
        if project_id not in restriction.writable():
            raise ProjectUnavailable()
    row = batch.conn.execute("SELECT is_global FROM projects WHERE id = ?",
                             (project_id,)).fetchone()
    if row is None:
        if restriction is not None:
            raise ProjectUnavailable()
        raise ProjectNotFound(project_id)
    # the cached global id above is only an early refusal: the role that
    # decides is the one on the row, read just now
    if restriction is not None and row[0]:
        raise GlobalReadOnly()


def _update(batch: _Batch, index: int, op: Update) -> Step:
    current = _target(batch, index, op)
    before = _state_of(batch.conn, current)
    sources = _admit(batch, index, current.id, before.sources, op.sources)
    trust, sync = _derived(batch, index, current.id, before.trust, before.sync, sources) \
        if sources else (before.trust, before.sync)
    after = replace(
        before, trust=trust, sync=sync, sources=sources,
        description=before.description if op.description is None else op.description.strip(),
        body=before.body if op.body is None else op.body.strip())
    return _write_version(batch, index, "update", current, before, after)


def _soft_delete(batch: _Batch, index: int, op: SoftDelete) -> Step:
    current = _target(batch, index, op)
    if op.unread_since is not None:
        if not is_timestamp(op.unread_since):
            raise ValueError("unread_since is not a timestamp")
        # both are the fixed-width form, so text order is time order
        if current.last_read_at is not None and current.last_read_at >= op.unread_since:
            raise BatchConflict(index, current.id, "read-since")
    before = _state_of(batch.conn, current)
    return _write_version(batch, index, "soft_delete", current, before,
                          replace(before, deleted=True))


def _restore(batch: _Batch, index: int, op: Restore) -> Step:
    """`to_version`'s recorded state -- its own trust and sync included -- as the next version."""
    current = _target(batch, index, op, allow_deleted=True)
    row = batch.conn.execute(
        "SELECT type, trust, sync, description, body, deleted FROM memory_versions "
        "WHERE memory_id = ? AND version = ?", (current.id, op.to_version)).fetchone()
    if row is None:
        raise BatchConflict(index, current.id, "missing")
    type_, trust, sync, description, body, deleted = _decode_history_state(*row)
    sources = _sources_of(batch.conn, current.id, op.to_version)
    # restored references go back as they were; each cited version must still exist
    for ref in sources:
        if batch.conn.execute("SELECT 1 FROM memory_versions WHERE memory_id = ? AND version = ?",
                              (ref.memory_id, ref.version)).fetchone() is None:
            raise BatchConflict(index, current.id, "source")
    after = _State(type_, trust, sync, description, body, deleted, sources)
    return _write_version(batch, index, "restore", current, _state_of(batch.conn, current),
                          after)


def _target(batch: _Batch, index: int, op: Update | SoftDelete | Restore, *,
            allow_deleted: bool = False) -> Memory:
    """The memory `op` names, located and authorized inside the transaction."""
    if batch.restriction is not None:
        # the agent path answers as it always has: an agent learns nothing new
        memory = _writable(batch.conn, op.memory_id, batch.restriction)
        if memory.version != op.expected_version:
            raise VersionConflict(op.memory_id)
        return memory
    memory = _joined(batch.conn, op.memory_id, include_deleted=True, readable=None) \
        if _addressable(op.memory_id) else None
    if memory is None:
        raise BatchConflict(index, op.memory_id, "missing")
    if memory.version != op.expected_version:
        raise BatchConflict(index, op.memory_id, "version")
    if memory.deleted_at is not None and not allow_deleted:
        raise BatchConflict(index, op.memory_id, "deleted")
    return memory


def _admit(batch: _Batch, index: int, memory_id: str | None,
           current: tuple[SourceRef, ...],
           requested: tuple[SourceRef, ...] | None) -> tuple[SourceRef, ...]:
    """The resulting source set of a create (`current` empty) or an update (spec §3.3).

    None keeps the current set. Otherwise a reference already in the current
    set is carried -- its version exists, whatever the source did since --
    and any other is newly cited: it must be the source's current,
    non-deleted version as it stood before this change.
    """
    if requested is None:
        return current
    refs = tuple(sorted(requested, key=lambda ref: ref.memory_id))
    cited = [ref.memory_id for ref in refs]
    if len(set(cited)) != len(cited):
        raise ValueError("a source set cites a memory at most once")
    for ref in refs:
        if ref.memory_id == memory_id:
            raise BatchConflict(index, memory_id, "cycle")
        if ref not in current and _pre_batch(batch, ref.memory_id) != (ref.version, False):
            raise BatchConflict(index, memory_id, "source")
    return refs


def _pre_batch(batch: _Batch, memory_id: str) -> tuple[int, bool] | None:
    """(version, deleted) of a memory before this change; None when it did not exist."""
    if memory_id in batch.before:
        return batch.before[memory_id]
    row = batch.conn.execute("SELECT version, deleted_at IS NOT NULL FROM memories WHERE id = ?",
                             (memory_id,)).fetchone()
    return None if row is None else (row[0], bool(row[1]))


def _derived(batch: _Batch, index: int, memory_id: str | None, trust: str, sync: bool,
             sources: tuple[SourceRef, ...]) -> tuple[str, bool]:
    """The lowest of `trust` and every cited version's trust; sync only if all sync."""
    for ref in sources:
        row = batch.conn.execute("SELECT trust, sync FROM memory_versions "
                                 "WHERE memory_id = ? AND version = ?",
                                 (ref.memory_id, ref.version)).fetchone()
        if row is None:
            raise BatchConflict(index, memory_id, "source")
        trust = min(trust, row[0], key=_TRUST_ORDER.index)
        sync = sync and bool(row[1])
    return trust, sync


def _refuse_cycles(batch: _Batch) -> None:
    """No walk along the source rows of any version leads a memory back to itself."""
    for index, memory_id, version in batch.cited:
        if batch.conn.execute(_REACHES, (memory_id, version, memory_id)).fetchone():
            raise BatchConflict(index, memory_id, "cycle")


def _state_of(conn: sqlite3.Connection, memory: Memory) -> _State:
    return _State(memory.type, memory.trust, memory.sync, memory.description, memory.body,
                  memory.deleted_at is not None, _sources_of(conn, memory.id, memory.version))


def _sources_of(conn: sqlite3.Connection, memory_id: str,
                version: int) -> tuple[SourceRef, ...]:
    """A version's sources, decoded like `_decode_history_state`: `memory_sources` has
    no CHECK on `source_id`, and the connection's lenient text factory hands back
    undecodable TEXT as bytes rather than raising -- so a damaged source id must be
    caught here, not carried into a `SourceRef` a caller (`memriver history`'s
    `visible`) never expects to hold bytes. `StorageFailure` for a row memriver could
    not have written: damage is reported as damage, never silently handed out.
    """
    sources = []
    for source_id, source_version in conn.execute(
            "SELECT source_id, source_version FROM memory_sources "
            "WHERE memory_id = ? AND version = ? ORDER BY source_id", (memory_id, version)):
        if not _addressable(source_id):
            raise StorageFailure
        sources.append(SourceRef(source_id, source_version))
    return tuple(sources)


def _decode_history_state(type_: object, trust: object, sync: object, description: object,
                          body: object, deleted: object) -> tuple[str, str, bool, str, str, bool]:
    """A `memory_versions` row's state, validated like `memory_from_row`.

    `memory_versions` has no CHECK on `type`/`trust`, and the connection's lenient
    text factory hands back undecodable TEXT as bytes rather than raising -- so a
    damaged history row must be caught here, not decoded into a `MemoryVersion` (or
    a `Restore`'s recorded state) that carries bytes or an unknown type/trust.
    `StorageFailure` for a row memriver could not have written: damage is reported
    as damage, never silently skipped or handed out.
    """
    texts = (type_, trust, description, body)
    if not all(isinstance(value, str) for value in texts):
        raise StorageFailure
    if type_ not in get_args(MemoryType) or trust not in get_args(Trust):
        raise StorageFailure
    if type(sync) is not int or sync not in (0, 1):
        raise StorageFailure
    if type(deleted) is not int or deleted not in (0, 1):
        raise StorageFailure
    return type_, trust, bool(sync), description, body, bool(deleted)


def _write_version(batch: _Batch, index: int, op_name: OpName, current: Memory,
                   before: _State, after: _State) -> Step:
    """The next version of `current` in state `after`: the row, the version and its sources."""
    if after == before:
        raise BatchConflict(index, current.id, "same-state")
    # every resulting state passes today's policy, a deleted one included (spec §0.2)
    # ponytail: every op's resulting text is scanned again here, while BEGIN IMMEDIATE
    # holds the write lock, on top of the wrapper's own scan just before the call
    # (application/memory.py's `_check_text`) -- one write pays for the policy check
    # twice. Upgrade path: scan before taking the lock, and inside re-check only the
    # text a concurrent writer could have changed since.
    rule = batch.check(after.description, after.body)
    if rule is not None:
        raise ContentRejected(rule_id=rule, memory_id=current.id)
    updated = now_strictly_after(current.updated)
    memory = replace(current, type=after.type, trust=after.trust, sync=after.sync,
                     description=after.description, body=after.body, updated=updated,
                     version=current.version + 1,
                     deleted_at=updated if after.deleted else None)
    # validate the row as it will stand, before writing it
    memory_from_row(memory_to_row(memory))
    batch.before.setdefault(current.id, (current.version, before.deleted))
    batch.conn.execute(
        "UPDATE memories SET type = ?, trust = ?, sync = ?, description = ?, body = ?, "
        "updated = ?, version = ?, deleted_at = ? WHERE id = ?",
        (memory.type, memory.trust, int(memory.sync), memory.description, memory.body,
         memory.updated, memory.version, memory.deleted_at, memory.id))
    _insert_version(batch, index, memory.id, memory.version, after)
    return Step(index + 1, memory.id, op_name, current.version, memory.version)


def _change(conn: sqlite3.Connection, change_id: str) -> Change | None:
    """A change with the steps still stored (a hard delete removes its members' steps)."""
    row = conn.execute("SELECT change_id, at, changed_by, changed_via, step_count, undoes "
                       "FROM changes WHERE change_id = ?", (change_id,)).fetchone()
    if row is None:
        return None
    steps = tuple(Step(*step) for step in conn.execute(
        "SELECT step, memory_id, op, before_version, after_version FROM change_steps "
        "WHERE change_id = ? ORDER BY step", (change_id,)))
    return Change(*row, steps)


def _plan(conn: sqlite3.Connection, memory_id: str) -> HardDeletePlan:
    """The target and every memory with a stored version citing a member, to a fixed point.

    Referrers only: a member's own sources are never followed.
    """
    if not _addressable(memory_id) or conn.execute(
            "SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is None:
        raise MemoryNotFound(memory_id)
    members, frontier = {memory_id}, [memory_id]
    citations: dict[str, list[PlanCitation]] = {}
    while frontier:
        cited_id = frontier.pop()
        for citing_id, citing_version, cited_version, citing_current in conn.execute(
                "SELECT s.memory_id, s.version, s.source_version, s.version = m.version "
                "FROM memory_sources s JOIN memories m ON m.id = s.memory_id "
                "WHERE s.source_id = ?", (cited_id,)).fetchall():
            citations.setdefault(citing_id, []).append(PlanCitation(
                citing_id, citing_version, cited_id, cited_version, bool(citing_current)))
            if citing_id not in members:
                members.add(citing_id)
                frontier.append(citing_id)
    referrers = members - {memory_id}
    # a referrer id memriver could not have written (undecodable bytes) cannot be
    # sorted against the addressable ones below; damage is reported as damage,
    # not a crash out of the plan
    if not all(_addressable(referrer) for referrer in referrers):
        raise StorageFailure
    ordered = [memory_id, *sorted(referrers)]
    rows = {row[0]: row[1:] for row in conn.execute(
        "SELECT id, project_id, version, deleted_at IS NOT NULL FROM memories "
        f"WHERE id IN ({', '.join('?' for _ in ordered)})", ordered)}
    items = tuple(HardDeleteItem(
        member, rows[member][0], rows[member][1], bool(rows[member][2]),
        tuple(sorted(citations.get(member, ()),
                     key=lambda c: (c.citing_version, c.cited_id, c.cited_version))))
        for member in ordered)
    pairs = "\n".join(sorted(f"{item.memory_id}:{item.version}" for item in items))
    return HardDeletePlan(memory_id, items, hashlib.sha256(pairs.encode("utf-8")).hexdigest()[:16])


def _insert_version(batch: _Batch, index: int, memory_id: str, version: int,
                    state: _State) -> None:
    batch.conn.execute(
        "INSERT INTO memory_versions (memory_id, version, type, trust, sync, description, "
        "body, deleted, change_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, version, state.type, state.trust, int(state.sync), state.description,
         state.body, int(state.deleted), batch.change_id))
    batch.conn.executemany(
        "INSERT INTO memory_sources (memory_id, version, source_id, source_version) "
        "VALUES (?, ?, ?, ?)",
        [(memory_id, version, ref.memory_id, ref.version) for ref in state.sources])
    if state.sources:
        batch.cited.append((index, memory_id, version))


class SqliteMemoryStore:
    def __init__(self, root: Path, *, busy_timeout_ms: int) -> None:
        self.root = Path(root)
        self._database = Database(self.root, busy_timeout_ms=busy_timeout_ms)

    def apply(self, ops: Sequence[Op], *, changed_by: str, changed_via: str | None,
              check: Check) -> Change:
        with self._database.write(create=False) as conn:
            return apply_ops(conn, ops, restriction=None, changed_by=changed_by,
                             changed_via=changed_via, check=check)

    def change(self, change_id: str) -> Change | None:
        with self._database.read() as conn:
            return None if conn is None else _change(conn, change_id)

    def undo(self, change_id: str, *, changed_by: str, changed_via: str | None,
             check: Check) -> Change:
        """The inverse of a change as one new change (spec §4.1), in one transaction."""
        with self._database.write(create=False) as conn:
            change = _change(conn, change_id)
            if change is None:
                raise UndoRefused("not-found")
            if len(change.steps) < change.step_count:
                raise UndoRefused("hard-deleted")
            ids = [step.memory_id for step in change.steps]
            current = dict(conn.execute(
                f"SELECT id, version FROM memories WHERE id IN ({', '.join('?' for _ in ids)})",
                ids).fetchall())
            # versions only: text changed back is still a change, a read is not
            moved = tuple(sorted(step.memory_id for step in change.steps
                                 if current.get(step.memory_id) != step.after_version))
            if moved:
                raise UndoRefused("changed", moved)
            inverse = [SoftDelete(step.memory_id, step.after_version) if step.op == "create"
                       else Restore(step.memory_id, step.after_version, step.before_version)
                       for step in change.steps]
            return apply_ops(conn, inverse, restriction=None, changed_by=changed_by,
                             changed_via=changed_via, check=check, undoes=change_id)

    def delete_global(self, op: SoftDelete, *, changed_by: str, changed_via: str | None,
                      check: Check) -> Change:
        """`op.memory_id` soft-deleted only if it is, right now, a live memory of the
        global project -- checked inside the same transaction as the delete, so a
        project's role cannot change between the check and the write. `MemoryNotFound`
        for anything else: absent, already deleted, or an ordinary project's memory.
        """
        if not _addressable(op.memory_id):
            raise MemoryNotFound(op.memory_id)
        if not self._database.exists():
            raise MemoryNotFound(op.memory_id)
        with self._database.write(create=False) as conn:
            row = conn.execute(
                "SELECT p.is_global FROM memories m JOIN projects p ON p.id = m.project_id "
                "WHERE m.id = ? AND m.deleted_at IS NULL", (op.memory_id,)).fetchone()
            if row is None or not row[0]:
                raise MemoryNotFound(op.memory_id)
            return apply_ops(conn, [op], restriction=None, changed_by=changed_by,
                             changed_via=changed_via, check=check)

    def scan(self, check: Callable[[str], str | None]) -> list[PolicyHit]:
        """Every stored version of every memory against `check`: one hit per version, no text."""
        with self._database.read() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT v.memory_id, v.version, v.description, v.body, m.version "
                "FROM memory_versions v JOIN memories m ON m.id = v.memory_id "
                "ORDER BY v.memory_id, v.version").fetchall()
        # checked after the read transaction: scanning must not hold the read lock
        hits: list[PolicyHit] = []
        for memory_id, version, description, body, current in rows:
            # an id no write path could ever have produced (undecodable bytes, or text
            # outside ID_RE) is not addressable by any command that would act on a
            # PolicyHit; the inspector already reports the row itself as invalid-row
            if not _addressable(memory_id):
                continue
            for text in (description, body):
                rule = check(text) if isinstance(text, str) else None
                if rule is not None:
                    hits.append(PolicyHit(memory_id, version, rule, version == current))
                    break
        return hits

    def plan_hard_delete(self, memory_id: str) -> HardDeletePlan:
        with self._database.read() as conn:
            if conn is None:
                raise MemoryNotFound(memory_id)
            return _plan(conn, memory_id)

    def hard_delete(self, memory_id: str, *, expected: frozenset[tuple[str, int]] | None,
                    code: str | None) -> list[str]:
        """Recompute the plan in the write transaction; delete every member only if it matches."""
        if not self._database.exists():
            raise MemoryNotFound(memory_id)
        with self._database.write(create=False) as conn:
            plan = _plan(conn, memory_id)
            if (expected is not None and plan.expected != frozenset(expected)) \
                    or (code is not None and plan.code != code):
                raise PlanChanged(plan)
            members = [item.memory_id for item in plan.items]
            # the cascade takes versions, source rows, reads and steps; change rows stay
            conn.execute(f"DELETE FROM memories WHERE id IN ({', '.join('?' for _ in members)})",
                         members)
            return members

    def write(self, op: Op, *, restriction: ReadWriteSet, changed_by: str,
              changed_via: str | None, check: Check) -> Memory:
        """One agent op; the resulting memory, read inside the write transaction.

        An unchanged result writes nothing and returns the memory as checked.
        """
        # Restore and an explicit source set are management-only: admitting a
        # newly cited source (`_pre_batch`) looks a memory up by id alone, with
        # no `restriction` check, so an agent op that reached it could probe
        # another project's memories through "source"/"cycle" conflicts
        if isinstance(op, Restore):
            raise ValueError("restore is not an agent operation")  # noqa: TRY004
        # an agent never sets sources: a Create cites none, an Update keeps them
        if (isinstance(op, Create) and op.sources != ()) or \
                (isinstance(op, Update) and op.sources is not None):
            raise ValueError("citing sources is not an agent operation")
        # a malformed target id answers exactly like a missing one, never as
        # storage damage: SQLite never sees it
        if not isinstance(op, Create) and not _addressable(op.memory_id):
            raise MemoryNotFound(op.memory_id)
        # as on every agent write: a removed store is never recreated, and it
        # answers as the missing project or memory it hides
        if not self._database.exists():
            if isinstance(op, Create):
                raise ProjectUnavailable()
            raise MemoryNotFound(op.memory_id)
        with self._database.write(create=False) as conn:
            # the change row is written first, so a checked no-op rolls back to here
            conn.execute("SAVEPOINT agent_write")
            try:
                memory_id = apply_ops(conn, [op], restriction=restriction,
                                      changed_by=changed_by, changed_via=changed_via,
                                      check=check).steps[0].memory_id
            except BatchConflict as err:
                # on this path only an unchanged result, after every other check passed
                if err.reason != "same-state":
                    raise
                conn.execute("ROLLBACK TO agent_write")
                memory_id = op.memory_id
            conn.execute("RELEASE agent_write")
            memory = _joined(conn, memory_id, include_deleted=True, readable=None)
        if memory is None:              # cannot happen: the row was written or checked just now
            raise StorageFailure
        return memory

    def read(self, memory_id: str, read_write_set: ReadWriteSet) -> Memory:
        if not _addressable(memory_id):
            raise MemoryNotFound(memory_id)
        with self._database.read() as conn:
            memory = _joined(conn, memory_id, include_deleted=False,
                             readable=read_write_set.readable())
        if memory is None:
            raise MemoryNotFound(memory_id)
        return memory

    def touch_read(self, memory_id: str, at: str, *, memory_version: int, harness: str,
                   session_id: str | None) -> None:
        if not _addressable(memory_id) or not is_timestamp(at):
            return
        try:
            with self._database.write(create=False) as conn:
                conn.execute(
                    "UPDATE memories SET last_read_at = max(coalesce(last_read_at, ''), ?) "
                    "WHERE id = ? AND deleted_at IS NULL", (at, memory_id))
                # the high-water mark above must land even when the read fact below
                # cannot (an oversized harness or session id fails its CHECK): its own
                # savepoint, so only the insert -- never last_read_at -- rolls back
                conn.execute("SAVEPOINT touch_read")
                try:
                    # the version the reader was handed, not the row's current one:
                    # another writer may have moved the row since the read
                    conn.execute(
                        "INSERT INTO memory_reads (memory_id, memory_version, read_at, harness, "
                        "session_id) SELECT id, ?, ?, ?, ? FROM memories "
                        "WHERE id = ? AND deleted_at IS NULL",
                        (memory_version, at, harness, session_id, memory_id))
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK TO touch_read")
                conn.execute("RELEASE touch_read")
        except StorageFailure:
            pass   # best effort (spec §3.3): a missing store, or any failure, never fails the read

    def read_any(self, memory_id: str, *, include_deleted: bool) -> Memory:
        if not _addressable(memory_id):
            raise MemoryNotFound(memory_id)
        with self._database.read() as conn:
            memory = _joined(conn, memory_id, include_deleted=include_deleted, readable=None)
        if memory is None:
            raise MemoryNotFound(memory_id)
        return memory

    def prune_reads(self, retention_days: int) -> int:
        """Delete read facts older than `retention_days`; how many went."""
        try:
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        except OverflowError:
            return 0            # further back than any representable time: nothing is older
        # isoformat, not strftime: %Y is not zero-padded below year 1000 on every
        # platform, and read_at is compared as fixed-width text
        cutoff_text = cutoff.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"
        if not self._database.exists():
            return 0
        with self._database.write(create=False) as conn:
            return conn.execute("DELETE FROM memory_reads WHERE read_at < ?",
                                (cutoff_text,)).rowcount

    def usage(self, memory_ids: Sequence[str]) -> dict[str, Usage]:
        ids = sorted(set(memory_ids))
        if not ids:
            return {}
        with self._database.read() as conn:
            if conn is None:
                return {}
            rows = conn.execute(
                "SELECT m.id, m.last_read_at, "
                "(SELECT count(*) FROM memory_reads r WHERE r.memory_id = m.id) "
                f"FROM memories m WHERE m.id IN ({', '.join('?' for _ in ids)})",
                ids).fetchall()
        return {memory_id: Usage(reads, last_read_at) for memory_id, last_read_at, reads in rows}

    def memories(self, project_id: str | None = None, *,
                 include_deleted: bool = False) -> list[Memory]:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("m.project_id = ?")
            params.append(project_id)
        if not include_deleted:
            clauses.append("m.deleted_at IS NULL")
        query = _SELECT_ALL + (" WHERE " + " AND ".join(clauses) if clauses else "")
        found: list[Memory] = []
        with self._database.read() as conn:
            if conn is None:
                return []
            for row in conn.execute(query + " ORDER BY m.updated DESC, m.id DESC", params):
                try:
                    found.append(memory_from_row(row))
                except ValueError:
                    continue                # a bad row is a doctor finding, skipped here
        return found

    def versions(self, memory_id: str) -> list[MemoryVersion]:
        if not _addressable(memory_id):
            raise MemoryNotFound(memory_id)
        with self._database.read() as conn:
            if conn is None or conn.execute("SELECT 1 FROM memories WHERE id = ?",
                                            (memory_id,)).fetchone() is None:
                raise MemoryNotFound(memory_id)
            rows = conn.execute(
                "SELECT v.version, v.type, v.trust, v.sync, v.description, v.body, v.deleted, "
                "c.change_id, c.at, c.changed_by, c.changed_via, c.step_count, c.undoes "
                "FROM memory_versions v LEFT JOIN changes c ON c.change_id = v.change_id "
                "WHERE v.memory_id = ? ORDER BY v.version", (memory_id,)).fetchall()
            found = []
            for (version, type_, trust, sync, description, body, deleted, change_id,
                 at, changed_by, changed_via, step_count, undoes) in rows:
                type_, trust, sync, description, body, deleted = _decode_history_state(
                    type_, trust, sync, description, body, deleted)
                change = None if change_id is None else Change(
                    change_id, at, changed_by, changed_via, step_count, undoes, ())
                found.append(MemoryVersion(memory_id, version, type_, trust, sync, description,
                                           body, deleted, _sources_of(conn, memory_id, version),
                                           change))
            return found

    def citing(self, memory_id: str) -> list[Citation]:
        with self._database.read() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT s.memory_id, s.version, s.source_version, s.version = m.version "
                "FROM memory_sources s JOIN memories m ON m.id = s.memory_id "
                "WHERE s.source_id = ? ORDER BY s.memory_id, s.version",
                (memory_id,)).fetchall()
        return [Citation(citing_id, version, source_version, bool(current))
                for citing_id, version, source_version, current in rows]
