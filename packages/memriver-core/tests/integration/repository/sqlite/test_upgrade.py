"""`upgrade_store`: the offline rebuild of a v1, v2 or v3 store as schema v4 (spec §9)."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
from contextlib import closing
from pathlib import Path

import pytest
from memriver_core import bootstrap
from memriver_core.bootstrap import UpgradeResult, build_services
from memriver_core.models import Memory
from memriver_core.models.changes import MemoryVersion, Usage
from memriver_core.models.errors import (
    StorageFailure,
    StoreNeedsUpgrade,
    UpgradeRefused,
)
from memriver_core.repository.sqlite import upgrade as upgrade_module
from memriver_core.repository.sqlite.database import Database
from memriver_core.settings import Settings

# --- the old schemas, frozen here as fixtures --------------------------------

# v1: main's own test fixture (tests/integration/repository/sqlite/test_database.py)
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

# v2: main's `_SCHEMA` (repository/sqlite/database.py at 49625bf), verbatim
_V2_SESSIONS_TABLE = """CREATE TABLE sessions (
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
_V2_SCHEMA = (
    _V1_SCHEMA[0],
    _V1_SCHEMA[1],
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
    _V1_SCHEMA[3],
    _V2_SESSIONS_TABLE,
    "CREATE INDEX sessions_by_project ON sessions(project_id, last_active_at DESC)",
    """CREATE TABLE tool_calls (
  harness     TEXT NOT NULL CHECK (harness IN ('claude-code','codex')),
  call_id     TEXT NOT NULL CHECK (length(call_id) BETWEEN 1 AND 256),
  session_id  TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (harness, call_id)
) STRICT""",
    "CREATE INDEX tool_calls_by_recorded_at ON tool_calls(recorded_at)",
)

# v3: b3566f0's `_V3_STATEMENTS`, run after `_SCHEMA` exactly as its fresh create did
_V3_STATEMENTS = (
    "ALTER TABLE sessions ADD COLUMN summary TEXT",
    "ALTER TABLE sessions ADD COLUMN summary_at TEXT",
    "ALTER TABLE sessions ADD COLUMN summary_input TEXT",
    ("ALTER TABLE sessions ADD COLUMN summary_status TEXT "
     "CHECK (summary_status IN ('ok','empty','omitted','failed'))"),
    "ALTER TABLE sessions ADD COLUMN summary_attempted_at TEXT",
    "ALTER TABLE sessions ADD COLUMN summary_progress TEXT",
    """CREATE TABLE memory_source_sets (
      derived_id       TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      derived_version  INTEGER NOT NULL CHECK (derived_version >= 1),
      PRIMARY KEY (derived_id, derived_version)
    ) STRICT""",
    """CREATE TABLE memory_sources (
      derived_id       TEXT NOT NULL,
      derived_version  INTEGER NOT NULL CHECK (derived_version >= 1),
      source_id        TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
      source_version   INTEGER NOT NULL CHECK (source_version >= 1),
      source_project   TEXT NOT NULL,
      snapshot         TEXT NOT NULL,
      PRIMARY KEY (derived_id, derived_version, source_id),
      FOREIGN KEY (derived_id, derived_version)
        REFERENCES memory_source_sets(derived_id, derived_version) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE memory_reads (
      memory_id      TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      memory_version INTEGER NOT NULL CHECK (memory_version >= 1),
      read_at        TEXT NOT NULL,
      harness        TEXT NOT NULL CHECK (length(harness) BETWEEN 1 AND 64),
      session_id     TEXT CHECK (session_id IS NULL OR length(session_id) BETWEEN 1 AND 128)
    ) STRICT""",
    "CREATE INDEX memory_reads_by_memory ON memory_reads(memory_id, read_at)",
    """CREATE TABLE dream_changes (
      change_id   TEXT PRIMARY KEY NOT NULL CHECK (length(change_id) = 10),
      run_id      TEXT NOT NULL,
      kind        TEXT NOT NULL
                  CHECK (kind IN ('merge','rewrite','extract','retire','secret','unsafe')),
      project_id  TEXT NOT NULL,
      applied_at  TEXT NOT NULL,
      rows        TEXT NOT NULL,
      reason      TEXT NOT NULL,
      undone_at   TEXT
    ) STRICT""",
    """CREATE TABLE dream_reviews (
      memory_id       TEXT PRIMARY KEY NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      memory_version  INTEGER NOT NULL,
      decided_at      TEXT NOT NULL,
      decision        TEXT NOT NULL CHECK (decision IN ('keep','delete','uncertain')),
      reason          TEXT NOT NULL,
      uncertain_streak INTEGER NOT NULL DEFAULT 0 CHECK (uncertain_streak >= 0),
      next_review_at  TEXT NOT NULL,
      run_id          TEXT NOT NULL,
      executor        TEXT NOT NULL,
      prompt_version  TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE dream_state (
      scope        TEXT PRIMARY KEY NOT NULL,
      fingerprint  TEXT NOT NULL,
      processed_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE dream_runs (
      run_id       TEXT PRIMARY KEY NOT NULL CHECK (length(run_id) = 10),
      started_at   TEXT NOT NULL,
      finished_at  TEXT,
      trigger      TEXT NOT NULL CHECK (trigger IN ('schedule','manual')),
      executor     TEXT,
      status       TEXT NOT NULL CHECK (status IN ('running','completed','failed','skipped')),
      report       TEXT NOT NULL DEFAULT '{}'
    ) STRICT""",
)
_SCHEMAS = {1: _V1_SCHEMA, 2: _V2_SCHEMA, 3: (*_V2_SCHEMA, *_V3_STATEMENTS)}

# --- seed rows ---------------------------------------------------------------

GLOBAL, PROJECT = "gggggggggg", "pppppppppp"
KEPT, GLOBAL_KEPT, DELETED = "aaaaaaaaaa", "bbbbbbbbbb", "dddddddddd"
T0, T1, T2 = ("2026-09-20T10:00:00.000000Z", "2026-09-21T11:30:00.000000Z",
              "2026-09-22T09:15:00.000000Z")
READ_1, READ_2 = "2026-09-23T08:00:00.000000Z", "2026-09-24T08:00:00.000000Z"
# kept exactly: spacing, a zero-width space (as an escape) and text today's
# content policy refuses -- the import neither normalizes nor checks
KEPT_DESCRIPTION = "café  notes  "
KEPT_BODY = "line one\n  line two\u200b\naws key AKIA" + "A" * 16

PROJECTS = [(GLOBAL, "global", None, 1), (PROJECT, "app", "/work/app", 0)]
MEMORY_COLUMNS_V2 = ("id, project_id, type, source_harness, source_method, trust, sync, "
                     "description, body, created, updated, version, deleted_at, last_read_at")
MEMORIES = [   # v2/v3 rows; v1 has no last_read_at column
    (KEPT, PROJECT, "project", "codex", "agent", "untrusted-derived", 0, KEPT_DESCRIPTION,
     KEPT_BODY, T0, T1, 7, None, READ_2),
    (GLOBAL_KEPT, GLOBAL, "user", "claude-code", "human", "user", 1, "Prefers short answers",
     "Answer briefly.", T0, T0, 1, None, None),
    (DELETED, PROJECT, "reference", "claude-code", "agent", "agent", 1, "gone", "old", T0, T1,
     2, T1, READ_1),
]
SESSION_COLUMNS_V2 = ("harness, session_id, status, origin, project_id, candidate_id, "
                      "candidate_root, entry_cwd, branch, transcript_path, started_at, "
                      "last_active_at, ended_at, prompt_count, last_write_prompt_count, "
                      "last_nudge_prompt_count, first_prompt, recent_prompts")
SESSIONS = [
    ("claude-code", "session-1", "registered", "start", PROJECT, None, None, "/work/app",
     "main", "/t/session-1.jsonl", T0, T1, None, 3, 2, 0, None, "[]"),
    ("codex", "session-2", "pending", "first-seen", None, PROJECT, "/work/app",
     "/work/app", None, None, T0, T0, None, 0, 0, 0, None, "[]"),
]
TOOL_CALLS = [("claude-code", "toolu_1", "session-1", T1)]
SUMMARY = "Fixed the flaky test."
# v3: session-1 published a summary; session-2's failed outcome stamped summary_at alone
V3_SUMMARIES = [("session-1", SUMMARY, T2, '{"complete":true}', "ok", T2),
                ("session-2", None, T2, '{"complete":false}', "failed", T2)]
V3_READS = [(KEPT, 7, READ_1, "claude-code", "session-1"), (KEPT, 7, READ_2, "codex", None),
            (DELETED, 2, READ_1, "codex", None)]


def _marks(columns: str) -> str:
    return ", ".join("?" for _ in columns.split(","))


def _seed(conn: sqlite3.Connection, version: int) -> None:
    conn.executemany("INSERT INTO projects (id, name, root, is_global) VALUES (?, ?, ?, ?)",
                     PROJECTS)
    if version == 1:
        columns = MEMORY_COLUMNS_V2.removesuffix(", last_read_at")
        rows = [row[:-1] for row in MEMORIES]
    else:
        columns, rows = MEMORY_COLUMNS_V2, MEMORIES
    conn.executemany(f"INSERT INTO memories ({columns}) VALUES ({_marks(columns)})", rows)
    if version >= 2:
        conn.executemany(f"INSERT INTO sessions ({SESSION_COLUMNS_V2}) "
                         f"VALUES ({_marks(SESSION_COLUMNS_V2)})", SESSIONS)
        conn.executemany("INSERT INTO tool_calls VALUES (?, ?, ?, ?)", TOOL_CALLS)
    if version >= 3:
        for session_id, summary, at, summary_input, status, attempted in V3_SUMMARIES:
            conn.execute("UPDATE sessions SET summary = ?, summary_at = ?, summary_input = ?, "
                         "summary_status = ?, summary_attempted_at = ? WHERE session_id = ?",
                         (summary, at, summary_input, status, attempted, session_id))
        conn.executemany("INSERT INTO memory_reads VALUES (?, ?, ?, ?, ?)", V3_READS)
        # dream's v3 trial data: none of it may reach v4 (R1)
        conn.execute("INSERT INTO dream_runs VALUES ('rrrrrrrrrr', ?, ?, 'manual', 'codex', "
                     "'completed', '{}')", (T0, T1))
        conn.execute("INSERT INTO dream_changes VALUES ('cccccccccc', 'rrrrrrrrrr', 'merge', "
                     "?, ?, '[]', 'merged', NULL)", (PROJECT, T1))
        conn.execute("INSERT INTO dream_reviews VALUES (?, 7, ?, 'keep', 'used', 0, ?, "
                     "'rrrrrrrrrr', 'codex', 'dream-2')", (KEPT, T1, T2))
        conn.execute("INSERT INTO dream_state VALUES (?, 'f', ?)", (f"project:{PROJECT}", T1))
        conn.execute("INSERT INTO memory_source_sets VALUES (?, 7)", (KEPT,))
        conn.execute("INSERT INTO memory_sources VALUES (?, 7, ?, 1, ?, "
                     "'{\"type\":\"user\",\"description\":\"d\",\"body\":\"b\"}')",
                     (KEPT, GLOBAL_KEPT, GLOBAL))


def _make_store(root: Path, version: int) -> Path:
    root.mkdir(mode=0o700)
    path = root / "memriver.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        for statement in _SCHEMAS[version]:
            conn.execute(statement)
        _seed(conn, version)
        conn.execute(f"PRAGMA user_version = {version}")
    os.chmod(path, 0o600)
    return path


def _make_v4_store(root: Path) -> Path:
    with Database(root, busy_timeout_ms=2000).write() as conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, 'g', NULL, 1)",
                     (GLOBAL,))
    return root / "memriver.db"


