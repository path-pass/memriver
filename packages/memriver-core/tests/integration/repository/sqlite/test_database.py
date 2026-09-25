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
from memriver_core.models import (
    Change,
    ChangeRow,
    DreamRun,
    Memory,
    Review,
    SourceRef,
    is_timestamp,
    new_id,
    now,
)
from memriver_core.models.errors import StorageFailure
from memriver_core.repository.sqlite import database as database_module
from memriver_core.repository.sqlite.database import (
    MEMORY_COLUMNS,
    Database,
    change_from_row,
    change_to_row,
    memory_from_object,
    memory_from_row,
    memory_object,
    memory_to_row,
    project_from_row,
    read_row_check,
    review_from_row,
    review_to_row,
    run_from_row,
    run_to_row,
    set_row_check,
    source_from_row,
    state_row_check,
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"projects", "memories", "sessions", "tool_calls", "memory_source_sets",
                      "memory_sources", "memory_reads", "dream_changes", "dream_reviews",
                      "dream_state", "dream_runs"}


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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"sessions", "tool_calls", "memory_sources", "dream_changes"} <= tables
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"tool_calls_by_recorded_at", "memory_reads_by_memory"} <= indexes
        row = conn.execute(f"SELECT {MEMORY_COLUMNS} FROM memories WHERE id = ?",
                           (memory.id,)).fetchone()
    seen = memory_from_row(row)
    # one upgrade runs both steps: v2 adds the column, v3 starts its TTL clock (D11)
    assert is_timestamp(seen.last_read_at)
    seen.last_read_at = None
    assert seen == memory


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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    assert "last_read_at" in columns


def test_a_read_of_a_v2_database_does_not_queue_behind_a_writers_lock(tmp_path):
    """`upgrade_if_needed` must not take BEGIN IMMEDIATE once the schema is already
    current: that would serialize every read behind any concurrent writer's own
    write transaction, for an upgrade that never has anything to do."""
    root = tmp_path / "store"
    with _db(root).write():
        pass                                # creates the v2 schema
    holder = sqlite3.connect(root / "memriver.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'g', NULL, 1)",
                   (new_id(),))
    try:
        # a busy timeout far shorter than the holder keeps its lock: if the
        # upgrade check took the write lock too, this read would block on it
        # and raise StorageFailure once the timeout elapsed
        with Database(root, busy_timeout_ms=50).read() as conn:
            assert conn is not None
    finally:
        holder.execute("ROLLBACK")
        holder.close()


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
        assert "tool_calls" not in tables


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


def _build_v2(path) -> None:
    """A v2 file: the v1 schema plus the v1 -> v2 step, exactly as an upgrade left it."""
    _build_v1(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        for statement in database_module._UPGRADE_STATEMENTS:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 2")


def _insert_memory(path, memory: Memory) -> None:
    placeholders = ", ".join("?" for _ in MEMORY_COLUMNS.split(","))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"INSERT INTO memories ({MEMORY_COLUMNS}) VALUES ({placeholders})",
                     memory_to_row(memory))


def test_a_version_two_database_is_upgraded_to_three_and_last_read_at_is_set_once(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    path = root / "memriver.db"
    _build_v2(path)
    pid = new_id()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'p', NULL, 0)",
                     (pid,))
    never = Memory.new(body="a", type="project", project_id=pid, source={"harness": "h",
                                                                          "method": "agent"})
    read = Memory.new(body="b", type="project", project_id=pid, source={"harness": "h",
                                                                         "method": "agent"})
    read.last_read_at = "2026-01-01T00:00:00.000000Z"
    deleted = Memory.new(body="c", type="project", project_id=pid, source={"harness": "h",
                                                                            "method": "agent"})
    deleted.deleted_at = now()
    for memory in (never, read, deleted):
        _insert_memory(path, memory)
    with _db(root).read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        session_columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        stamps = dict(conn.execute("SELECT id, last_read_at FROM memories"))
    assert {"summary", "summary_at", "summary_input", "summary_status",
            "summary_attempted_at", "summary_progress"} <= session_columns
    assert stamps[read.id] == "2026-01-01T00:00:00.000000Z"
    # deleted rows too, so an undelete does not revive a row already past its TTL
    assert is_timestamp(stamps[never.id]) and stamps[never.id] == stamps[deleted.id]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE memories SET last_read_at = NULL WHERE id = ?", (never.id,))
    with _db(root).read() as conn:
        assert conn.execute("SELECT last_read_at FROM memories WHERE id = ?",
                            (never.id,)).fetchone()[0] is None     # the upgrade ran once


