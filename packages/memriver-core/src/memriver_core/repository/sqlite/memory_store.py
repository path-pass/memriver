"""`MemoryStore` over the SQLite database: current state, permanent history, the change log.

A `memories` row is a memory's current state; every state it ever had is a
`memory_versions` row with its complete `memory_sources` set; every write is
one `changes` row with one `change_steps` row per memory it touched. All of
it is written by `apply_ops`, inside one write transaction, so a refused
change leaves nothing behind.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from memriver_core.models import (
    ID_RE,
    Memory,
    ReadWriteSet,
    is_timestamp,
    new_id,
    now,
    now_strictly_after,
)
from memriver_core.models.changes import (
    Change,
    Create,
    Op,
    OpName,
    SoftDelete,
    SourceRef,
    Step,
    Update,
)
from memriver_core.models.errors import (
    BatchConflict,
    ContentRejected,
    GlobalReadOnly,
    IdCollision,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
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
    raise TypeError(f"{type(op).__name__} is not applied by this store")


def _create(batch: _Batch, index: int, op: Create, *, changed_by: str,
            changed_via: str | None) -> Step:
    _authorize_create(batch, op.project_id)
    sources = _admit(batch, index, None, (), op.sources)
    memory = Memory.new(body=op.body, type=op.type, project_id=op.project_id,
                        source={"harness": changed_via or "unknown", "method": changed_by},
                        trust=op.trust, sync=op.sync, description=op.description)
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
    _insert_version(batch, memory.id, 1, _State(memory.type, memory.trust, memory.sync,
                                                memory.description, memory.body, False,
                                                sources))
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
    after = replace(
        before,
        description=before.description if op.description is None else op.description.strip(),
        body=before.body if op.body is None else op.body.strip(),
        sources=_admit(batch, index, current.id, before.sources, op.sources))
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


def _target(batch: _Batch, index: int, op: Update | SoftDelete, *,
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
    """The resulting source set: the current one, carried as it is."""
    if requested is None or tuple(requested) == current:
        return current
    raise ValueError("citing sources is not supported by this store")


def _state_of(conn: sqlite3.Connection, memory: Memory) -> _State:
    return _State(memory.type, memory.trust, memory.sync, memory.description, memory.body,
                  memory.deleted_at is not None, _sources_of(conn, memory.id, memory.version))


def _sources_of(conn: sqlite3.Connection, memory_id: str,
                version: int) -> tuple[SourceRef, ...]:
    return tuple(SourceRef(source_id, source_version)
                 for source_id, source_version in conn.execute(
                     "SELECT source_id, source_version FROM memory_sources "
                     "WHERE memory_id = ? AND version = ? ORDER BY source_id",
                     (memory_id, version)))


def _write_version(batch: _Batch, index: int, op_name: OpName, current: Memory,
                   before: _State, after: _State) -> Step:
    """The next version of `current` in state `after`: the row, the version and its sources."""
    if after == before:
        raise BatchConflict(index, current.id, "same-state")
    # every resulting state passes today's policy, a deleted one included (spec §0.2)
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
    batch.conn.execute(
        "UPDATE memories SET type = ?, trust = ?, sync = ?, description = ?, body = ?, "
        "updated = ?, version = ?, deleted_at = ? WHERE id = ?",
        (memory.type, memory.trust, int(memory.sync), memory.description, memory.body,
         memory.updated, memory.version, memory.deleted_at, memory.id))
    _insert_version(batch, memory.id, memory.version, after)
    return Step(index + 1, memory.id, op_name, current.version, memory.version)


def _insert_version(batch: _Batch, memory_id: str, version: int, state: _State) -> None:
    batch.conn.execute(
        "INSERT INTO memory_versions (memory_id, version, type, trust, sync, description, "
        "body, deleted, change_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, version, state.type, state.trust, int(state.sync), state.description,
         state.body, int(state.deleted), batch.change_id))
    batch.conn.executemany(
        "INSERT INTO memory_sources (memory_id, version, source_id, source_version) "
        "VALUES (?, ?, ?, ?)",
        [(memory_id, version, ref.memory_id, ref.version) for ref in state.sources])


class SqliteMemoryStore:
    def __init__(self, root: Path, *, busy_timeout_ms: int) -> None:
        self.root = Path(root)
        self._database = Database(self.root, busy_timeout_ms=busy_timeout_ms)

    def apply(self, ops: Sequence[Op], *, changed_by: str, changed_via: str | None,
              check: Check) -> Change:
        with self._database.write(create=False) as conn:
            return apply_ops(conn, ops, restriction=None, changed_by=changed_by,
                             changed_via=changed_via, check=check)

    def write(self, op: Op, *, restriction: ReadWriteSet, changed_by: str,
              changed_via: str | None, check: Check) -> Memory:
        """One agent op; the resulting memory, read inside the write transaction.

        An unchanged result writes nothing and returns the memory as checked.
        """
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

    def touch_read(self, memory_id: str, at: str) -> None:
        if not _addressable(memory_id) or not is_timestamp(at):
            return
        try:
            with self._database.write(create=False) as conn:
                conn.execute(
                    "UPDATE memories SET last_read_at = max(coalesce(last_read_at, ''), ?) "
                    "WHERE id = ? AND deleted_at IS NULL", (at, memory_id))
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
