"""`MaintenanceStore` over the SQLite database: what the maintenance run reads and writes."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from memriver_core.models import (
    ID_RE,
    Candidate,
    Change,
    ChangeGroup,
    ChangeRow,
    CreateOp,
    DreamRun,
    Memory,
    Review,
    SourceRef,
    UndoResult,
    UpdateOp,
    effective_ttl_days,
    now_strictly_after,
    timestamp_shift,
)
from memriver_core.models.errors import (
    GroupConflict,
    IdCollision,
    StorageFailure,
    UndoConflict,
)

from .database import (
    CHANGE_COLUMNS,
    MEMORY_COLUMNS,
    REVIEW_COLUMNS,
    RUN_COLUMNS,
    SOURCE_COLUMNS,
    STATE_COLUMNS,
    Database,
    change_from_row,
    change_to_row,
    dumps_json,
    memory_from_object,
    memory_from_row,
    memory_object,
    memory_to_row,
    review_from_row,
    review_to_row,
    run_from_row,
    run_to_row,
    source_from_row,
    state_row_check,
)

_M = ", ".join(f"m.{column.strip()}" for column in MEMORY_COLUMNS.split(","))
_R = ", ".join(f"r.{column.strip()}" for column in REVIEW_COLUMNS.split(","))
_WIDTH = len(MEMORY_COLUMNS.split(","))
# joined to its project, so an orphan never reaches the run
_ACTIVE = (f"SELECT {_M} FROM memories m JOIN projects p ON p.id = m.project_id "
           "WHERE m.deleted_at IS NULL")
_OLDEST_FIRST = " ORDER BY m.created, m.id"
_S = ", ".join(f"s.{column.strip()}" for column in SOURCE_COLUMNS.split(","))
# the source set in force for derived row `d` at its current version (spec §3.2)
_EFFECTIVE = ("s.derived_version = (SELECT max(t.derived_version) FROM memory_source_sets t "
              "WHERE t.derived_id = d.id AND t.derived_version <= d.version)")


def _decoded[T](rows: Iterable[tuple], decode: Callable[[tuple], T]) -> list[T]:
    """Every row that decodes; one memriver could not have written is skipped."""
    found: list[T] = []
    for row in rows:
        try:
            found.append(decode(row))
        except ValueError:
            continue                        # a doctor finding, never a failed run
    return found


def _last_use(memory: Memory) -> str:
    return max(memory.created, memory.updated, memory.last_read_at or "")


_PLACEHOLDERS = ", ".join("?" for _ in MEMORY_COLUMNS.split(","))
_TRUST_RANK = {"user": 0, "agent": 1, "untrusted-derived": 2}
_GROUP_KINDS = frozenset({"merge", "rewrite", "extract", "unsafe"})


def _row(conn: sqlite3.Connection, memory_id: str, *, include_deleted: bool) -> Memory | None:
    """The row decoded inside the caller's transaction; damage is StorageFailure."""
    query = f"SELECT {_M} FROM memories m JOIN projects p ON p.id = m.project_id WHERE m.id = ?"
    if not include_deleted:
        query += " AND m.deleted_at IS NULL"
    row = conn.execute(query, (memory_id,)).fetchone()
    if row is None:
        return None
    try:
        return memory_from_row(row)
    except ValueError as err:
        raise StorageFailure from err


def _effective_set(conn: sqlite3.Connection, memory: Memory) -> list[SourceRef]:
    """The source set in force for `memory` at its current version (spec §3.2)."""
    rows = conn.execute(f"SELECT {_S} FROM memory_sources s JOIN memories d "
                        f"ON d.id = s.derived_id WHERE d.id = ? AND {_EFFECTIVE} "
                        "ORDER BY s.source_id", (memory.id,)).fetchall()
    try:
        return [source_from_row(row)[2] for row in rows]
    except ValueError as err:
        raise StorageFailure from err


