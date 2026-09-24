"""`MemoryStore` over the SQLite database: one row per memory, body included."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from memriver_core.models import (
    ID_RE,
    Memory,
    ReadWriteSet,
    is_timestamp,
    now,
    now_strictly_after,
)
from memriver_core.models.errors import (
    GlobalReadOnly,
    IdCollision,
    MemoryNotFound,
    ProjectUnavailable,
    StorageFailure,
    VersionConflict,
)

from .database import MEMORY_COLUMNS, Database, memory_from_row, memory_to_row

_NO_WRITABLE_PROJECT = "no writable project in this session"

# joined to its project, so an orphan (a writer ignored foreign keys) answers
# like any other hidden id
_SELECT_JOINED = (
    "SELECT " + ", ".join(f"m.{column.strip()}" for column in MEMORY_COLUMNS.split(","))
    + " FROM memories m JOIN projects p ON p.id = m.project_id WHERE m.id = ?"
)
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


class SqliteMemoryStore:
    def __init__(self, root: Path, *, busy_timeout_ms: int) -> None:
        self.root = Path(root)
        self._database = Database(self.root, busy_timeout_ms=busy_timeout_ms)

    def record(self, memory: Memory, read_write_set: ReadWriteSet) -> None:
        if memory.project_id == read_write_set.global_project_id:
            raise GlobalReadOnly()
        if memory.project_id not in read_write_set.writable():
            raise ProjectUnavailable(_NO_WRITABLE_PROJECT)
        if not _addressable(memory.id):
            raise ValueError("invalid memory id")
        # like update/delete: a removed store is never recreated by a write
        if not self._database.exists():
            raise ProjectUnavailable(_NO_WRITABLE_PROJECT)
        with self._database.write() as conn:
            row = conn.execute("SELECT is_global FROM projects WHERE id = ?",
                               (memory.project_id,)).fetchone()
            if row is None:
                raise ProjectUnavailable(_NO_WRITABLE_PROJECT)
            # the cached global_project_id above is only an early refusal: the
            # database can be replaced or restored under a running server, so
            # the role that decides is the one on the row, read just now
            if row[0]:
                raise GlobalReadOnly()
            # deleted rows keep their id: an id is never reused
            if conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory.id,)).fetchone():
                raise IdCollision(memory.id)
            memory_row = memory_to_row(memory)
            # a row the read path would reject is never committed: the same
            # decoder that would refuse it on the next read refuses it now
            memory_from_row(memory_row)
            conn.execute(f"INSERT INTO memories ({MEMORY_COLUMNS}) VALUES ({_PLACEHOLDERS})",
                         memory_row)

    def read(self, memory_id: str, read_write_set: ReadWriteSet) -> Memory:
        if not _addressable(memory_id):
            raise MemoryNotFound(memory_id)
        with self._database.read() as conn:
            memory = _joined(conn, memory_id, include_deleted=False,
                             readable=read_write_set.readable())
        if memory is None:
            raise MemoryNotFound(memory_id)
        return memory

    def update(self, memory_id: str, read_write_set: ReadWriteSet, *, expected_version: int,
               body: str, description: str | None) -> Memory:
        if not _addressable(memory_id) or not self._database.exists():
            raise MemoryNotFound(memory_id)
        with self._database.write() as conn:
            memory = self._writable(conn, memory_id, read_write_set, allow_deleted=False)
            if memory.version != expected_version:
                raise VersionConflict(memory_id)
            memory.body = body.strip()
            if description is not None:
                # None keeps the existing description; "" clears it
                memory.description = description.strip()
            memory.updated = now_strictly_after(memory.updated)
            memory.version = expected_version + 1
            # validate the row as it will stand after this update, before writing it
            memory_from_row(memory_to_row(memory))
            cursor = conn.execute(
                "UPDATE memories SET body = ?, description = ?, updated = ?, version = ? "
                "WHERE id = ? AND version = ? AND deleted_at IS NULL",
                (memory.body, memory.description, memory.updated, memory.version,
                 memory_id, expected_version))
            if cursor.rowcount != 1:
                raise VersionConflict(memory_id)
        return memory

    def delete(self, memory_id: str, read_write_set: ReadWriteSet, *, expected_version: int,
               hard: bool) -> int:
        if not _addressable(memory_id) or not self._database.exists():
            raise MemoryNotFound(memory_id)
        with self._database.write() as conn:
            memory = self._writable(conn, memory_id, read_write_set, allow_deleted=hard)
            if memory.version != expected_version:
                raise VersionConflict(memory_id)
            if hard:
                conn.execute("DELETE FROM memories WHERE id = ? AND version = ?",
                             (memory_id, expected_version))
                return 0
            conn.execute("UPDATE memories SET deleted_at = ?, version = version + 1 "
                         "WHERE id = ? AND version = ? AND deleted_at IS NULL",
                         (now(), memory_id, expected_version))
            return expected_version + 1

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

    @staticmethod
    def _writable(conn: sqlite3.Connection, memory_id: str, read_write_set: ReadWriteSet, *,
                  allow_deleted: bool) -> Memory:
        """Locate and authorize inside the write transaction."""
        memory = _joined(conn, memory_id, include_deleted=allow_deleted,
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
