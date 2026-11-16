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


V6_TABLES = {"projects", "memories", "memory_versions", "memory_sources", "changes",
             "change_steps", "memory_reads", "sessions", "tool_calls", "tags",
             "session_moves"}


def test_the_first_write_creates_a_private_directory_file_and_schema(tmp_path):
    root = tmp_path / "store"
    with _db(root).write() as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'g', NULL, 1)",
                     (new_id(),))
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "memriver.db").st_mode) == 0o600
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == V6_TABLES


def test_create_schema_is_the_whole_v6_definition_and_leaves_the_version_alone():
    with closing(sqlite3.connect(":memory:")) as fresh:
        create_schema(fresh)
        assert fresh.execute("PRAGMA user_version").fetchone()[0] == 0
        assert {r[0] for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")} == V6_TABLES
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


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_a_store_below_v5_is_refused_and_left_untouched(tmp_path, version):
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


# --- schema v5 -> v6, in place --------------------------------------------------

def _v5_store(root) -> str:
    """A store as schema v5 left it: no session_moves table, tool_calls without its
    cwd column, one session and one recorded call; the call id."""
    with _db(root).write():
        pass
    with closing(sqlite3.connect(root / "memriver.db", isolation_level=None)) as conn:
        conn.execute("DROP TABLE session_moves")
        conn.execute("ALTER TABLE tool_calls DROP COLUMN cwd")
        conn.execute("INSERT INTO sessions (harness, session_id, status, origin, entry_cwd, "
                     "started_at, last_active_at) "
                     "VALUES ('codex', 's', 'registered', 'start', '/w', 'a', 'b')")
        conn.execute("INSERT INTO tool_calls (harness, call_id, session_id, recorded_at) "
                     "VALUES ('codex', 'c1', 's', 'a')")
        conn.execute("PRAGMA user_version = 5")
    return "c1"


def _columns(root, table: str) -> list[str]:
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _normalised_schema(root) -> dict[str, str]:
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        return {name: " ".join(sql.split()) for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}


def test_fresh_store_is_v6(tmp_path):
    with _db(tmp_path).write():
        pass
    assert SCHEMA_VERSION == 6
    assert _columns(tmp_path, "tool_calls")[-1] == "cwd"
    assert _columns(tmp_path, "session_moves") == [
        "harness", "session_id", "seq", "moved_at", "from_entry_cwd", "from_project_id",
        "to_entry_cwd", "to_project_id"]


@pytest.mark.parametrize("opener", ["read", "write"])
def test_v5_upgraded_on_write(tmp_path, opener):
    call_id = _v5_store(tmp_path)
    with getattr(_db(tmp_path), opener)() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        calls = conn.execute("SELECT call_id, cwd FROM tool_calls").fetchall()
        moves = conn.execute("SELECT count(*) FROM session_moves").fetchone()[0]
    assert version == 6
    assert calls == [(call_id, None)]
    assert moves == 0


def test_v5_upgraded_on_read_equals_fresh_v6(tmp_path):
    _v5_store(tmp_path / "old")
    with _db(tmp_path / "old").read():
        pass
    with _db(tmp_path / "new").write():
        pass
    assert _normalised_schema(tmp_path / "old") == _normalised_schema(tmp_path / "new")
    for table in ("tool_calls", "session_moves"):
        with closing(sqlite3.connect(tmp_path / "old" / "memriver.db")) as old, \
                closing(sqlite3.connect(tmp_path / "new" / "memriver.db")) as new:
            query = f"SELECT * FROM pragma_table_info('{table}')"
            assert old.execute(query).fetchall() == new.execute(query).fetchall()


def test_v5_upgrade_rolls_back_on_failure(tmp_path, monkeypatch):
    from memriver_core.repository.sqlite import database
    _v5_store(tmp_path)
    before = (tmp_path / "memriver.db").read_bytes()
    monkeypatch.setattr(database, "_UPGRADE", (database._UPGRADE[0], "CREATE TABLE ("))
    with pytest.raises(StorageFailure), _db(tmp_path).read():
        pass
    assert (tmp_path / "memriver.db").read_bytes() == before
    with closing(sqlite3.connect(tmp_path / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert "cwd" not in _columns(tmp_path, "tool_calls")
    assert _columns(tmp_path, "session_moves") == []


def test_two_upgraders_upgrade_once(tmp_path, monkeypatch):
    _v5_store(tmp_path)
    executed: list[str] = []
    real_connect = Database._connect

    def traced(self, *args, **kwargs):
        conn = real_connect(self, *args, **kwargs)
        conn.set_trace_callback(executed.append)    # list.append is atomic
        return conn

    monkeypatch.setattr(Database, "_connect", traced)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def upgrade() -> None:
        try:
            db = _db(tmp_path)
            barrier.wait(timeout=10)
            db.upgrade()
        except BaseException as err:  # noqa: BLE001 - reported to the test thread
            errors.append(err)

    threads = [threading.Thread(target=upgrade) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    for ddl in ("ALTER TABLE tool_calls ADD COLUMN cwd", "CREATE TABLE session_moves"):
        assert sum(stmt.startswith(ddl) for stmt in executed) == 1
    with closing(sqlite3.connect(tmp_path / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_v4_store_refused(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        conn.execute("CREATE TABLE projects (id TEXT)")
        conn.execute("PRAGMA user_version = 4")
    for open_store in (_db(root).read, _db(root).write):
        with pytest.raises(StoreNeedsUpgrade) as excinfo, open_store():
            pass
        assert excinfo.value.version == 4


def test_newer_version_unavailable(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        conn.execute("PRAGMA user_version = 7")
    with pytest.raises(StorageFailure), _db(root).read():
        pass
    with pytest.raises(StorageFailure), _db(root).write():
        pass
