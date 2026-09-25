"""`MaintenanceStore` over the SQLite database: what the maintenance run reads and writes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from memriver_core.models import (
    Candidate,
    Change,
    Memory,
    SourceRef,
    effective_ttl_days,
    timestamp_shift,
)

from .database import (
    CHANGE_COLUMNS,
    MEMORY_COLUMNS,
    REVIEW_COLUMNS,
    SOURCE_COLUMNS,
    Database,
    change_from_row,
    memory_from_row,
    review_from_row,
    source_from_row,
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
        return [row[0] for row in self._rows(
            "SELECT DISTINCT d.id FROM memory_sources s JOIN memories d ON d.id = s.derived_id "
            f"WHERE s.source_id = ? AND d.deleted_at IS NULL AND {_EFFECTIVE} ORDER BY d.id",
            (memory_id,))]

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
        rows = self._rows("SELECT fingerprint FROM dream_state WHERE scope = ?", (scope,))
        return rows[0][0] if rows and isinstance(rows[0][0], str) else None

    def changes(self, limit: int) -> list[Change]:
        return _decoded(self._rows(f"SELECT {CHANGE_COLUMNS} FROM dream_changes "
                                   "ORDER BY applied_at DESC, change_id LIMIT ?", (limit,)),
                        change_from_row)
