"""The one SQLite database behind both stores: connecting, schema, rows.

One connection per operation, closed at its end: a long-lived MCP server never
holds a handle across a file replaced by migration or restore. Reads never
create anything; the first write creates the directory, the file and the
schema, deciding "fresh or not" only after it holds the write lock, so two
first writers cannot both create the schema.

A read opens the existing file with mode=rw plus `PRAGMA query_only`, not
mode=ro: a writer that crashed mid-transaction leaves a hot journal, and only
a connection with write access can roll it back (a mode=ro open fails until
someone writes). mode=rw never creates a database, so reads still create
nothing, and query_only keeps the connection itself read-only.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import get_args

from memriver_core.models import (
    ID_RE,
    Memory,
    MemoryType,
    Project,
    Trust,
    is_timestamp,
    single_line,
)
from memriver_core.models.errors import StorageFailure

DATABASE_FILENAME = "memriver.db"
SCHEMA_VERSION = 2

# shared between a fresh v2 create (_SCHEMA) and the v1 -> v2 upgrade
# (_UPGRADE_STATEMENTS): copied verbatim from spec §3.1
_SESSIONS_TABLE = """CREATE TABLE sessions (
  harness            TEXT NOT NULL CHECK (harness IN ('claude-code','codex')),
  session_id         TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
  status             TEXT NOT NULL CHECK (status IN ('registered','pending')),
  origin             TEXT NOT NULL CHECK (origin IN ('start','first-seen')),
  project_id         TEXT REFERENCES projects(id),     -- registered: the project or NULL (entry unbound)
  candidate_id       TEXT REFERENCES projects(id),     -- pending: the project to confirm, or NULL
  candidate_root     TEXT,                             -- pending: projects.root seen when the candidate was computed
  entry_cwd          TEXT NOT NULL,
  branch             TEXT,
  transcript_path    TEXT,
  started_at         TEXT NOT NULL,                    -- first time memriver saw the session
  last_active_at     TEXT NOT NULL,
  ended_at           TEXT,                             -- last SessionEnd received
  prompt_count       INTEGER NOT NULL DEFAULT 0 CHECK (prompt_count >= 0),
  last_write_prompt_count INTEGER NOT NULL DEFAULT 0 CHECK (last_write_prompt_count >= 0),
  last_nudge_prompt_count INTEGER NOT NULL DEFAULT 0 CHECK (last_nudge_prompt_count >= 0),
  first_prompt       TEXT,                             -- JSON PromptEntry or NULL
  recent_prompts     TEXT NOT NULL DEFAULT '[]',       -- JSON array of PromptEntry, newest last, at most 5
  PRIMARY KEY (harness, session_id),
  CHECK (status = 'registered' OR (project_id IS NULL AND origin = 'first-seen'))
) STRICT"""
_SESSIONS_INDEX = "CREATE INDEX sessions_by_project ON sessions(project_id, last_active_at DESC)"
# Claude Code's tool_use_id -> the session that made the call, recorded by its
# PreToolUse hook: its MCP server outlives /clear and an in-app /resume, so the
# call, not the server's environment, names the current session (spec U15)
_TOOL_CALLS_TABLE = """CREATE TABLE tool_calls (
  harness     TEXT NOT NULL CHECK (harness IN ('claude-code','codex')),
  call_id     TEXT NOT NULL CHECK (length(call_id) BETWEEN 1 AND 256),
  session_id  TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (harness, call_id)
) STRICT"""
_TOOL_CALLS_INDEX = "CREATE INDEX tool_calls_by_recorded_at ON tool_calls(recorded_at)"

_SCHEMA = (
    """CREATE TABLE projects (
      id        TEXT PRIMARY KEY NOT NULL CHECK (length(id) = 10),
      name      TEXT NOT NULL CHECK (length(name) >= 1),
      root      TEXT UNIQUE,
      is_global INTEGER NOT NULL DEFAULT 0 CHECK (is_global IN (0, 1)),
      CHECK (is_global = 0 OR root IS NULL)
    ) STRICT""",
    "CREATE UNIQUE INDEX projects_one_global ON projects(is_global) WHERE is_global = 1",
    """CREATE TABLE memories (
      id             TEXT PRIMARY KEY NOT NULL CHECK (length(id) = 10),
      project_id     TEXT NOT NULL REFERENCES projects(id),
      type           TEXT NOT NULL CHECK (type IN ('user','feedback','project','reference')),
      source_harness TEXT NOT NULL,
      source_method  TEXT NOT NULL,
      trust          TEXT NOT NULL CHECK (trust IN ('user','agent','untrusted-derived')),
      sync           INTEGER NOT NULL CHECK (sync IN (0, 1)),
      description    TEXT NOT NULL,
      body           TEXT NOT NULL,
      created        TEXT NOT NULL,
      updated        TEXT NOT NULL,
      version        INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
      deleted_at     TEXT,
      last_read_at   TEXT
    ) STRICT""",
    ("CREATE INDEX memories_active_by_project "
     "ON memories(project_id, updated DESC) WHERE deleted_at IS NULL"),
    _SESSIONS_TABLE,
    _SESSIONS_INDEX,
    _TOOL_CALLS_TABLE,
    _TOOL_CALLS_INDEX,
)

# the v1 -> v2 upgrade (spec §3.2): a module-level tuple so a test can
# monkeypatch it to fail part-way and prove the whole transaction rolls back
_UPGRADE_STATEMENTS = (
    "ALTER TABLE memories ADD COLUMN last_read_at TEXT",   # NULL = never read since v2
    _SESSIONS_TABLE,
    _SESSIONS_INDEX,
    _TOOL_CALLS_TABLE,
    _TOOL_CALLS_INDEX,
)

MEMORY_COLUMNS = ("id, project_id, type, source_harness, source_method, trust, sync, "
                  "description, body, created, updated, version, deleted_at, last_read_at")
PROJECT_COLUMNS = "id, name, root, is_global"

# anything a driver or the OS can raise while we talk to the file; a lone
# surrogate in a bound string is a UnicodeEncodeError, not a sqlite3.Error
_BACKEND_ERRORS = (sqlite3.Error, OSError, UnicodeError)


def _lenient_text(data: bytes) -> str | bytes:
    """Decode a TEXT column as UTF-8; hand back the raw bytes when it is not.

    A STRICT table's TEXT affinity does not stop a raw writer from planting
    invalid UTF-8 (``CAST(X'80' AS TEXT)``). sqlite3's default text_factory
    would raise `OperationalError` while fetching such a row, turning one
    damaged row into a failure of the whole query -- every other row of the
    same fetch, and every other row of the store the row shares a table with.
    Bytes fail the `isinstance(value, str)` checks in
    `memory_from_row`/`project_from_row`, so the row becomes a `ValueError`
    instead of a crash: skipped where a caller can move on, `StorageFailure`
    where it cannot.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def memory_to_row(memory: Memory) -> tuple:
    return (memory.id, memory.project_id, memory.type, memory.source["harness"],
            memory.source["method"], memory.trust, int(memory.sync), memory.description,
            memory.body, memory.created, memory.updated, memory.version, memory.deleted_at,
            memory.last_read_at)


