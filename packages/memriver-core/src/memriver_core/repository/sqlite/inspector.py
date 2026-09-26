"""Whole-store administrative inspection of the SQLite store.

The serving read paths skip what they cannot trust, which is right for an
agent and wrong for a doctor. This inspector walks the same tables and keeps
what reads drop, each finding with a fixed reason. It never creates the
database; the one change it may make is the v1/v2 -> v3 schema upgrade every
opener runs first (`upgrade_if_needed`), after which it reads read-only.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

from memriver_core.models import (
    ID_RE,
    InspectedMemory,
    InspectedProject,
    SessionKey,
    StoreFinding,
    StoreReport,
)
from memriver_core.models.errors import StorageFailure

from .. import directories
from .database import (
    CHANGE_COLUMNS,
    DATABASE_FILENAME,
    MEMORY_COLUMNS,
    PROJECT_COLUMNS,
    READ_COLUMNS,
    REVIEW_COLUMNS,
    RUN_COLUMNS,
    SCHEMA_VERSION,
    SET_COLUMNS,
    SOURCE_COLUMNS,
    STATE_COLUMNS,
    _lenient_text,
    change_from_row,
    memory_from_row,
    project_from_row,
    read_row_check,
    review_from_row,
    run_from_row,
    set_row_check,
    source_from_row,
    state_row_check,
    upgrade_if_needed,
)
from .session_store import SESSION_COLUMNS, session_from_row

# the names the file store of earlier versions used at the store root
_LEGACY_NAMES = ("global", "memories", "projects", "registry", "store.toml")

_REASONS = {
    "unknown-schema": "database schema is not one this version knows",
    "unsafe-database": "memriver.db is a symlink or not a regular file; it is not followed",
    "integrity": "SQLite integrity check failed",
    "orphan": "memory belongs to a project that does not exist",
    "invalid-row": "row holds a value memriver could not have written",
    "non-canonical-root": "bound directory is no longer a canonical path",
    "unverifiable-root": "bound directory could not be checked",
    "legacy-layout": "file-store layout from an earlier version; this version does not read it",
    "root-conflict": "two projects are bound to the same directory under different spellings",
    "session-orphan": "session refers to a project that does not exist",
}


def _finding(kind: str, location: str, *, project_id: str | None = None,
             memory_id: str | None = None) -> StoreFinding:
    return StoreFinding(kind=kind, project_id=project_id, location_hint=location,
                        memory_id=memory_id, reason=_REASONS[kind])


def _sorted_findings(findings: list[StoreFinding]) -> tuple[StoreFinding, ...]:
    return tuple(sorted(findings, key=lambda f: (f.location_hint, f.kind)))


def _shaped_id(value: object) -> str | None:
    """`value` as an addressable id, or None -- never runs a str-only regex on non-str."""
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def _session_location(harness: object, session_id: object) -> str:
    """`sessions/<harness>/<session_id>`, or the bare table name when either
    column is not a session id memriver could have written (never runs
    `SessionKey`'s printability check against a non-str, and never shows a raw
    control character or non-str value in a location hint)."""
    if isinstance(harness, str) and isinstance(session_id, str):
        try:
            key = SessionKey(harness, session_id)
        except ValueError:
            pass
        else:
            return f"sessions/{key.harness}/{key.session_id}"
    return "sessions"


# (table, columns, decoder, whether the first column is an addressable id
# worth naming in the location hint)
_DREAM_TABLES = (
    ("memory_source_sets", SET_COLUMNS, set_row_check, True),
    ("memory_sources", SOURCE_COLUMNS, source_from_row, True),
    ("memory_reads", READ_COLUMNS, read_row_check, True),
    ("dream_changes", CHANGE_COLUMNS, change_from_row, True),
    ("dream_reviews", REVIEW_COLUMNS, review_from_row, True),
    ("dream_state", STATE_COLUMNS, state_row_check, False),
    ("dream_runs", RUN_COLUMNS, run_from_row, True),
)
# the maintenance tables with a REFERENCES column, and each one's first column
# (the id worth naming in a finding's location hint); `PRAGMA integrity_check`
# never checks foreign keys, and a raw writer with `PRAGMA foreign_keys` off can
# plant a well-shaped id that names no such row, past every shape check above
_FK_CHECKED_TABLES = {table: columns.split(",")[0].strip() for table, columns, _, _ in
                      _DREAM_TABLES if table in
                      ("memory_source_sets", "memory_sources", "memory_reads", "dream_reviews")}


class SqliteStoreInspector:
    """`StoreInspector` over the SQLite store: every row, read-only once the
    v1/v2 -> v3 upgrade (if one is due) has run."""

    def __init__(self, root: Path, *, busy_timeout_ms: int) -> None:
        self.root = Path(root)
        self._busy_timeout_ms = busy_timeout_ms

    def inspect(self) -> StoreReport:
        try:
            root_stat = self.root.stat()
        except FileNotFoundError:
            return StoreReport(initialized=False, entries=(), projects=(), findings=())
        except OSError as err:
            raise StorageFailure from err
        if not stat.S_ISDIR(root_stat.st_mode):
            raise StorageFailure
        findings = [_finding("legacy-layout", name) for name in _LEGACY_NAMES
                    if os.path.lexists(self.root / name)]
        path = self.root / DATABASE_FILENAME
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return StoreReport(initialized=False, entries=(), projects=(),
                               findings=_sorted_findings(findings))
        except OSError as err:
            raise StorageFailure from err
        if not stat.S_ISREG(info.st_mode):
            findings.append(_finding("unsafe-database", DATABASE_FILENAME))
            return StoreReport(initialized=True, entries=(), projects=(),
                               findings=_sorted_findings(findings))
        try:
            upgrade_if_needed(path, busy_timeout_ms=self._busy_timeout_ms)
        except StorageFailure:
            pass   # the v1 it leaves behind is reported below, by the ordinary version check
        try:
            # the same per-connection settings as Database's reads (mode=rw so a
            # hot journal left by a crashed writer can be rolled back; query_only
            # keeps it read-only; isolation_level=None so the explicit BEGIN below
            # is the only transaction sqlite3 ever opens); not Database.read(),
            # which would turn an unknown schema into a failure instead of a finding
            conn = sqlite3.connect(f"{Path(os.path.abspath(path)).as_uri()}?mode=rw", uri=True,
                                   isolation_level=None, timeout=self._busy_timeout_ms / 1000)
        except sqlite3.Error as err:
            raise StorageFailure from err
        # a STRICT TEXT column is not guaranteed valid UTF-8; decode leniently
        # so one damaged column becomes an invalid-row finding, not a crash
        conn.text_factory = _lenient_text
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA query_only = ON")
            # one read transaction: schema, integrity, projects, counts and
            # entries all come from the same snapshot
            conn.execute("BEGIN")
            try:
                snapshot = self._inspect(conn, findings)
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        except sqlite3.Error as err:
            raise StorageFailure from err
        finally:
            conn.close()
        if isinstance(snapshot, StoreReport):
            return snapshot
        # the directory checks stat the filesystem, so they run only after the
        # ROLLBACK: a hung network mount must not hold the read lock and block writers
        initialized, entries, rows, sources = snapshot
        projects = _classify_roots(rows, findings)
        return StoreReport(initialized=initialized, entries=tuple(entries),
                           projects=tuple(projects), findings=_sorted_findings(findings),
                           sources=sources)

    def _inspect(self, conn: sqlite3.Connection, findings: list[StoreFinding]
                 ) -> (StoreReport |
                       tuple[bool, list[InspectedMemory], list[InspectedProject],
                             frozenset[tuple[str, str]]]):
        """A finished report for an empty or unknown store, else the snapshot's rows."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        if version == 0 and tables == 0:
            return StoreReport(initialized=False, entries=(), projects=(),
                               findings=_sorted_findings(findings))
        if version != SCHEMA_VERSION:
            findings.append(_finding("unknown-schema", DATABASE_FILENAME))
            return StoreReport(initialized=True, entries=(), projects=(),
                               findings=_sorted_findings(findings))
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            findings.append(_finding("integrity", DATABASE_FILENAME))
        orphans = {row[0] for row in conn.execute(
            "SELECT m.id FROM memories m LEFT JOIN projects p ON p.id = m.project_id "
            "WHERE p.id IS NULL")}
        # an undecodable id (see _lenient_text) survives as bytes, so this set can
        # mix str and bytes: sort by a type-stable key, never the raw values
        for memory_id in sorted(orphans, key=repr):
            shaped = _shaped_id(memory_id)
            findings.append(_finding("orphan", f"memories/{shaped}" if shaped else "memories",
                                     memory_id=shaped))
        rows, initialized = self._projects(conn, findings)
        entries: list[InspectedMemory] = []
        # every row is validated, deleted ones too; only active valid rows are entries
        for row in conn.execute(f"SELECT {MEMORY_COLUMNS} FROM memories ORDER BY id"):
            memory_id = row[0]
            if memory_id in orphans:
                continue
            try:
                memory = memory_from_row(row)
            except ValueError:
                shaped = _shaped_id(memory_id)
                findings.append(_finding("invalid-row",
                                         f"memories/{shaped}" if shaped else "memories",
                                         memory_id=shaped))
                continue
            if memory.deleted_at is None:
                entries.append(InspectedMemory(memory=memory,
                                               location_hint=f"memories/{memory.id}"))
        self._sessions(conn, findings)
        self._dream_rows(conn, findings)
        self._dream_foreign_keys(conn, findings)
        return initialized, entries, rows, self._sources(conn)

    def _sources(self, conn: sqlite3.Connection) -> frozenset[tuple[str, str]]:
        """(derived_id, source_id) for every memory's effective source set (the
        set recorded at the greatest `memory_source_sets` version not above the
        memory's own -- the same rule `MaintenanceStore.sources_of` applies),
        so a diagnostics policy can tell a dream-kept source apart from an
        unrelated near-duplicate without reaching past this port into SQL."""
        pairs: set[tuple[str, str]] = set()
        for derived_id, source_id in conn.execute(
                "SELECT s.derived_id, s.source_id FROM memory_sources s "
                "JOIN memories d ON d.id = s.derived_id "
                "WHERE s.derived_version = (SELECT max(t.derived_version) "
                "FROM memory_source_sets t "
                "WHERE t.derived_id = d.id AND t.derived_version <= d.version)"):
            derived, source = _shaped_id(derived_id), _shaped_id(source_id)
            if derived and source:
                pairs.add((derived, source))
        return frozenset(pairs)

    def _dream_rows(self, conn: sqlite3.Connection, findings: list[StoreFinding]) -> None:
        """Every maintenance-table row memriver could not have written."""
        for table, columns, decode, named in _DREAM_TABLES:
            for row in conn.execute(f"SELECT {columns} FROM {table} ORDER BY rowid"):
                try:
                    decode(row)
                except ValueError:
                    shaped = _shaped_id(row[0]) if named else None
                    findings.append(_finding("invalid-row",
                                             f"{table}/{shaped}" if shaped else table))

    def _dream_foreign_keys(self, conn: sqlite3.Connection,
                            findings: list[StoreFinding]) -> None:
        """A dangling reference in a maintenance table: well-shaped, but naming no row --
        the one thing the shape checks above cannot see on their own."""
        seen: set[tuple[str, object]] = set()
        for table, column in _FK_CHECKED_TABLES.items():
            for violation in conn.execute(f"PRAGMA foreign_key_check({table})"):
                rowid = violation[1]
                if (table, rowid) in seen:
                    continue
                seen.add((table, rowid))
                found = conn.execute(f"SELECT {column} FROM {table} WHERE rowid = ?",
                                     (rowid,)).fetchone()
                shaped = _shaped_id(found[0]) if found else None
                findings.append(_finding("invalid-row",
                                         f"{table}/{shaped}" if shaped else table))

    def _sessions(self, conn: sqlite3.Connection, findings: list[StoreFinding]) -> None:
        """Session-row findings only: `memriver sessions` reads sessions itself
        (spec section 9); the doctor's own entries/counts stay memory-only."""
        orphans = {(row[0], row[1]) for row in conn.execute(
            "SELECT s.harness, s.session_id FROM sessions s "
            "LEFT JOIN projects p ON p.id = s.project_id "
            "WHERE s.project_id IS NOT NULL AND p.id IS NULL "
            "UNION "
            "SELECT s.harness, s.session_id FROM sessions s "
            "LEFT JOIN projects p ON p.id = s.candidate_id "
            "WHERE s.candidate_id IS NOT NULL AND p.id IS NULL")}
        # a session key can mix str and bytes the same way an undecodable
        # memory/project id can (see _lenient_text); sort by a type-stable key
        for harness, session_id in sorted(orphans, key=repr):
            findings.append(_finding("session-orphan", _session_location(harness, session_id)))
        for row in conn.execute(f"SELECT {SESSION_COLUMNS} FROM sessions "
                                "ORDER BY harness, session_id"):
            key = (row[0], row[1])
            if key in orphans:
                continue                    # already filed as session-orphan, not invalid-row
            try:
                session_from_row(row)
            except ValueError:
                findings.append(_finding("invalid-row", _session_location(*key)))

    def _projects(self, conn: sqlite3.Connection,
                  findings: list[StoreFinding]) -> tuple[list[InspectedProject], bool]:
        """Every valid project row with its counts; root_state is filled in later
        by `_classify_roots`, outside the transaction ("unbound" until then)."""
        counts = {(project_id, deleted): n for project_id, deleted, n in conn.execute(
            "SELECT project_id, deleted_at IS NOT NULL, count(*) FROM memories "
            "GROUP BY project_id, deleted_at IS NOT NULL")}
        projects: list[InspectedProject] = []
        initialized = False
        for row in conn.execute(f"SELECT {PROJECT_COLUMNS} FROM projects "
                                "ORDER BY is_global, name, id"):
            try:
                project, is_global = project_from_row(row)
            except ValueError:
                shaped = _shaped_id(row[0])
                findings.append(_finding("invalid-row",
                                         f"projects/{shaped}" if shaped else "projects",
                                         project_id=shaped))
                continue
            initialized = initialized or is_global
            projects.append(InspectedProject(
                id=project.id, name=project.name, root=project.root, is_global=is_global,
                root_state="unbound", active_memories=counts.get((project.id, 0), 0),
                deleted_memories=counts.get((project.id, 1), 0)))
        return projects, initialized