def _reaches(conn: sqlite3.Connection, start: str, target: str) -> bool:
    """Whether `start` is `target` or cites it, directly or through other entries, in any
    version -- the RESTRICT key covers every version, so history counts."""
    return conn.execute(
        "WITH RECURSIVE cited(id) AS (SELECT ? UNION "
        "SELECT s.source_id FROM memory_sources s JOIN cited c ON s.derived_id = c.id) "
        "SELECT 1 FROM cited WHERE id = ? LIMIT 1", (start, target)).fetchone() is not None


def _least_trusted(memories: list[Memory]) -> str:
    return max((memory.trust for memory in memories), key=_TRUST_RANK.__getitem__)


def _ref(memory: Memory) -> SourceRef:
    """A source as it stands now, sealed: its version and a snapshot of its content."""
    return SourceRef(memory.id, memory.version, memory.project_id,
                     {"type": memory.type, "description": memory.description,
                      "body": memory.body})


def _consumed(conn: sqlite3.Connection, group: ChangeGroup, pairs: tuple[tuple[str, int], ...],
              *, target_id: str | None = None) -> list[Memory]:
    """The newly consumed sources as they stand: each active, at its named version, in
    scope, and -- for an update -- not citing the target (no cycle)."""
    ids = [source_id for source_id, _ in pairs]
    if len(set(ids)) != len(ids):
        raise GroupConflict(None, tuple(ids))
    found: list[Memory] = []
    for source_id, version in pairs:
        memory = _row(conn, source_id, include_deleted=False)
        if memory is None or memory.version != version or (
                group.kind != "extract" and memory.project_id != group.project_id) or (
                target_id is not None and _reaches(conn, source_id, target_id)):
            raise GroupConflict(None, (source_id,))
        found.append(memory)
    return found


def _already_cited(conn: sqlite3.Connection, project_id: str,
                   pairs: tuple[tuple[str, int], ...], *, exclude: str = "") -> tuple[str, ...]:
    """Sources an active entry of `project_id` other than `exclude` already has in its
    effective set at that version: the guard that keeps a re-planned group from being
    applied twice and one source version from feeding two global entries."""
    return tuple(source_id for source_id, version in pairs if conn.execute(
        "SELECT 1 FROM memory_sources s JOIN memories d ON d.id = s.derived_id "
        "WHERE s.source_id = ? AND s.source_version = ? AND d.project_id = ? "
        f"AND d.id != ? AND d.deleted_at IS NULL AND {_EFFECTIVE}",
        (source_id, version, project_id, exclude)).fetchone())


def _write_set(conn: sqlite3.Connection, derived_id: str, derived_version: int,
               refs: list[SourceRef]) -> None:
    """Record `refs` as the source set of that version; no refs: an explicitly empty set."""
    conn.execute("INSERT INTO memory_source_sets (derived_id, derived_version) VALUES (?, ?)",
                 (derived_id, derived_version))
    for ref in refs:
        conn.execute(f"INSERT INTO memory_sources ({SOURCE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                     (derived_id, derived_version, ref.source_id, ref.source_version,
                      ref.source_project, dumps_json(ref.snapshot)))


def _target(conn: sqlite3.Connection, group: ChangeGroup, memory_id: str,
            expected_version: int) -> Memory:
    memory = _row(conn, memory_id, include_deleted=False)
    if memory is None or memory.version != expected_version \
            or memory.project_id != group.project_id:
        raise GroupConflict(None, (memory_id,))
    return memory


def _create(conn: sqlite3.Connection, group: ChangeGroup, op: CreateOp, memory_id: str,
            now: str) -> ChangeRow:
    if op.project_id != group.project_id:
        raise GroupConflict(None, (op.project_id,))
    sources = _consumed(conn, group, op.sources)
    cited = _already_cited(conn, op.project_id, op.sources)
    if cited:
        raise GroupConflict(None, cited)
    if conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone():
        raise IdCollision(memory_id)
    memory = Memory(id=memory_id, project_id=op.project_id, type=op.type,
                    source={"harness": group.harness, "method": "dream"},
                    trust=_least_trusted(sources), sync=all(s.sync for s in sources),
                    created=now, updated=now, description=op.description.strip(),
                    body=op.body.strip())
    row = memory_to_row(memory)
    memory_from_row(row)                    # a row the read path would reject is never written
    conn.execute(f"INSERT INTO memories ({MEMORY_COLUMNS}) VALUES ({_PLACEHOLDERS})", row)
    _write_set(conn, memory.id, 1, [_ref(source) for source in sources])
    return ChangeRow(memory.id, None, None, 1)


