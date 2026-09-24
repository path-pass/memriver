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
from memriver_core.models import Memory, new_id
from memriver_core.models.errors import StorageFailure
from memriver_core.repository.sqlite import database as database_module
from memriver_core.repository.sqlite.database import (
    MEMORY_COLUMNS,
    Database,
    memory_from_row,
    memory_to_row,
    project_from_row,
    upgrade_if_needed,
)

# the v1 schema, frozen here as a fixture: production code only ever creates
# v2 directly (_SCHEMA) or upgrades from it (_UPGRADE_STATEMENTS)
_V1_SCHEMA = (
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
      deleted_at     TEXT
    ) STRICT""",
    ("CREATE INDEX memories_active_by_project "
     "ON memories(project_id, updated DESC) WHERE deleted_at IS NULL"),
)


def _db(root) -> Database:
    return Database(root, busy_timeout_ms=2000)


def _build_v1(path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        for statement in _V1_SCHEMA:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 1")


def test_a_read_of_a_missing_store_creates_nothing(tmp_path):
    root = tmp_path / "store"
    with _db(root).read() as conn:
        assert conn is None
    assert not root.exists()


def test_the_first_write_creates_a_private_directory_file_and_schema(tmp_path):
    root = tmp_path / "store"
    with _db(root).write() as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'g', NULL, 1)",
                     (new_id(),))
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "memriver.db").st_mode) == 0o600
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"projects", "memories", "sessions"}


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


def test_a_version_one_database_is_upgraded_in_place(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    _build_v1(root / "memriver.db")
    pid = new_id()
    memory = Memory.new(body="b", type="project", project_id=pid,
                        source={"harness": "h", "method": "agent"})
    v1_columns = ("id, project_id, type, source_harness, source_method, trust, sync, "
                  "description, body, created, updated, version, deleted_at")
    with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'p', NULL, 0)",
                     (pid,))
        placeholders = ", ".join("?" for _ in v1_columns.split(","))
        conn.execute(f"INSERT INTO memories ({v1_columns}) VALUES ({placeholders})",
                     memory_to_row(memory)[:-1])   # v1 has no last_read_at column yet
    with _db(root).read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "sessions" in tables
        row = conn.execute(f"SELECT {MEMORY_COLUMNS} FROM memories WHERE id = ?",
                           (memory.id,)).fetchone()
    seen = memory_from_row(row)
    assert seen == memory
    assert seen.last_read_at is None


def test_two_openers_upgrade_a_version_one_database_once(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    _build_v1(root / "memriver.db")
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def opener() -> None:
        try:
            barrier.wait()
            with _db(root).read():
                pass
        except BaseException as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=opener) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    assert "last_read_at" in columns


def test_an_upgrade_failing_part_way_leaves_version_one_intact(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    db_path = root / "memriver.db"
    _build_v1(db_path)
    broken = list(database_module._UPGRADE_STATEMENTS)
    broken[-1] = "CREATE INDEX sessions_by_project ON no_such_table(project_id)"
    monkeypatch.setattr(database_module, "_UPGRADE_STATEMENTS", broken)
    with pytest.raises(StorageFailure):
        upgrade_if_needed(db_path, busy_timeout_ms=2000)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        assert "last_read_at" not in columns
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "sessions" not in tables


def test_upgrade_if_needed_on_a_missing_file_is_a_no_op(tmp_path):
    upgrade_if_needed(tmp_path / "store" / "memriver.db", busy_timeout_ms=2000)
    assert not (tmp_path / "store").exists()


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
            "sync, description, body, created, updated) "
            "VALUES (?, ?, 'user', 'h', 'agent', 'agent', 1, '', 'b', 'c', 'u')",
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


@pytest.mark.parametrize("change", [
    {"id": "../../evil"}, {"project_id": "ABCDEFGHJK"}, {"type": "note"}, {"trust": "high"},
    {"sync": 2}, {"version": 0}, {"body": b"bytes"}, {"deleted_at": 5}, {"last_read_at": 5},
])
def test_a_memory_row_memriver_could_not_have_written_is_invalid(change):
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    columns = ["id", "project_id", "type", "source_harness", "source_method", "trust", "sync",
               "description", "body", "created", "updated", "version", "deleted_at",
               "last_read_at"]
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
