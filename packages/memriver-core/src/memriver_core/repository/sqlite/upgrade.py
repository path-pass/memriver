"""The offline rebuild of a store below schema v4 into a fresh v4 file (spec §9).

Never in place: the live file is attached read-only to a new file built beside
it with the one schema definition fresh stores use, filled in one transaction,
verified, and published with one `os.replace`. Any failure before that replace
removes the new file and leaves the live file as it was, so the live path
always holds a working store. A v1 or v2 file is read in v3 shape by the
import statements themselves (a column or table it lacks reads as NULL or as
no rows); nothing ever alters it.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from memriver_core.models.errors import StorageFailure, UpgradeRefused

from .database import (
    _BACKEND_ERRORS,
    DATABASE_FILENAME,
    SCHEMA_VERSION,
    Database,
    create_schema,
)

_UPGRADABLE = (1, 2, 3)
_ACTIVE = "old.memories WHERE deleted_at IS NULL"
_NONE = "SELECT 0"
# sessions' identity, routing and activity columns: every version since v2 has them
_SESSION_COLUMNS = ("harness, session_id, status, origin, project_id, candidate_id, "
                    "candidate_root, entry_cwd, branch, transcript_path, started_at, "
                    "last_active_at, ended_at, prompt_count, last_write_prompt_count, "
                    "last_nudge_prompt_count, first_prompt, recent_prompts")
# the row/version invariant (spec §3.1): each current row equals its version row
_ROWS_MATCHING_THEIR_VERSION = (
    "SELECT count(*) FROM main.memories AS m JOIN main.memory_versions AS v "
    "ON v.memory_id = m.id AND v.version = m.version "
    "WHERE v.type IS m.type AND v.trust IS m.trust AND v.sync IS m.sync "
    "AND v.description IS m.description AND v.body IS m.body "
    "AND v.deleted = (m.deleted_at IS NOT NULL)")


@dataclass(frozen=True)
class UpgradeResult:
    from_version: int | None     # None: no store at the root, nothing to rebuild
    imported: int                # active memories, now each at version 1
    dropped_deleted: int         # soft-deleted memories left out
    reads_kept: int              # memory_reads rows of imported memories
    backup_path: Path | None     # None when nothing was rebuilt


def rebuild_store(root: Path, *, busy_timeout_ms: int, upgrade_lock: Path,
                  work_filename: str, backup_filename: str) -> UpgradeResult:
    """Rebuild the store at `root` as schema v4; see the module docstring.

    `UpgradeRefused` when another upgrade holds `upgrade_lock`, or when the
    new file fails verification; `StorageFailure` for a schema
    version this code does not know or any backend failure. Either way the
    live file is exactly as it was.
    """
    root = Path(root)
    live = root / DATABASE_FILENAME
    if not Database(root, busy_timeout_ms=busy_timeout_ms).exists():
        return UpgradeResult(None, 0, 0, 0, None)     # creates nothing, not even a lock
    work = root / work_filename
    backup = root / backup_filename
    try:
        with _locked(upgrade_lock):
            # re-read under the locks: a peer may have published v4 since
            version = _stored_version(live, busy_timeout_ms)
            if version is None or version == SCHEMA_VERSION:
                return UpgradeResult(version, 0, 0, 0, None)
            if version not in _UPGRADABLE:
                raise StorageFailure
            _discard(work)       # a killed upgrade's leftover: nobody else builds under the lock
            try:
                imported, dropped_deleted, reads_kept = _build(work, live, version,
                                                               busy_timeout_ms)
                _copy_backup(live, backup)
                os.replace(work, live)       # the one publish: the live path is never empty
            except BaseException:
                _discard(work)
                raise
    except _BACKEND_ERRORS as err:
        raise StorageFailure from err
    return UpgradeResult(version, imported, dropped_deleted, reads_kept, backup)


@contextlib.contextmanager
def _locked(upgrade_lock: Path) -> Iterator[None]:
    """The upgrade lock held, non-blocking; a flock dies with its process.

    Only this one: any other lock the rebuild must wait out (a plug-in's run
    lock) is its caller's to take, since core knows no plug-in.
    """
    fd = os.open(upgrade_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpgradeRefused("upgrade-running") from None
        yield
    finally:
        os.close(fd)


def _uri(path: Path, mode: str) -> str:
    return f"{Path(os.path.abspath(path)).as_uri()}?mode={mode}"


def _connect(path: Path, mode: str, busy_timeout_ms: int) -> sqlite3.Connection:
    return sqlite3.connect(_uri(path, mode), uri=True, isolation_level=None,
                           timeout=busy_timeout_ms / 1000)


def _scalar(conn: sqlite3.Connection, query: str) -> object:
    return conn.execute(query).fetchone()[0]


def _stored_version(live: Path, busy_timeout_ms: int) -> int | None:
    """The live file's user_version; None for no store (gone, or created but still empty).

    mode=rw with query_only, like every reader: the read rolls back a crashed
    writer's hot journal, so the read-only attachment later finds none.
    """
    try:
        conn = _connect(live, "rw", busy_timeout_ms)
    except sqlite3.OperationalError:
        if not live.exists():
            return None
        raise
    with contextlib.closing(conn):
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        version = _scalar(conn, "PRAGMA user_version")
        empty = _scalar(conn, "SELECT count(*) FROM sqlite_master") == 0
        conn.execute("ROLLBACK")
    return None if version == 0 and empty else version


def _imports(version: int) -> list[str]:
    """The copy into the new file; a table the old version lacks is skipped.

    A module-level function so a test can drop or corrupt one statement and
    watch verification refuse the result.
    """
    last_read_at = "last_read_at" if version >= 2 else "NULL"
    statements = [
        ("INSERT INTO main.projects (id, name, root, is_global) "
         "SELECT id, name, root, is_global FROM old.projects"),
        # as it is, at version 1: no text normalization, no content-policy check
        ("INSERT INTO main.memories (id, project_id, source_harness, source_method, created, "
         "version, type, trust, sync, description, body, updated, deleted_at, last_read_at) "
         "SELECT id, project_id, source_harness, source_method, created, 1, type, trust, "
         f"sync, description, body, updated, NULL, {last_read_at} FROM {_ACTIVE}"),
        # an imported version belongs to no change, so the migration cannot be undone
        ("INSERT INTO main.memory_versions (memory_id, version, type, trust, sync, "
         "description, body, deleted, change_id) "
         "SELECT id, 1, type, trust, sync, description, body, 0, NULL FROM main.memories"),
    ]
    if version >= 2:
        # v3 also stamps summary_at on an outcome without text: only a
        # published summary is carried, with its time
        summary = ("summary, CASE WHEN summary IS NOT NULL THEN summary_at END"
                   if version >= 3 else "NULL, NULL")
        statements += [
            (f"INSERT INTO main.sessions ({_SESSION_COLUMNS}, summary, summary_at) "
             f"SELECT {_SESSION_COLUMNS}, {summary} FROM old.sessions"),
            ("INSERT INTO main.tool_calls (harness, call_id, session_id, recorded_at) "
             "SELECT harness, call_id, session_id, recorded_at FROM old.tool_calls"),
        ]
    if version >= 3:
        statements.append(
            "INSERT INTO main.memory_reads (memory_id, memory_version, read_at, harness, "
            "session_id) SELECT r.memory_id, 1, r.read_at, r.harness, r.session_id "
            "FROM old.memory_reads AS r JOIN main.memories AS m ON m.id = r.memory_id "
            "ORDER BY r.rowid")
    return statements


def _expected_counts(version: int) -> dict[str, str]:
    """Each new table's row count, computed from the old file alone."""
    return {
        "projects": "SELECT count(*) FROM old.projects",
        "memories": f"SELECT count(*) FROM {_ACTIVE}",
        "memory_versions": f"SELECT count(*) FROM {_ACTIVE}",
        "memory_sources": _NONE,
        "changes": _NONE,
        "change_steps": _NONE,
        "sessions": "SELECT count(*) FROM old.sessions" if version >= 2 else _NONE,
        "tool_calls": "SELECT count(*) FROM old.tool_calls" if version >= 2 else _NONE,
        "memory_reads": (f"SELECT count(*) FROM old.memory_reads WHERE memory_id IN "
                         f"(SELECT id FROM {_ACTIVE})") if version >= 3 else _NONE,
    }