def _update(conn: sqlite3.Connection, group: ChangeGroup, op: UpdateOp,
            now: str) -> ChangeRow:
    memory = _target(conn, group, op.id, op.expected_version)
    sources = _consumed(conn, group, op.sources, target_id=op.id)
    if group.kind == "extract":
        # new consumption only: the target's own carried sources are not re-checked
        cited = _already_cited(conn, memory.project_id, op.sources, exclude=memory.id)
        if cited:
            raise GroupConflict(None, cited)
    carried = _effective_set(conn, memory)
    # the set grows, never shrinks by omission: carried rows stay sealed at their own
    # version and snapshot; a newly consumed source replaces a carried row of its id
    new_ids = {source.id for source in sources}
    refs = [ref for ref in carried if ref.source_id not in new_ids] + [_ref(s) for s in sources]
    # never above its own row: dream does not raise trust or turn a sync on
    updated = dataclasses.replace(
        memory, description=op.description.strip(), body=op.body.strip(),
        source={"harness": group.harness, "method": "dream"},
        updated=now_strictly_after(memory.updated), version=memory.version + 1,
        trust=_least_trusted([memory, *sources]),
        sync=memory.sync and all(s.sync for s in sources))
    memory_from_row(memory_to_row(updated))
    conn.execute("UPDATE memories SET description = ?, body = ?, source_harness = ?, "
                 "source_method = ?, trust = ?, sync = ?, updated = ?, version = ? "
                 "WHERE id = ? AND version = ? AND deleted_at IS NULL",
                 (updated.description, updated.body, updated.source["harness"],
                  updated.source["method"], updated.trust, int(updated.sync), updated.updated,
                  updated.version, memory.id, memory.version))
    _write_set(conn, updated.id, updated.version, sorted(refs, key=lambda ref: ref.source_id))
    return ChangeRow(memory.id, memory_object(memory), tuple(carried), updated.version)


def _soft_delete_row(conn: sqlite3.Connection, memory: Memory, now: str) -> ChangeRow:
    before_sources = tuple(_effective_set(conn, memory))
    conn.execute("UPDATE memories SET deleted_at = ?, version = version + 1 "
                 "WHERE id = ? AND version = ? AND deleted_at IS NULL",
                 (now, memory.id, memory.version))
    return ChangeRow(memory.id, memory_object(memory), before_sources, memory.version + 1)


def _log(conn: sqlite3.Connection, change: Change) -> None:
    row = change_to_row(change)
    change_from_row(row)                    # the change log only holds rows it can read back
    conn.execute(f"INSERT INTO dream_changes ({CHANGE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?)", row)


_UPSERT_REVIEW = (
    f"INSERT INTO dream_reviews ({REVIEW_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(memory_id) DO UPDATE SET "
    + ", ".join(f"{column.strip()} = excluded.{column.strip()}"
                for column in REVIEW_COLUMNS.split(",")[1:]))


def _save_review(conn: sqlite3.Connection, review: Review) -> None:
    row = review_to_row(review)
    review_from_row(row)                    # only a review the read path accepts is kept
    conn.execute(_UPSERT_REVIEW, row)