def test_a_v2_to_v3_upgrade_failing_part_way_leaves_version_two_intact(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    path = root / "memriver.db"
    _build_v2(path)
    broken = list(database_module._V3_STATEMENTS)
    broken[-1] = "CREATE INDEX nothing ON no_such_table(x)"
    monkeypatch.setattr(database_module, "_V3_STATEMENTS", broken)
    with pytest.raises(StorageFailure):
        upgrade_if_needed(path, busy_timeout_ms=2000)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        session_columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "memory_sources" not in tables and "summary" not in session_columns


def _columns(path) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(path)) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: [tuple(r[1:]) for r in conn.execute(f"PRAGMA table_info({table})")]
                for table in tables}


def test_a_fresh_database_and_an_upgraded_one_hold_the_same_columns(tmp_path):
    fresh = tmp_path / "fresh"
    with _db(fresh).write():
        pass
    upgraded = tmp_path / "upgraded"
    upgraded.mkdir()
    _build_v1(upgraded / "memriver.db")
    upgrade_if_needed(upgraded / "memriver.db", busy_timeout_ms=2000)
    assert _columns(fresh / "memriver.db") == _columns(upgraded / "memriver.db")


def _review(**fields) -> Review:
    values = {"memory_id": new_id(), "memory_version": 3, "decided_at": now(),
              "decision": "keep", "reason": "still true", "uncertain_streak": 0,
              "next_review_at": now(), "run_id": "run1", "executor": "claude",
              "prompt_version": "dream-1"}
    return Review(**(values | fields))


def _change(**fields) -> Change:
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    another = Memory.new(body="c", type="project", project_id=memory.project_id,
                         source={"harness": "h", "method": "agent"})
    values = {"change_id": new_id(), "run_id": "run1", "kind": "merge",
              "project_id": memory.project_id, "applied_at": now(),
              "rows": (ChangeRow(new_id(), None, None, 1),
                       ChangeRow(memory.id, memory_object(memory),
                                 (SourceRef(new_id(), 3, memory.project_id,
                                            {"type": "user", "description": "", "body": "b"}),),
                                 memory.version + 1),
                       ChangeRow(another.id, memory_object(another), (), another.version + 1)),
              "reason": "same fact twice", "undone_at": None}
    return Change(**(values | fields))