def _query(path: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def _schema(path: Path) -> list[tuple]:
    return _query(path, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")


def _services(root: Path, home: Path):
    return build_services(Settings(root=root), root=root, home=home)


def _expected_memories(version: int) -> list[tuple]:
    """The active rows, as they must read back: version 1, everything else unchanged."""
    return sorted((*row[:11], 1, None, row[13] if version >= 2 else None)
                  for row in MEMORIES if row[12] is None)


def _assert_untouched(path: Path, before: bytes, version: int) -> None:
    """The live file is the old store, byte for byte, and still a working one."""
    assert path.read_bytes() == before
    assert _query(path, "PRAGMA integrity_check") == [("ok",)]
    assert _query(path, "PRAGMA user_version") == [(version,)]
    assert not (path.parent / "memriver.db.upgrade").exists()
    assert not (path.parent / "memriver.db.upgrade-journal").exists()


# --- the import rules (§10 item 13) ------------------------------------------

@pytest.mark.parametrize("version", [1, 2, 3])
def test_active_memories_are_imported_exactly_at_version_one(tmp_path, version):
    # §10 item 13: ids, times, text, trust, sync, source fields exact; soft-deleted excluded
    root = tmp_path / "store"
    path = _make_store(root, version)
    result = bootstrap.upgrade_store(root)
    assert result == UpgradeResult(from_version=version, imported=2, dropped_deleted=1,
                                   reads_kept=2 if version == 3 else 0,
                                   backup_path=root / "memriver.db.v3-backup")
    assert _query(path, f"SELECT {MEMORY_COLUMNS_V2} FROM memories ORDER BY id") \
        == _expected_memories(version)


def test_the_services_read_the_imported_memories_as_they_were(tmp_path):
    # §10 item 13, through the public surface: the same fields, version 1, no history
    root = tmp_path / "store"
    _make_store(root, 3)
    bootstrap.upgrade_store(root)
    services = _services(root, tmp_path)
    assert sorted(services.memory.memories(), key=lambda memory: memory.id) == [
        Memory(id=KEPT, project_id=PROJECT, type="project",
               source={"harness": "codex", "method": "agent"}, trust="untrusted-derived",
               sync=False, created=T0, updated=T1, description=KEPT_DESCRIPTION,
               body=KEPT_BODY, version=1, deleted_at=None, last_read_at=READ_2),
        Memory(id=GLOBAL_KEPT, project_id=GLOBAL, type="user",
               source={"harness": "claude-code", "method": "human"}, trust="user", sync=True,
               created=T0, updated=T0, description="Prefers short answers",
               body="Answer briefly.", version=1, deleted_at=None, last_read_at=None),
    ]


def test_imported_versions_belong_to_no_change_so_the_migration_cannot_be_undone(tmp_path):
    # §10 item 13: imported versions have no change and cannot be undone
    root = tmp_path / "store"
    path = _make_store(root, 3)
    bootstrap.upgrade_store(root)
    assert _query(path, "SELECT memory_id, version, type, trust, sync, description, body, "
                        "deleted, change_id FROM memory_versions ORDER BY memory_id") == [
        (KEPT, 1, "project", "untrusted-derived", 0, KEPT_DESCRIPTION, KEPT_BODY, 0, None),
        (GLOBAL_KEPT, 1, "user", "user", 1, "Prefers short answers", "Answer briefly.", 0,
         None),
    ]
    for table in ("changes", "change_steps", "memory_sources"):
        assert _query(path, f"SELECT count(*) FROM {table}") == [(0,)]
    services = _services(root, tmp_path)
    assert services.memory.versions(KEPT) == [
        MemoryVersion(memory_id=KEPT, version=1, type="project", trust="untrusted-derived",
                      sync=False, description=KEPT_DESCRIPTION, body=KEPT_BODY, deleted=False,
                      sources=(), change=None)]


def test_reads_of_imported_memories_are_kept_at_version_one(tmp_path):
    # §10 item 13: reads at version 1; the soft-deleted memory's reads excluded
    root = tmp_path / "store"
    path = _make_store(root, 3)
    bootstrap.upgrade_store(root)
    assert _query(path, "SELECT memory_id, memory_version, read_at, harness, session_id "
                        "FROM memory_reads ORDER BY read_at") == [
        (KEPT, 1, READ_1, "claude-code", "session-1"), (KEPT, 1, READ_2, "codex", None)]
    assert _services(root, tmp_path).memory.usage([KEPT]) == {
        KEPT: Usage(reads=2, last_read_at=READ_2)}


@pytest.mark.parametrize("version", [1, 2, 3])
def test_projects_sessions_tool_calls_and_published_summaries_are_kept(tmp_path, version):
    # §10 item 13: sessions, tool calls and published summaries intact
    root = tmp_path / "store"
    path = _make_store(root, version)
    bootstrap.upgrade_store(root)
    assert _query(path, "SELECT id, name, root, is_global FROM projects ORDER BY id") \
        == sorted(PROJECTS)
    sessions = _query(path, f"SELECT {SESSION_COLUMNS_V2}, summary, summary_at FROM sessions "
                            "ORDER BY session_id")
    tool_calls = _query(path, "SELECT harness, call_id, session_id, recorded_at FROM tool_calls")
    if version == 1:
        assert (sessions, tool_calls) == ([], [])
        return
    # summary_at only where summary is set: session-2's v3 stamp was not a publication
    summaries = [(SUMMARY, T2), (None, None)] if version == 3 else [(None, None)] * 2
    assert sessions == [(*row, *summary) for row, summary in zip(SESSIONS, summaries,
                                                                 strict=True)]
    assert tool_calls == TOOL_CALLS


@pytest.mark.parametrize("version", [1, 2, 3])
def test_the_rebuilt_schema_is_identical_to_a_fresh_store(tmp_path, version):
    # §10 item 13: migrated and fresh schemas identical (no dream table, no production
    # column, no v3 source relation survives); an old binary refuses v4
    root = tmp_path / "store"
    path = _make_store(root, version)
    bootstrap.upgrade_store(root)
    fresh = _make_v4_store(tmp_path / "fresh")
    assert _schema(path) == _schema(fresh)
    # an older binary opens only the versions it knows (main: 1 and 2; the v3
    # build: 1 to 3) and refuses any other with StorageFailure, so 4 locks it out
    assert _query(path, "PRAGMA user_version") == _query(fresh, "PRAGMA user_version") == [(4,)]


def test_below_v4_the_services_refuse_until_upgraded(tmp_path):
    # §10 item 13 (core part): an entry point refuses a v3 store with the upgrade hint
    root = tmp_path / "store"
    _make_store(root, 3)
    with pytest.raises(StoreNeedsUpgrade) as refused:
        _services(root, tmp_path).memory.memories()
    assert refused.value.version == 3
    bootstrap.upgrade_store(root)
    assert {memory.id for memory in _services(root, tmp_path).memory.memories()} \
        == {KEPT, GLOBAL_KEPT}


# --- publishing: backup, one replace, idempotence ----------------------------

def test_the_backup_is_a_byte_copy_of_the_old_file_and_every_file_stays_private(tmp_path):
    root = tmp_path / "store"
    path = _make_store(root, 3)
    before = path.read_bytes()
    result = bootstrap.upgrade_store(root)
    assert result.backup_path.read_bytes() == before
    assert stat.S_IMODE(os.stat(result.backup_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not (root / "memriver.db.upgrade").exists()


def test_a_killed_upgrades_leftover_new_file_is_discarded(tmp_path):
    root = tmp_path / "store"
    _make_store(root, 3)
    (root / "memriver.db.upgrade").write_bytes(b"half a file")
    (root / "memriver.db.upgrade-journal").write_bytes(b"a stale journal")
    assert bootstrap.upgrade_store(root).imported == 2
    assert not (root / "memriver.db.upgrade-journal").exists()


def test_an_upgrade_of_a_v4_store_does_nothing(tmp_path):
    root = tmp_path / "store"
    path = _make_v4_store(root)
    before = path.read_bytes()
    assert bootstrap.upgrade_store(root) == UpgradeResult(4, 0, 0, 0, None)
    assert path.read_bytes() == before
    assert not (root / "memriver.db.v3-backup").exists()


def test_no_store_is_nothing_to_rebuild_and_creates_nothing(tmp_path):
    missing = tmp_path / "missing"
    assert bootstrap.upgrade_store(missing) == UpgradeResult(None, 0, 0, 0, None)
    assert not missing.exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "memriver.db").touch()        # created by a first write that never committed
    assert bootstrap.upgrade_store(empty) == UpgradeResult(None, 0, 0, 0, None)
    assert (empty / "memriver.db").read_bytes() == b""


def test_an_unknown_schema_version_is_refused_and_left_alone(tmp_path):
    root = tmp_path / "store"
    path = _make_store(root, 3)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 7")
    before = path.read_bytes()
    with pytest.raises(StorageFailure):
        bootstrap.upgrade_store(root)
    _assert_untouched(path, before, 7)


# --- failures before the replace (§10 item 13) -------------------------------

def _fail_after_the_backup_copy(monkeypatch):
    real = upgrade_module._copy_backup

    def copy_then_fail(live, backup):
        real(live, backup)
        raise OSError("injected after the backup copy")
    monkeypatch.setattr(upgrade_module, "_copy_backup", copy_then_fail)


def _fail_just_before_the_replace(monkeypatch):
    def refuse(source, target):
        raise OSError("injected at the replace")
    monkeypatch.setattr(upgrade_module.os, "replace", refuse)


@pytest.mark.parametrize("version", [1, 3])
@pytest.mark.parametrize("inject", [_fail_after_the_backup_copy, _fail_just_before_the_replace])
def test_a_failure_before_the_replace_leaves_the_live_store_working(tmp_path, monkeypatch,
                                                                    version, inject):
    # §10 item 13: a failure between the backup copy and the replace, and just before
    # the replace, leaves the live path a working old store (a v1 one never altered)
    root = tmp_path / "store"
    path = _make_store(root, version)
    before = path.read_bytes()
    inject(monkeypatch)
    with pytest.raises(StorageFailure):
        bootstrap.upgrade_store(root)
    _assert_untouched(path, before, version)
    monkeypatch.undo()
    assert bootstrap.upgrade_store(root).from_version == version   # the retry succeeds


def _drop_the_tool_calls_copy(statements):
    return [statement for statement in statements if "tool_calls" not in statement]


def _break_a_row_after_its_version(statements):
    return [*statements, "UPDATE main.memories SET body = body || '.'"]


def _add_a_foreign_table(statements):
    return [*statements, "CREATE TABLE main.leftover (x INTEGER) STRICT"]


@pytest.mark.parametrize(("corrupt", "reason"), [
    (_drop_the_tool_calls_copy, "counts"),
    (_break_a_row_after_its_version, "invariant"),
    (_add_a_foreign_table, "schema"),
])
def test_a_rebuild_failing_verification_publishes_nothing(tmp_path, monkeypatch, corrupt,
                                                          reason):
    # §10 item 13: a failure leaves the old file
    root = tmp_path / "store"
    path = _make_store(root, 3)
    before = path.read_bytes()
    real = upgrade_module._imports
    monkeypatch.setattr(upgrade_module, "_imports", lambda version: corrupt(real(version)))
    with pytest.raises(UpgradeRefused) as refused:
        bootstrap.upgrade_store(root)
    assert refused.value.reason == reason
    _assert_untouched(path, before, 3)


def test_a_dangling_foreign_key_in_the_old_store_publishes_nothing(tmp_path):
    root = tmp_path / "store"
    path = _make_store(root, 3)
    with closing(sqlite3.connect(path)) as conn, conn:     # foreign keys off: raw damage
        conn.execute("UPDATE sessions SET project_id = 'zzzzzzzzzz' "
                     "WHERE session_id = 'session-1'")
    before = path.read_bytes()
    with pytest.raises(UpgradeRefused) as refused:
        bootstrap.upgrade_store(root)
    assert refused.value.reason == "foreign-keys"
    _assert_untouched(path, before, 3)


# --- locks and concurrency (§10 item 13) --------------------------------------

def test_a_held_upgrade_lock_refuses_and_changes_nothing(tmp_path):
    root = tmp_path / "store"
    path = _make_store(root, 3)
    before = path.read_bytes()
    fd = os.open(root / ".upgrade.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(UpgradeRefused) as refused:
            bootstrap.upgrade_store(root)
    finally:
        os.close(fd)
    assert refused.value.reason == "upgrade-running"
    _assert_untouched(path, before, 3)
    assert not (root / "memriver.db.v3-backup").exists()


def test_a_second_upgrade_during_the_first_exits_and_a_later_write_survives(tmp_path,
                                                                            monkeypatch):
    # §10 item 13: two concurrent upgrades -- the second exits without replacing, so a
    # write made after the first publish survives
    root = tmp_path / "store"
    path = _make_store(root, 3)
    real = upgrade_module._copy_backup
    seen: list[str] = []

    def second_upgrade_meanwhile(live, backup):
        with pytest.raises(UpgradeRefused) as refused:
            bootstrap.upgrade_store(root)
        seen.append(refused.value.reason)
        real(live, backup)
    monkeypatch.setattr(upgrade_module, "_copy_backup", second_upgrade_meanwhile)
    assert bootstrap.upgrade_store(root).from_version == 3
    assert seen == ["upgrade-running"]
    monkeypatch.undo()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO projects (id, name, root, is_global) "
                     "VALUES ('qqqqqqqqqq', 'later', '/work/later', 0)")
    backup = (root / "memriver.db.v3-backup").read_bytes()
    assert bootstrap.upgrade_store(root) == UpgradeResult(4, 0, 0, 0, None)
    assert _query(path, "SELECT name FROM projects WHERE id = 'qqqqqqqqqq'") == [("later",)]
    assert (root / "memriver.db.v3-backup").read_bytes() == backup


def test_the_upgrade_path_imports_no_plugin(tmp_path):
    # core knows no plug-in: it imports none and touches nothing of one (the
    # umbrella takes the plug-in's run lock before calling upgrade_store)
    root = tmp_path / "store"
    _make_store(root, 3)
    code = textwrap.dedent(f"""
        import sys
        from pathlib import Path
        from memriver_core import bootstrap
        assert bootstrap.upgrade_store(Path({str(root)!r})).from_version == 3
        print(sorted(name for name in sys.modules
                     if name.split(".")[0] in ("memriver", "memriver_dream")))
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          check=True)
    assert done.stdout.strip() == "[]"
    assert not (root / "dream").exists()