def _restore(conn: sqlite3.Connection, memory: Memory, change_row: ChangeRow, now: str) -> None:
    """One row back to its before-image and before source set, its version moving on."""
    version = memory.version + 1
    if change_row.before is None:           # a row the group created: soft-deleted
        conn.execute("UPDATE memories SET deleted_at = ?, version = ? WHERE id = ? "
                     "AND version = ?", (now, version, memory.id, memory.version))
        return
    image = memory_from_object(change_row.before)
    conn.execute("UPDATE memories SET description = ?, body = ?, source_harness = ?, "
                 "source_method = ?, trust = ?, sync = ?, deleted_at = ?, updated = ?, "
                 "version = ? WHERE id = ? AND version = ?",
                 (image.description, image.body, image.source["harness"],
                  image.source["method"], image.trust, int(image.sync), image.deleted_at,
                  now_strictly_after(memory.updated), version, memory.id, memory.version))
    # the provenance in force before comes back with the content -- an empty set too.
    # It cannot close a cycle: every edge it restores was checked, in every version,
    # when it was first written, and later writes are checked against it
    _write_set(conn, memory.id, version, list(change_row.before_sources or ()))


class SqliteMaintenanceStore:
    def __init__(self, root: Path, *, busy_timeout_ms: int) -> None:
        self.root = Path(root)
        self._database = Database(self.root, busy_timeout_ms=busy_timeout_ms)

    def _rows(self, query: str, params: tuple = ()) -> list[tuple]:
        with self._database.read() as conn:
            return [] if conn is None else conn.execute(query, params).fetchall()

    def memories(self, project_id: str) -> list[Memory]:
        return _decoded(self._rows(_ACTIVE + " AND m.project_id = ?" + _OLDEST_FIRST,
                                   (project_id,)), memory_from_row)

    def active_memories(self) -> list[Memory]:
        return _decoded(self._rows(_ACTIVE + _OLDEST_FIRST), memory_from_row)

    def sources_of(self, memory_id: str) -> list[SourceRef]:
        rows = self._rows(
            f"SELECT {_S} FROM memory_sources s JOIN memories d ON d.id = s.derived_id "
            f"WHERE d.id = ? AND {_EFFECTIVE} ORDER BY s.source_id", (memory_id,))
        return [ref for _, _, ref in _decoded(rows, source_from_row)]

    def derived_from(self, memory_id: str) -> list[str]:
        rows = self._rows(
            "SELECT DISTINCT d.id FROM memory_sources s JOIN memories d ON d.id = s.derived_id "
            f"WHERE s.source_id = ? AND d.deleted_at IS NULL AND {_EFFECTIVE} ORDER BY d.id",
            (memory_id,))
        # a lenient text_factory hands back raw bytes for an id that is not valid
        # UTF-8; that, or any id the store could never have written, is skipped
        # like any other row that fails validation, never echoed to a caller
        return [row[0] for row in rows if isinstance(row[0], str) and ID_RE.fullmatch(row[0])]

    def ttl_candidates(self, *, now: str, ttl_days: int, multiplier_max: int,
                       limit: int) -> list[Candidate]:
        # ponytail: every uncovered active row is read and its cutoff computed
        # here rather than in SQL; fine at hundreds of memories -- move the
        # comparison into SQL (julianday) if stores grow far past that
        rows = self._rows(
            f"SELECT {_M}, (SELECT count(*) FROM memory_reads x WHERE x.memory_id = m.id), "
            f"{_R} FROM memories m JOIN projects p ON p.id = m.project_id "
            "LEFT JOIN dream_reviews r ON r.memory_id = m.id "
            "WHERE m.deleted_at IS NULL AND (r.memory_id IS NULL OR r.next_review_at <= ?)",
            (now,))
        due: list[tuple[str, str, Candidate]] = []
        for row in rows:
            try:
                memory = memory_from_row(row[:_WIDTH])
                review = None if row[_WIDTH + 1] is None else review_from_row(row[_WIDTH + 1:])
            except ValueError:
                continue
            reads = row[_WIDTH]
            cutoff = timestamp_shift(now, days=-effective_ttl_days(ttl_days, reads,
                                                                   multiplier_max))
            if _last_use(memory) <= cutoff:
                due.append((_last_use(memory), memory.id, Candidate(memory, review, reads)))
        due.sort(key=lambda item: item[:2])
        return [candidate for _, _, candidate in due[:limit]]

    def fingerprint_of(self, scope: str) -> str | None:
        rows = self._rows(f"SELECT {STATE_COLUMNS} FROM dream_state WHERE scope = ?", (scope,))
        if not rows:
            return None
        try:
            state_row_check(rows[0])
        except ValueError:
            return None                     # a doctor finding, never a failed run
        return rows[0][1]

    def changes(self, limit: int) -> list[Change]:
        return _decoded(self._rows(f"SELECT {CHANGE_COLUMNS} FROM dream_changes "
                                   "ORDER BY applied_at DESC, change_id LIMIT ?", (limit,)),
                        change_from_row)

    def apply_group(self, group: ChangeGroup, *, change_id: str,
                    created_ids: tuple[str, ...], now: str) -> None:
        if group.kind not in _GROUP_KINDS:
            raise ValueError("retire and secret changes have writes of their own")
        with self._database.write(create=False) as conn:
            if conn.execute("SELECT 1 FROM dream_changes WHERE change_id = ?",
                            (change_id,)).fetchone():
                raise IdCollision(change_id)
            project = conn.execute("SELECT is_global FROM projects WHERE id = ?",
                                   (group.project_id,)).fetchone()
            if project is None or (group.kind == "extract" and not project[0]):
                raise GroupConflict(None, (group.project_id,))
            created = iter(created_ids)
            rows: list[ChangeRow] = []
            for op in group.ops:
                if isinstance(op, CreateOp):
                    rows.append(_create(conn, group, op, next(created), now))
                elif isinstance(op, UpdateOp):
                    rows.append(_update(conn, group, op, now))
                else:
                    memory = _target(conn, group, op.id, op.expected_version)
                    rows.append(_soft_delete_row(conn, memory, now))
            _log(conn, Change(change_id=change_id, run_id=group.run_id, kind=group.kind,
                              project_id=group.project_id, applied_at=now, rows=tuple(rows),
                              reason=group.reason, undone_at=None))

    def set_fingerprint(self, scope: str, fingerprint: str, now: str) -> None:
        state_row_check((scope, fingerprint, now))
        with self._database.write(create=False) as conn:
            conn.execute("INSERT INTO dream_state (scope, fingerprint, processed_at) "
                         "VALUES (?, ?, ?) ON CONFLICT(scope) DO UPDATE SET "
                         "fingerprint = excluded.fingerprint, "
                         "processed_at = excluded.processed_at", (scope, fingerprint, now))

    def change(self, change_id: str) -> Change | None:
        found = _decoded(self._rows(f"SELECT {CHANGE_COLUMNS} FROM dream_changes "
                                    "WHERE change_id = ?", (change_id,)), change_from_row)
        return found[0] if found else None

    def undo(self, change_id: str, *, now: str) -> UndoResult:
        with self._database.write(create=False) as conn:
            row = conn.execute(f"SELECT {CHANGE_COLUMNS} FROM dream_changes "
                               "WHERE change_id = ?", (change_id,)).fetchone()
            if row is None:
                return UndoResult(change_id, "not-found", ())
            try:
                change = change_from_row(row)
            except ValueError as err:
                raise StorageFailure from err
            if change.undone_at is not None:
                return UndoResult(change_id, "already-undone", ())
            current = {r.id: _row(conn, r.id, include_deleted=True) for r in change.rows}
            moved = tuple(r.id for r in change.rows
                          if current[r.id] is None or current[r.id].version != r.after_version)
            if moved:
                raise UndoConflict(change_id, moved)
            for change_row in change.rows:
                _restore(conn, current[change_row.id], change_row, now)
            conn.execute("UPDATE dream_changes SET undone_at = ? WHERE change_id = ?",
                         (now, change_id))
        return UndoResult(change_id, "undone", tuple(r.id for r in change.rows))

    def retire(self, memory_id: str, *, judged_version: int, ttl_days: int,
              multiplier_max: int, now: str, review: Review, change_id: str) -> bool:
        # the review must be a judgment of this exact memory and version: a
        # mismatch here would delete one row while recording the judgment as
        # if it were about another, or about a version that never existed
        if (review.memory_id, review.memory_version) != (memory_id, judged_version):
            raise ValueError("retire: review must judge memory_id at judged_version")
        with self._database.write(create=False) as conn:
            memory = _row(conn, memory_id, include_deleted=False)
            # memory_read never moves the version, so the version alone cannot
            # tell a read that landed while the model judged: last use, the
            # current read count and a newer review are re-checked too
            if memory is None or memory.version != judged_version:
                return False
            reads = conn.execute("SELECT count(*) FROM memory_reads WHERE memory_id = ?",
                                 (memory_id,)).fetchone()[0]
            cutoff = timestamp_shift(now, days=-effective_ttl_days(ttl_days, reads,
                                                                   multiplier_max))
            if _last_use(memory) > cutoff or conn.execute(
                    "SELECT 1 FROM dream_reviews WHERE memory_id = ? AND next_review_at > ?",
                    (memory_id, now)).fetchone():
                return False
            change_row = _soft_delete_row(conn, memory, now)
            _save_review(conn, review)
            _log(conn, Change(change_id=change_id, run_id=review.run_id, kind="retire",
                              project_id=memory.project_id, applied_at=now,
                              rows=(change_row,), reason=review.reason, undone_at=None))
            return True

    def record_review(self, review: Review) -> bool:
        with self._database.write(create=False) as conn:
            # a judgment holds only for the content it read: an edit, a delete or a
            # purge since then makes it stale, and nothing is recorded
            if conn.execute("SELECT 1 FROM memories WHERE id = ? AND version = ? "
                            "AND deleted_at IS NULL",
                            (review.memory_id, review.memory_version)).fetchone() is None:
                return False
            _save_review(conn, review)
            return True

    def quarantine(self, memory_id: str, *, expected_version: int, run_id: str, rule_id: str,
                   change_id: str, now: str) -> Change | None:
        with self._database.write(create=False) as conn:
            memory = _row(conn, memory_id, include_deleted=False)
            if memory is None or memory.version != expected_version:
                return None                  # moved since it was scanned: the next run looks again
            change = Change(change_id=change_id, run_id=run_id, kind="secret",
                            project_id=memory.project_id, applied_at=now,
                            rows=(_soft_delete_row(conn, memory, now),), reason=rule_id,
                            undone_at=None)
            _log(conn, change)
            return change

    def start_run(self, run: DreamRun) -> None:
        with self._database.write(create=False) as conn:
            # called under the run lock: any other running row is a run that died
            conn.execute("UPDATE dream_runs SET status = 'failed', finished_at = ? "
                         "WHERE status = 'running'", (run.started_at,))
            self._insert_run(conn, run)

    def insert_run(self, run: DreamRun) -> None:
        with self._database.write(create=False) as conn:
            self._insert_run(conn, run)

    @staticmethod
    def _insert_run(conn: sqlite3.Connection, run: DreamRun) -> None:
        row = run_to_row(run)
        run_from_row(row)
        conn.execute(f"INSERT INTO dream_runs ({RUN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)", row)

    def finish_run(self, run_id: str, *, status: str, report: dict, finished_at: str) -> None:
        with self._database.write(create=False) as conn:
            conn.execute("UPDATE dream_runs SET status = ?, report = ?, finished_at = ? "
                         "WHERE run_id = ?", (status, dumps_json(report), finished_at, run_id))

    def runs(self, limit: int) -> list[DreamRun]:
        return _decoded(self._rows(f"SELECT {RUN_COLUMNS} FROM dream_runs "
                                   "ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)),
                        run_from_row)

    def run(self, run_id: str) -> DreamRun | None:
        found = _decoded(self._rows(f"SELECT {RUN_COLUMNS} FROM dream_runs WHERE run_id = ?",
                                    (run_id,)), run_from_row)
        return found[0] if found else None

    def changes_of_run(self, run_id: str) -> list[Change]:
        return _decoded(self._rows(f"SELECT {CHANGE_COLUMNS} FROM dream_changes "
                                   "WHERE run_id = ? ORDER BY applied_at, change_id",
                                   (run_id,)), change_from_row)