def memory_from_row(row: Sequence[object]) -> Memory:
    """A validated Memory; ValueError for a row memriver could not have written.

    Table constraints do not replace this: `length(id) = 10` accepts
    '../../evil', and a writer with foreign keys off is not stopped at all.
    """
    (memory_id, project_id, type_, harness, method, trust, sync, description, body,
     created, updated, version, deleted_at, last_read_at) = row
    texts = (memory_id, project_id, type_, harness, method, trust, description, body,
             created, updated)
    if not all(isinstance(value, str) for value in texts):
        raise ValueError("a text column holds something else")
    if not (ID_RE.fullmatch(memory_id) and ID_RE.fullmatch(project_id)):
        raise ValueError("an id is not addressable")
    if type_ not in get_args(MemoryType) or trust not in get_args(Trust):
        raise ValueError("unknown type or trust")
    if type(sync) is not int or sync not in (0, 1):
        raise ValueError("sync is not 0 or 1")
    if type(version) is not int or version < 1:
        raise ValueError("version is not a positive integer")
    if deleted_at is not None and not isinstance(deleted_at, str):
        raise ValueError("deleted_at is not text")
    if last_read_at is not None and not is_timestamp(last_read_at):
        raise ValueError("last_read_at is not a timestamp")
    return Memory(id=memory_id, project_id=project_id, type=type_,
                  source={"harness": harness, "method": method}, trust=trust,
                  sync=bool(sync), created=created, updated=updated,
                  description=description, body=body, version=version,
                  deleted_at=deleted_at, last_read_at=last_read_at)


def project_from_row(row: Sequence[object]) -> tuple[Project, bool]:
    """A validated (Project, is_global); ValueError for an impossible row."""
    project_id, name, root, is_global = row
    if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
        raise ValueError("project id is not addressable")
    if not isinstance(name, str) or not name or single_line(name) != name:
        raise ValueError("project name is not one non-empty line")
    # lexical shape only: whether it still exists or was re-pointed is the
    # directory rules' question, and an offline root stays valid
    # POSIX normpath keeps a leading "//", so it is refused on its own
    if root is not None and (not isinstance(root, str) or not os.path.isabs(root)
                             or "\x00" in root or os.path.normpath(root) != root
                             or root.startswith("//")):
        raise ValueError("root is not an absolute, canonical-shaped, addressable path")
    if type(is_global) is not int or is_global not in (0, 1):
        raise ValueError("is_global is not 0 or 1")
    return Project(id=project_id, name=name, root=root), bool(is_global)