def _schema(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT type, name, tbl_name, sql FROM main.sqlite_master "
                        "ORDER BY type, name").fetchall()


def _fresh_schema() -> list[tuple]:
    with contextlib.closing(sqlite3.connect(":memory:", isolation_level=None)) as conn:
        create_schema(conn)
        return _schema(conn)


def _verify(conn: sqlite3.Connection, version: int) -> None:
    for table, expected in _expected_counts(version).items():
        if _scalar(conn, f"SELECT count(*) FROM main.{table}") != _scalar(conn, expected):
            raise UpgradeRefused("counts")
    if (_scalar(conn, _ROWS_MATCHING_THEIR_VERSION)
            != _scalar(conn, "SELECT count(*) FROM main.memories")):
        raise UpgradeRefused("invariant")
    if conn.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
        raise UpgradeRefused("foreign-keys")
    if _schema(conn) != _fresh_schema():
        raise UpgradeRefused("schema")


def _build(work: Path, live: Path, version: int, busy_timeout_ms: int) -> tuple[int, int, int]:
    """The new file, complete, verified and closed: (imported, dropped_deleted, reads_kept)."""
    os.close(os.open(work, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    with contextlib.closing(_connect(work, "rw", busy_timeout_ms)) as conn:
        # foreign keys stay off for the bulk copy: _verify checks them in full
        conn.execute("ATTACH DATABASE ? AS old", (_uri(live, "ro"),))
        conn.execute("BEGIN IMMEDIATE")
        try:
            create_schema(conn)
            for statement in _imports(version):
                conn.execute(statement)
            _verify(conn, version)
            counts = (_scalar(conn, "SELECT count(*) FROM main.memories"),
                      _scalar(conn, "SELECT count(*) FROM old.memories "
                                    "WHERE deleted_at IS NOT NULL"),
                      _scalar(conn, "SELECT count(*) FROM main.memory_reads"))
            conn.execute(f"PRAGMA main.user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return counts


def _copy_backup(live: Path, backup: Path) -> None:
    """A byte copy of the old file, private like the store; the live file stays in place."""
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with open(fd, "wb") as target, open(live, "rb") as source:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())


def _discard(work: Path) -> None:
    for path in (work, work.with_name(f"{work.name}-journal")):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