def test_the_dream_rows_round_trip_through_their_codecs():
    review = _review()
    assert review_from_row(review_to_row(review)) == review
    change = _change()
    assert change_from_row(change_to_row(change)) == change
    memory = Memory.new(body="b", type="user", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    assert memory_from_object(memory_object(memory)) == memory
    derived, source = new_id(), new_id()
    snapshot = '{"type":"user","description":"d","body":"b"}'
    assert source_from_row((derived, 2, source, 1, memory.project_id, snapshot)) == (
        derived, 2, SourceRef(source, 1, memory.project_id,
                              {"type": "user", "description": "d", "body": "b"}))
    read_row_check((memory.id, 1, now(), "codex", None))
    state_row_check(("consolidate:" + memory.project_id, "abc", now()))
    run = DreamRun(run_id=new_id(), started_at=now(), finished_at=None, trigger="manual",
                   executor=None, status="running", report={"secrets": {"done": 0}})
    assert run_from_row(run_to_row(run)) == run


@pytest.mark.parametrize("row_index, value", [
    (0, "short"), (1, "later"), (2, "later"), (3, "cron"), (4, ""), (5, "done"),
    (6, "[]"), (6, "not json"),
])
def test_a_run_row_memriver_could_not_have_written_is_invalid(row_index, value):
    row = list(run_to_row(DreamRun(run_id=new_id(), started_at=now(), finished_at=now(),
                                   trigger="schedule", executor="codex", status="completed",
                                   report={})))
    row[row_index] = value
    with pytest.raises(ValueError):
        run_from_row(tuple(row))


@pytest.mark.parametrize("change", [
    {"decision": "maybe"}, {"memory_version": 0}, {"uncertain_streak": -1},
    {"decided_at": "yesterday"}, {"reason": ""}, {"memory_id": "../../evil"},
])
def test_a_review_row_memriver_could_not_have_written_is_invalid(change):
    with pytest.raises(ValueError):
        review_from_row(review_to_row(_review(**change)))


@pytest.mark.parametrize("row_index, value", [
    (0, "short"), (2, "delete"), (4, "not-a-time"), (5, "[]"), (5, "[{}]"),
    (5, '[{"id":"aaaaaaaaaa","before":null,"before_sources":null,"after_version":0}]'),
    (5, '[{"id":"aaaaaaaaaa","before":{"id":"x"},"before_sources":[],"after_version":1}]'),
    (5, '[{"id":"aaaaaaaaaa","before":null,"before_sources":[],"after_version":1}]'),
    (5, '[{"id":"aaaaaaaaaa","before":null,"after_version":1}]'),    # no before_sources
    (5, ('[{"id":"aaaaaaaaaa","before":null,"before_sources":[{"source_id":"x"}],'
         '"after_version":1}]')),
    (5, '[ {"id":"aaaaaaaaaa","before":null,"before_sources":null,"after_version":1}]'),
    (7, "later"),
])
def test_a_change_row_memriver_could_not_have_written_is_invalid(row_index, value):
    row = list(change_to_row(_change()))
    row[row_index] = value
    with pytest.raises(ValueError):
        change_from_row(tuple(row))


def _one_row_change(project_id: str, rows: tuple[ChangeRow, ...], **fields) -> Change:
    values = {"change_id": new_id(), "run_id": "run1", "kind": "rewrite",
              "project_id": project_id, "applied_at": now(), "rows": rows,
              "reason": "x", "undone_at": None}
    return Change(**(values | fields))


def test_a_change_row_naming_another_memorys_before_image_is_invalid():
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    # the outer id (new_id()) does not match before's own id, though the project agrees
    change = _one_row_change(memory.project_id,
                             (ChangeRow(new_id(), memory_object(memory), (),
                                       memory.version + 1),))
    with pytest.raises(ValueError):
        change_from_row(change_to_row(change))


def test_a_change_row_naming_another_projects_before_image_is_invalid():
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    # the target id agrees with before, but the change's project does not
    change = _one_row_change(new_id(),
                             (ChangeRow(memory.id, memory_object(memory), (),
                                       memory.version + 1),))
    with pytest.raises(ValueError):
        change_from_row(change_to_row(change))


@pytest.mark.parametrize("after_version", [1, 3])
def test_an_updated_rows_after_version_must_follow_its_before_images_version(after_version):
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    change = _one_row_change(memory.project_id,
                             (ChangeRow(memory.id, memory_object(memory), (), after_version),))
    with pytest.raises(ValueError):
        change_from_row(change_to_row(change))


@pytest.mark.parametrize("after_version", [0, 2])
def test_a_created_rows_after_version_must_be_one(after_version):
    change = _one_row_change(new_id(), (ChangeRow(new_id(), None, None, after_version),))
    with pytest.raises(ValueError):
        change_from_row(change_to_row(change))


def test_a_change_naming_the_same_target_twice_is_invalid():
    memory = Memory.new(body="b", type="project", project_id=new_id(),
                        source={"harness": "h", "method": "agent"})
    change = _one_row_change(memory.project_id,
                             (ChangeRow(memory.id, memory_object(memory), (),
                                       memory.version + 1),
                              ChangeRow(memory.id, None, None, 1)))
    with pytest.raises(ValueError):
        change_from_row(change_to_row(change))


@pytest.mark.parametrize("row", [
    ("bad", 1, new_id(), 1, new_id(), '{"type":"user","description":"d","body":"b"}'),
    (new_id(), 0, new_id(), 1, new_id(), '{"type":"user","description":"d","body":"b"}'),
    (new_id(), 1, new_id(), 1, new_id(), '{"type":"user","body":"b"}'),
    (new_id(), 1, new_id(), 1, new_id(), "not json"),
    (new_id(), 1, new_id(), 1, new_id(), '{"type":"invalid","description":"","body":"x"}'),
])
def test_a_source_row_memriver_could_not_have_written_is_invalid(row):
    with pytest.raises(ValueError):
        source_from_row(row)


@pytest.mark.parametrize("row", [("bad", 1), (new_id(), 0), (new_id(), True)])
def test_a_source_set_row_memriver_could_not_have_written_is_invalid(row):
    with pytest.raises(ValueError):
        set_row_check(row)


@pytest.mark.parametrize("row", [
    ("bad", 1, now(), "codex", None), (new_id(), 0, now(), "codex", None),
    (new_id(), 1, "later", "codex", None), (new_id(), 1, now(), "", None),
    (new_id(), 1, now(), "codex", ""), (new_id(), 1, now(), "x" * 65, None),
])
def test_a_read_row_memriver_could_not_have_written_is_invalid(row):
    with pytest.raises(ValueError):
        read_row_check(row)


@pytest.mark.parametrize("row", [("", "f", now()), ("s", "", now()), ("s", "f", "later")])
def test_a_state_row_memriver_could_not_have_written_is_invalid(row):
    with pytest.raises(ValueError):
        state_row_check(row)