def upgrade_if_needed(path: Path, *, busy_timeout_ms: int) -> None:
    """Upgrade an on-disk v1 database to v2 in place; a missing file is a no-op.

    The only upgrade code (spec §3.2): every opener -- `Database.read()`,
    `Database.write()` and the doctor's inspector, which opens its own
    connection -- calls this before its own connection. It opens mode=rw
    (never creates) and runs the whole upgrade in one `BEGIN IMMEDIATE`
    transaction, re-checking `user_version` inside it: a peer that already
    upgraded leaves the re-check at 2 and this does nothing. A failure
    part-way rolls back to an intact v1, since SQLite's DDL is transactional.

    `user_version` is read once outside any transaction first: almost every
    open finds v2 already, and only a database actually at v1 may take the
    write lock -- otherwise every read would queue behind a concurrent
    writer's `BEGIN IMMEDIATE` for a schema that never changes.
    """
    try:
        conn = sqlite3.connect(f"{Path(os.path.abspath(path)).as_uri()}?mode=rw", uri=True,
                               isolation_level=None, timeout=busy_timeout_ms / 1000)
    except sqlite3.OperationalError:
        if not Path(path).exists():
            return                          # nothing to upgrade
        raise StorageFailure from None
    except _BACKEND_ERRORS as err:
        raise StorageFailure from err
    conn.text_factory = _lenient_text
    try:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                return                       # no lock taken: nothing to upgrade
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("PRAGMA user_version").fetchone()[0] == 1:
                for statement in _UPGRADE_STATEMENTS:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except _BACKEND_ERRORS as err:
        raise StorageFailure from err
    finally:
        conn.close()


class Database:
    def __init__(self, root: Path, *, busy_timeout_ms: int) -> None:
        self.root = Path(root)
        self.path = self.root / DATABASE_FILENAME
        self._busy_timeout_ms = busy_timeout_ms

    def exists(self) -> bool:
        """Whether the database file is there; StorageFailure for anything but a regular file."""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return False
        except OSError as err:
            raise StorageFailure from err
        if not stat.S_ISREG(info.st_mode):
            # a symlink is never followed: its target is chosen outside the
            # store, and SQLite would happily open whatever it points at
            raise StorageFailure
        return True

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection | None]:
        """A read-only connection inside one read transaction, or None for an empty store.

        The connection is mode=rw with query_only on (see the module
        docstring): write access only so a hot journal can be rolled back.
        The schema check and the body share one snapshot, so a peer's first
        write cannot land between them.
        """
        if not self.exists():
            yield None
            return
        upgrade_if_needed(self.path, busy_timeout_ms=self._busy_timeout_ms)
        try:
            conn = self._connect(read_only=True)
        except _BACKEND_ERRORS as err:
            raise StorageFailure from err
        try:
            try:
                conn.execute("BEGIN")
                state = self._schema_state(conn)
                if state == "unknown":
                    raise StorageFailure
                yield conn if state == "current" else None
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")     # nothing to keep: the connection is query_only
        except _BACKEND_ERRORS as err:
            raise StorageFailure from err
        finally:
            conn.close()

    @contextmanager
    def write(self, *, create: bool = True) -> Iterator[sqlite3.Connection]:
        """One BEGIN IMMEDIATE transaction; the store and schema are created on demand.

        The body's own exceptions roll back and propagate; every driver error
        that escapes the body becomes StorageFailure. A store that wants to
        name a constraint it hit catches sqlite3.IntegrityError inside the body.

        `create=False` never creates anything: a missing store -- including
        one that disappears between the `exists()` check below and the
        connect -- raises StorageFailure instead of the file, or its
        directory, springing into being (nothing here creates a store).
        """
        if self.exists():
            upgrade_if_needed(self.path, busy_timeout_ms=self._busy_timeout_ms)
        elif create:
            self._create_file()
        else:
            raise StorageFailure
        try:
            conn = self._connect(read_only=False)
        except _BACKEND_ERRORS as err:
            raise StorageFailure from err
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
                # decided under the write lock: a peer that initialized the
                # store since we looked is seen here, never re-created
                state = self._schema_state(conn)
                if state == "unknown":
                    raise StorageFailure
                if state == "fresh":
                    for statement in _SCHEMA:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        except _BACKEND_ERRORS as err:
            raise StorageFailure from err
        finally:
            conn.close()

    def _create_file(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass            # a peer, or an earlier run, created it: use that file
        except OSError as err:
            raise StorageFailure from err
        else:
            os.close(fd)
        self.exists()       # re-lstat: whatever stands there must be a regular file

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        """An existing file, always mode=rw (never creates); read_only adds query_only."""
        uri = f"{Path(os.path.abspath(self.path)).as_uri()}?mode=rw"
        # the busy timeout is set here once; sqlite3 applies it at open
        conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                               timeout=self._busy_timeout_ms / 1000)
        conn.text_factory = _lenient_text
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if read_only:
                conn.execute("PRAGMA query_only = ON")
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _schema_state(conn: sqlite3.Connection) -> str:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return "current"
        if version == 0 and conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0:
            return "fresh"
        return "unknown"
