"""The one database file: creation, schema, refusal of anything unexpected."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
from contextlib import closing

import pytest
from memriver_core.models import Memory, new_id, now
from memriver_core.models.errors import StorageFailure, StoreNeedsUpgrade
from memriver_core.repository.sqlite.database import (
    SCHEMA_VERSION,
    Database,
    create_schema,
    memory_from_row,
    memory_to_row,
    project_from_row,
)


def _db(root) -> Database:
    return Database(root, busy_timeout_ms=2000)


def test_a_read_of_a_missing_store_creates_nothing(tmp_path):
    root = tmp_path / "store"
    with _db(root).read() as conn:
        assert conn is None
    assert not root.exists()


V5_TABLES = {"projects", "memories", "memory_versions", "memory_sources", "changes",
             "change_steps", "memory_reads", "sessions", "tool_calls", "tags"}


def test_the_first_write_creates_a_private_directory_file_and_schema(tmp_path):
    root = tmp_path / "store"
    with _db(root).write() as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'g', NULL, 1)",
                     (new_id(),))
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "memriver.db").st_mode) == 0o600
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == V5_TABLES


def test_create_schema_is_the_whole_v5_definition_and_leaves_the_version_alone():
    with closing(sqlite3.connect(":memory:")) as fresh:
        create_schema(fresh)
        assert fresh.execute("PRAGMA user_version").fetchone()[0] == 0
        assert {r[0] for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")} == V5_TABLES
        indexes = {r[0] for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
    assert indexes == {"projects_one_global", "memories_active_by_project",
                       "memory_sources_by_source", "change_steps_by_memory",
                       "memory_reads_by_memory", "sessions_by_project",
                       "tool_calls_by_recorded_at"}


def test_a_session_summary_needs_its_time_and_the_reverse(tmp_path):
    root = tmp_path / "store"
    with _db(root).write():
        pass
    for summary, summary_at in (("s", None), (None, "2026-09-27T00:00:00.000000Z")):
        with closing(sqlite3.connect(root / "memriver.db")) as conn, \
                pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sessions (harness, session_id, status, origin, entry_cwd, "
                "started_at, last_active_at, summary, summary_at) "
                "VALUES ('codex', 's', 'registered', 'start', '/w', 'a', 'b', ?, ?)",
                (summary, summary_at))


@pytest.mark.parametrize("version", [1, 2, 3])
def test_a_store_below_v4_is_refused_and_left_untouched(tmp_path, version):
    root = tmp_path / "store"
    root.mkdir()
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        conn.execute("CREATE TABLE projects (id TEXT)")
        conn.execute(f"PRAGMA user_version = {version}")
    before = (root / "memriver.db").read_bytes()
    for open_store in (_db(root).read, _db(root).write):
        with pytest.raises(StoreNeedsUpgrade) as excinfo, open_store():
            pass
        assert excinfo.value.version == version
    assert (root / "memriver.db").read_bytes() == before


def test_a_failed_first_write_leaves_no_schema(tmp_path):
    root = tmp_path / "store"
    with pytest.raises(RuntimeError), _db(root).write():
        raise RuntimeError("boom")
    with _db(root).read() as conn:
        assert conn is None                      # an empty file reads as an empty store


def test_an_unknown_schema_version_is_refused_for_reads_and_writes(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        conn.execute("PRAGMA user_version = 7")
    with pytest.raises(StorageFailure), _db(root).read():
        pass
    with pytest.raises(StorageFailure), _db(root).write():
        pass


def test_foreign_tables_at_version_zero_are_refused(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        conn.execute("CREATE TABLE other (x)")
    with pytest.raises(StorageFailure), _db(root).read():
        pass


def test_write_without_create_does_not_create_a_missing_store(tmp_path):
    root = tmp_path / "store"
    with pytest.raises(StorageFailure), _db(root).write(create=False) as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) "
                     "VALUES (?, 'g', NULL, 1)", (new_id(),))
    assert not root.exists()


def test_write_without_create_does_not_recreate_a_store_removed_after_the_check(tmp_path,
                                                                                monkeypatch):
    root = tmp_path / "store"
    database = _db(root)
    monkeypatch.setattr(database, "exists", lambda: True)
    with pytest.raises(StorageFailure), database.write(create=False):
        pass
    assert not root.exists()


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_database_name_that_is_not_a_regular_file_is_refused(tmp_path, kind):
    root = tmp_path / "store"
    root.mkdir()
    if kind == "symlink":
        (tmp_path / "elsewhere.db").write_text("")
        (root / "memriver.db").symlink_to(tmp_path / "elsewhere.db")
    else:
        (root / "memriver.db").mkdir()
    with pytest.raises(StorageFailure), _db(root).read():
        pass
    with pytest.raises(StorageFailure), _db(root).write():
        pass


def test_two_first_writers_racing_from_nothing_both_succeed(tmp_path):
    root = tmp_path / "store"
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def first_write(name: str) -> None:
        try:
            barrier.wait()
            with _db(root).write() as conn:
                conn.execute("INSERT INTO projects (id, name, root, is_global) "
                             "VALUES (?, ?, NULL, 0)", (new_id(), name))
        except BaseException as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=first_write, args=(n,)) for n in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with _db(root).read() as conn:
        assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 2


def test_foreign_keys_are_enforced_and_a_driver_error_never_crosses_the_boundary(tmp_path):
    root = tmp_path / "store"
    with pytest.raises(StorageFailure) as excinfo, _db(root).write() as conn:
        conn.execute(
            "INSERT INTO memories (id, project_id, type, source_harness, source_method, trust, "
            "sync, description, body, created, updated, version) "
            "VALUES (?, ?, 'user', 'h', 'agent', 'agent', 1, '', 'b', 'c', 'u', 1)",
            (new_id(), new_id()))
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)


def test_the_read_connection_enforces_foreign_keys_and_refuses_writes(tmp_path):
    root = tmp_path / "store"
    with _db(root).write():
        pass
    with _db(root).read() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(StorageFailure) as excinfo, _db(root).read() as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'n', NULL, 0)",
                     (new_id(),))
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)


def test_a_read_rolls_back_the_hot_journal_of_a_crashed_writer(tmp_path):
    root = tmp_path / "store"
    kept = new_id()
    with _db(root).write() as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'kept', NULL, 0)",
                     (kept,))
    # a tiny page cache forces the uncommitted pages into the database file,
    # so the journal the crash leaves behind is hot and must be played back
    script = textwrap.dedent("""
        import os, sqlite3, sys
        conn = sqlite3.connect(sys.argv[1], isolation_level=None)
        conn.execute("PRAGMA cache_size = 1")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE projects SET name = 'lost'")
        for i in range(8):
            conn.execute("INSERT INTO projects (id, name, root, is_global) "
                         "VALUES (?, ?, NULL, 0)", (f"{i:010d}", "x" * 100_000))
        os._exit(3)
    """)
    crashed = subprocess.run([sys.executable, "-c", script, str(root / "memriver.db")],
                             check=False)
    assert crashed.returncode == 3
    assert (root / "memriver.db-journal").exists()
    with _db(root).read() as conn:
        assert conn.execute("SELECT id, name FROM projects").fetchall() == [(kept, "kept")]
    assert not (root / "memriver.db-journal").exists()


def test_read_and_write_connections_tolerate_invalid_utf8_instead_of_crashing(tmp_path):
    root = tmp_path / "store"
    with _db(root).write() as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'g', NULL, 1)",
                     (new_id(),))
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        conn.execute("UPDATE projects SET name = CAST(X'80' AS TEXT)")
    # sqlite3's default text_factory raises OperationalError decoding this column;
    # a lenient one hands back bytes instead of failing the whole fetch
    with _db(root).read() as conn:
        assert isinstance(conn.execute("SELECT name FROM projects").fetchone()[0], bytes)
    with _db(root).write() as conn:
        assert isinstance(conn.execute("SELECT name FROM projects").fetchone()[0], bytes)


def test_a_memory_round_trips_through_its_row():
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"}, description="d")
    assert memory_from_row(memory_to_row(memory)) == memory


def test_a_valid_last_read_at_round_trips():
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    memory.last_read_at = now()
    assert memory_from_row(memory_to_row(memory)) == memory


# built with chr(...), never a raw non-ASCII digit in the file: fullwidth 0-9
# are U+FF10..U+FF19, one codepoint above their ASCII counterpart's ordinal
# shifted by the same offset as '0' -> U+FF10
_FULLWIDTH_DIGITS = str.maketrans("0123456789", "".join(chr(0xFF10 + i) for i in range(10)))
_FULLWIDTH_TIMESTAMP = "2026-09-24T00:00:01.000000Z".translate(_FULLWIDTH_DIGITS)


@pytest.mark.parametrize("change", [
    {"id": "../../evil"}, {"project_id": "ABCDEFGHJK"}, {"type": "note"}, {"trust": "high"},
    {"sync": 2}, {"version": 0}, {"body": b"bytes"}, {"deleted_at": 5}, {"last_read_at": 5},
    {"last_read_at": "not-a-timestamp"},
    {"last_read_at": "9999-99-99T99:99:99.999999Z"},          # right shape, no such calendar date
    {"last_read_at": "2026-02-30T00:00:00.000000Z"},          # right shape, February has no 30th
    {"last_read_at": _FULLWIDTH_TIMESTAMP},                    # right shape, not ASCII digits
    {"level": 0}, {"level": True}, {"level": "1"}, {"tags": None}, {"tags": "x"},
    {"tags": '{"a": 1}'}, {"tags": '["Bad"]'}, {"tags": '["a", "a"]'},
    {"tags": '["' + "a" * 33 + '"]'},
])
def test_a_memory_row_memriver_could_not_have_written_is_invalid(change):
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    columns = ["id", "project_id", "type", "source_harness", "source_method", "trust", "sync",
               "description", "body", "created", "updated", "version", "deleted_at",
               "last_read_at", "level", "tags"]
    row = dict(zip(columns, memory_to_row(memory), strict=True))
    row.update(change)
    with pytest.raises(ValueError):
        memory_from_row(tuple(row[c] for c in columns))


@pytest.mark.parametrize("row", [
    ("../../evil", "n", None, 0), (new_id(), "", None, 0), (new_id(), "two\nlines", None, 0),
    (new_id(), "n", "relative/path", 0), (new_id(), "n", "/nul\x00", 0), (new_id(), "n", None, 2),
    (new_id(), "n", "/work/x/../a", 0), (new_id(), "n", "/work/", 0), (new_id(), "n", "//work", 0),
])
def test_a_project_row_memriver_could_not_have_written_is_invalid(row):
    with pytest.raises(ValueError):
        project_from_row(row)


def test_a_valid_project_row_reads_back():
    pid = new_id()
    project, is_global = project_from_row((pid, "demo", "/w", 0))
    assert (project.id, project.name, project.root, is_global) == (pid, "demo", "/w", False)


# --- schema v4 -> v5, in place --------------------------------------------------

# the two tables v5 changes, as schema v4 defined them
_V4_TABLES = (
    """CREATE TABLE memories (
      id             TEXT PRIMARY KEY NOT NULL CHECK (length(id) = 10),
      project_id     TEXT NOT NULL REFERENCES projects(id),
      source_harness TEXT NOT NULL,
      source_method  TEXT NOT NULL,
      created        TEXT NOT NULL,
      version        INTEGER NOT NULL CHECK (version >= 1),
      type           TEXT NOT NULL CHECK (type IN ('user','feedback','project','reference')),
      trust          TEXT NOT NULL CHECK (trust IN ('user','agent','untrusted-derived')),
      sync           INTEGER NOT NULL CHECK (sync IN (0, 1)),
      description    TEXT NOT NULL,
      body           TEXT NOT NULL,
      updated        TEXT NOT NULL,
      deleted_at     TEXT,
      last_read_at   TEXT
    ) STRICT""",
    ("CREATE INDEX memories_active_by_project "
     "ON memories(project_id, updated DESC) WHERE deleted_at IS NULL"),
    """CREATE TABLE memory_versions (
      memory_id    TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      version      INTEGER NOT NULL CHECK (version >= 1),
      type         TEXT NOT NULL,
      trust        TEXT NOT NULL,
      sync         INTEGER NOT NULL CHECK (sync IN (0, 1)),
      description  TEXT NOT NULL,
      body         TEXT NOT NULL,
      deleted      INTEGER NOT NULL CHECK (deleted IN (0, 1)),
      change_id    TEXT REFERENCES changes(change_id),
      PRIMARY KEY (memory_id, version)
    ) STRICT""",
)


def _v4_store(root) -> tuple[str, str]:
    """A store as schema v4 left it, holding global and one memory at two versions;
    (global id, memory id)."""
    with _db(root).write():
        pass
    global_id, memory_id = new_id(), new_id()
    stamp = "2026-10-01T00:00:00.000000Z"
    with closing(sqlite3.connect(root / "memriver.db", isolation_level=None)) as conn:
        for table in ("tags", "memory_versions", "memories"):
            conn.execute(f"DROP TABLE {table}")
        for statement in _V4_TABLES:
            conn.execute(statement)
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'global', "
                     "NULL, 1)", (global_id,))
        conn.execute("INSERT INTO memories (id, project_id, source_harness, source_method, "
                     "created, version, type, trust, sync, description, body, updated) "
                     "VALUES (?, ?, 'h', 'human', ?, 2, 'feedback', 'user', 1, 'cue', "
                     "'second', ?)", (memory_id, global_id, stamp, stamp))
        conn.executemany("INSERT INTO memory_versions (memory_id, version, type, trust, sync, "
                         "description, body, deleted) VALUES (?, ?, 'feedback', 'user', 1, "
                         "'cue', ?, 0)", [(memory_id, 1, "first"), (memory_id, 2, "second")])
        conn.execute("PRAGMA user_version = 4")
    return global_id, memory_id


def _columns(root, table: str) -> list[str]:
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


@pytest.mark.parametrize("opener", ["read", "write"])
def test_a_v4_store_is_upgraded_in_place_by_the_first_read_or_write(tmp_path, opener):
    root = tmp_path / "store"
    _, memory_id = _v4_store(root)
    with getattr(_db(root), opener)() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        rows = conn.execute("SELECT version, level, tags FROM memory_versions "
                            "ORDER BY version").fetchall()
        memory = memory_from_row(conn.execute(
            "SELECT id, project_id, type, source_harness, source_method, trust, sync, "
            "description, body, created, updated, version, deleted_at, last_read_at, level, "
            "tags FROM memories").fetchone())
    assert version == SCHEMA_VERSION == 5
    assert rows == [(1, None, "[]"), (2, None, "[]")]          # no version row written
    assert (memory.id, memory.version, memory.level, memory.tags) == (memory_id, 2, None, ())
    assert _columns(root, "memories")[-2:] == _columns(root, "memory_versions")[-2:] == \
        ["level", "tags"]
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        assert conn.execute("SELECT count(*) FROM tags").fetchone()[0] == 0


def test_an_upgrade_that_fails_part_way_leaves_v4_intact(tmp_path, monkeypatch):
    from memriver_core.repository.sqlite import database
    root = tmp_path / "store"
    _v4_store(root)
    before = (root / "memriver.db").read_bytes()
    monkeypatch.setattr(database, "_UPGRADE", (*database._UPGRADE[:3], "CREATE TABLE ("))
    with pytest.raises(StorageFailure), _db(root).read():
        pass
    assert (root / "memriver.db").read_bytes() == before
    assert "level" not in _columns(root, "memories")


def test_an_upgraded_store_has_the_schema_a_fresh_one_has(tmp_path):
    _v4_store(tmp_path / "old")
    _db(tmp_path / "old").upgrade()
    with _db(tmp_path / "new").write():
        pass
    for table in ("memories", "memory_versions", "tags"):
        assert _columns(tmp_path / "old", table) == _columns(tmp_path / "new", table)
