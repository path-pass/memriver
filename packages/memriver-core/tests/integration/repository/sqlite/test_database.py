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
from memriver_core.repository.sqlite.database import (
    Database,
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


def test_the_first_write_creates_a_private_directory_file_and_schema(tmp_path):
    root = tmp_path / "store"
    with _db(root).write() as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'g', NULL, 1)",
                     (new_id(),))
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "memriver.db").st_mode) == 0o600
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"projects", "memories"}


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

def test_a_memory_round_trips_through_its_row():
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"}, description="d")
    assert memory_from_row(memory_to_row(memory)) == memory


@pytest.mark.parametrize("change", [
    {"id": "../../evil"}, {"project_id": "ABCDEFGHJK"}, {"type": "note"}, {"trust": "high"},
    {"sync": 2}, {"version": 0}, {"body": b"bytes"}, {"deleted_at": 5},
])
def test_a_memory_row_memriver_could_not_have_written_is_invalid(change):
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    columns = ["id", "project_id", "type", "source_harness", "source_method", "trust", "sync",
               "description", "body", "created", "updated", "version", "deleted_at"]
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