def _classify_roots(rows: list[InspectedProject],
                    findings: list[StoreFinding]) -> list[InspectedProject]:
    """Each bound root's state and every pairwise alias, from the filesystem."""
    projects: list[InspectedProject] = []
    for project in rows:
        if project.root is not None:
            state = directories.root_state(project.root)
            if state == "not-canonical":
                findings.append(_finding("non-canonical-root", f"projects/{project.id}",
                                         project_id=project.id))
            elif state == "unverifiable":
                findings.append(_finding("unverifiable-root", f"projects/{project.id}",
                                         project_id=project.id))
            project = replace(project, root_state=state)
        projects.append(project)
    # the resolver pools exact and alias matches; a physical alias between two
    # projects' roots degrades it, so doctor must name it. A root already
    # unverifiable on its own has already been reported once above; pairing
    # it further would blame a healthy neighbour and could double-report it,
    # so such a project sits out every pair, and a pair that only turns
    # unverifiable on the comparison itself is filed once per project, ever.
    bound = [p for p in projects if p.root is not None]
    already_unverifiable: set[str] = set()
    for index, first in enumerate(bound):
        if first.root_state == "unverifiable":
            continue
        for second in bound[index + 1:]:
            if second.root_state == "unverifiable":
                continue
            same = directories.same_directory(first.root, second.root)
            if same is None:
                for project in (first, second):
                    if project.id not in already_unverifiable:
                        findings.append(_finding("unverifiable-root",
                                                 f"projects/{project.id}",
                                                 project_id=project.id))
                        already_unverifiable.add(project.id)
            elif same:
                findings.append(_finding("root-conflict", f"projects/{second.id}",
                                         project_id=second.id))
    return projects
