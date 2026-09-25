"""Whole-store inspection of the SQLite store: read-only, keeps what reads skip."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from memriver_core.models import Memory, Project, new_id
from memriver_core.repository.sqlite import SqliteProjectStore, SqliteStoreInspector

MEMORY_COLUMNS = ("id, project_id, type, source_harness, source_method, trust, sync, "
                  "description, body, created, updated, version, deleted_at, last_read_at")
SESSION_COLUMNS = ("harness, session_id, status, origin, project_id, candidate_id, "
                   "candidate_root, entry_cwd, branch, transcript_path, started_at, "
                   "last_active_at, ended_at, prompt_count, last_write_prompt_count, "
                   "last_nudge_prompt_count, first_prompt, recent_prompts")

# the v1 schema, frozen here to build a database an upgrade must act on
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


def _build_v1(store: Path) -> None:
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        for statement in _V1_SCHEMA:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 1")


def _plant(store: Path, memory: Memory) -> Memory:
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        conn.execute(
            f"INSERT INTO memories ({MEMORY_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory.id, memory.project_id, memory.type, memory.source["harness"],
             memory.source["method"], memory.trust, int(memory.sync), memory.description,
             memory.body, memory.created, memory.updated, memory.version, memory.deleted_at,
             memory.last_read_at))
    return memory


def _plant_session(store: Path, **overrides: object) -> dict[str, object]:
    """A raw `sessions` row, bypassing every app-level check the store enforces --
    a bare `sqlite3.connect` never turns `PRAGMA foreign_keys` on, so a
    `candidate_id`/`project_id` naming no project plants cleanly, and
    `PRAGMA ignore_check_constraints` lets an otherwise-invalid row past the
    table's own CHECKs."""
    row: dict[str, object] = {
        "harness": "codex", "session_id": "s1", "status": "registered", "origin": "start",
        "project_id": None, "candidate_id": None, "candidate_root": None,
        "entry_cwd": "/tmp/x", "branch": None, "transcript_path": None,
        "started_at": "2026-09-24T00:00:00.000000Z",
        "last_active_at": "2026-09-24T00:00:00.000000Z", "ended_at": None,
        "prompt_count": 0, "last_write_prompt_count": 0, "last_nudge_prompt_count": 0,
        "first_prompt": None, "recent_prompts": "[]",
    }
    row.update(overrides)
    placeholders = ", ".join("?" for _ in SESSION_COLUMNS.split(","))
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(f"INSERT INTO sessions ({SESSION_COLUMNS}) VALUES ({placeholders})",
                     tuple(row.values()))
    return row


def _sql(store: Path, statement: str, *params) -> list[tuple]:
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        return conn.execute(statement, params).fetchall()


@pytest.fixture
def world(tmp_path):
    store, home, work = tmp_path / "store", tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    project_store = SqliteProjectStore(store, home=home, busy_timeout_ms=2000)
    global_id = project_store.ensure_global()
    project = Project.new("demo", max_chars=120)
    project_store.create(project, project_store.plan_root(str(work), None))
    return {"store": store, "work": work, "global": global_id, "project": project.id,
            "project_store": project_store}


def _memory(project_id: str, body: str = "b") -> Memory:
    return Memory.new(body=body, type="project", project_id=project_id,
                      source={"harness": "t", "method": "agent"})


def test_a_missing_store_is_uninitialized_and_nothing_is_created(tmp_path):
    report = SqliteStoreInspector(tmp_path / "none", busy_timeout_ms=2000).inspect()
    assert (report.initialized, report.entries, report.projects, report.findings) == \
        (False, (), (), ())
    assert not (tmp_path / "none").exists()


def test_a_healthy_store_lists_entries_and_projects_with_counts(world):
    kept = _plant(world["store"], _memory(world["project"]))
    gone = _memory(world["project"], "gone")
    gone.deleted_at, gone.version = "2026-09-24T00:00:00.000000Z", 2
    _plant(world["store"], gone)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert report.initialized and report.findings == ()
    assert [e.memory.id for e in report.entries] == [kept.id]
    by_id = {p.id: p for p in report.projects}
    demo = by_id[world["project"]]
    assert (demo.root, demo.root_state, demo.active_memories, demo.deleted_memories) == \
        (str(world["work"].resolve()), "ok", 1, 1)
    assert (by_id[world["global"]].is_global, by_id[world["global"]].root_state) == (True, "unbound")


def test_an_offline_directory_is_a_state_not_a_finding(world):
    world["work"].rmdir()
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert report.findings == ()
    assert {p.id: p.root_state for p in report.projects}[world["project"]] == "missing"


def test_a_re_pointed_directory_is_a_finding(world, tmp_path):
    moved = tmp_path / "moved"
    world["work"].rename(moved)
    world["work"].symlink_to(moved)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert [f.kind for f in report.findings] == ["non-canonical-root"]
    assert report.findings[0].project_id == world["project"]


def test_an_orphan_is_reported_and_kept_out_of_entries(world):
    orphan = _plant(world["store"], _memory(new_id()))
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert [(f.kind, f.memory_id) for f in report.findings] == [("orphan", orphan.id)]
    assert report.entries == ()


def test_two_orphans_one_with_an_undecodable_id_do_not_crash_the_sort(world):
    good_orphan = _plant(world["store"], _memory(new_id()))
    bad_orphan = _plant(world["store"], _memory(new_id()))
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET id = CAST(X'80' AS TEXT) WHERE id = ?",
                     (bad_orphan.id,))
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    orphans = [(f.kind, f.memory_id, f.location_hint) for f in report.findings
               if f.kind == "orphan"]
    assert ("orphan", good_orphan.id, f"memories/{good_orphan.id}") in orphans
    assert ("orphan", None, "memories") in orphans
    assert len(orphans) == 2


def test_an_invalid_row_is_reported_and_kept_out_of_entries(world):
    bad = _plant(world["store"], _memory(world["project"]))
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET trust = 'odd' WHERE id = ?", (bad.id,))
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    # integrity_check may also flag the bypassed CHECK; the row finding is what matters
    assert ("invalid-row", bad.id) in [(f.kind, f.memory_id) for f in report.findings]
    assert report.entries == ()


def test_a_malformed_last_read_at_is_reported_and_kept_out_of_entries(world):
    bad = _plant(world["store"], _memory(world["project"]))
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("UPDATE memories SET last_read_at = 'not-a-timestamp' WHERE id = ?",
                     (bad.id,))
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert ("invalid-row", bad.id) in [(f.kind, f.memory_id) for f in report.findings]
    assert report.entries == ()


def test_a_damaged_deleted_row_is_still_reported(world):
    gone = _memory(world["project"], "gone")
    gone.deleted_at, gone.version = "2026-09-24T00:00:00.000000Z", 2
    _plant(world["store"], gone)
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET trust = 'odd' WHERE id = ?", (gone.id,))
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert ("invalid-row", gone.id) in [(f.kind, f.memory_id) for f in report.findings]
    assert report.entries == ()


def test_an_undecodable_memory_column_is_invalid_row_not_a_crash(world):
    bad = _plant(world["store"], _memory(world["project"]))
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("UPDATE memories SET body = CAST(X'80' AS TEXT) WHERE id = ?", (bad.id,))
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert ("invalid-row", bad.id) in [(f.kind, f.memory_id) for f in report.findings]
    assert report.entries == ()


def test_an_invalid_session_row_is_reported_as_invalid_row(world):
    _plant_session(world["store"], harness="codex", session_id="bad-1", status="odd")
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert ("invalid-row", "sessions/codex/bad-1") in \
        [(f.kind, f.location_hint) for f in report.findings]


def test_a_session_with_a_dangling_candidate_id_is_a_session_orphan_finding(world):
    _plant_session(world["store"], harness="codex", session_id="pending-1", status="pending",
                   origin="first-seen", candidate_id="zzzzzzzzzz", candidate_root="/z")
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert ("session-orphan", "sessions/codex/pending-1") in \
        [(f.kind, f.location_hint) for f in report.findings]
    assert "invalid-row" not in [f.kind for f in report.findings
                                 if f.location_hint == "sessions/codex/pending-1"]


def test_a_session_with_a_dangling_project_id_is_a_session_orphan_finding(world):
    _plant_session(world["store"], harness="claude-code", session_id="reg-1",
                   status="registered", origin="start", project_id="zzzzzzzzzz")
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert ("session-orphan", "sessions/claude-code/reg-1") in \
        [(f.kind, f.location_hint) for f in report.findings]


def test_a_session_with_a_watermark_above_the_prompt_count_is_reported_as_invalid_row(world):
    _plant_session(world["store"], harness="codex", session_id="bad-2", status="registered",
                   origin="start", project_id=world["project"], prompt_count=1,
                   last_write_prompt_count=2)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert ("invalid-row", "sessions/codex/bad-2") in \
        [(f.kind, f.location_hint) for f in report.findings]


def test_a_healthy_session_row_is_not_a_finding(world):
    _plant_session(world["store"], harness="codex", session_id="ok-1", status="registered",
                   origin="start", project_id=world["project"])
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert report.findings == ()


def test_an_undecodable_project_column_is_invalid_row_not_a_crash(world):
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("UPDATE projects SET name = CAST(X'80' AS TEXT) WHERE id = ?",
                     (world["project"],))
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    matches = [f for f in report.findings if f.kind == "invalid-row"
               and f.project_id == world["project"]]
    assert len(matches) == 1
    assert world["project"] not in {p.id for p in report.projects}


def test_two_projects_bound_to_one_directory_by_alias_is_a_finding(world, tmp_path, monkeypatch):
    from memriver_core.repository import directories

    other = tmp_path / "other"
    other.mkdir()
    project = Project.new("other", max_chars=120)
    world["project_store"].create(project, world["project_store"].plan_root(str(other), None))
    monkeypatch.setattr(directories, "same_directory", lambda a, b: True)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    conflicts = [f for f in report.findings if f.kind == "root-conflict"]
    # "demo" sorts before "other", so the pair is filed on the second: "other"
    assert [f.project_id for f in conflicts] == [project.id]


def test_an_already_unverifiable_root_is_not_paired_and_not_duplicated(world, tmp_path,
                                                                       monkeypatch):
    from memriver_core.repository import directories

    second = tmp_path / "second"
    second.mkdir()
    third = tmp_path / "third"
    third.mkdir()
    p2 = Project.new("second", max_chars=120)
    world["project_store"].create(p2, world["project_store"].plan_root(str(second), None))
    p3 = Project.new("third", max_chars=120)
    world["project_store"].create(p3, world["project_store"].plan_root(str(third), None))
    unverifiable_root = str(second.resolve())
    real_root_state = directories.root_state
    real_same_directory = directories.same_directory

    def root_state(root):
        return "unverifiable" if root == unverifiable_root else real_root_state(root)

    def same_directory(a, b):
        return None if unverifiable_root in (a, b) else real_same_directory(a, b)

    monkeypatch.setattr(directories, "root_state", root_state)
    monkeypatch.setattr(directories, "same_directory", same_directory)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    unverifiable = [f for f in report.findings if f.kind == "unverifiable-root"]
    assert [f.project_id for f in unverifiable] == [p2.id]


def test_an_unverifiable_pair_is_filed_once_per_project_not_duplicated(world, tmp_path,
                                                                       monkeypatch):
    from memriver_core.repository import directories

    second = tmp_path / "second"
    second.mkdir()
    third = tmp_path / "third"
    third.mkdir()
    p2 = Project.new("second", max_chars=120)
    world["project_store"].create(p2, world["project_store"].plan_root(str(second), None))
    p3 = Project.new("third", max_chars=120)
    world["project_store"].create(p3, world["project_store"].plan_root(str(third), None))
    # all three pairs among the three bound roots go unverifiable, so each of the
    # three projects turns up in two pairs -- without cross-pair dedupe that is
    # up to six findings, not three
    roots = {str(world["work"].resolve()), str(second.resolve()), str(third.resolve())}
    real_same_directory = directories.same_directory

    def same_directory(a, b):
        return None if {a, b} <= roots else real_same_directory(a, b)

    monkeypatch.setattr(directories, "same_directory", same_directory)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    unverifiable = [f for f in report.findings if f.kind == "unverifiable-root"]
    assert {f.project_id for f in unverifiable} == {world["project"], p2.id, p3.id}
    assert len(unverifiable) == 3


def test_an_unknown_schema_is_one_finding(world):
    _sql(world["store"], "PRAGMA user_version = 9")
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert [f.kind for f in report.findings] == ["unknown-schema"]


def test_doctor_as_the_first_opener_upgrades_and_reports_no_unknown_schema(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    _build_v1(store)
    report = SqliteStoreInspector(store, busy_timeout_ms=2000).inspect()
    assert "unknown-schema" not in [f.kind for f in report.findings]
    with closing(sqlite3.connect(store / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_a_failed_upgrade_is_reported_as_unknown_schema_not_a_crash(tmp_path, monkeypatch):
    from memriver_core.repository.sqlite import database as database_module

    store = tmp_path / "store"
    store.mkdir()
    _build_v1(store)
    broken = list(database_module._UPGRADE_STATEMENTS)
    broken[-1] = "CREATE INDEX sessions_by_project ON no_such_table(project_id)"
    monkeypatch.setattr(database_module, "_UPGRADE_STATEMENTS", broken)
    report = SqliteStoreInspector(store, busy_timeout_ms=2000).inspect()
    assert [f.kind for f in report.findings] == ["unknown-schema"]
    with closing(sqlite3.connect(store / "memriver.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_a_symlinked_database_is_unsafe_and_not_followed(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (tmp_path / "real.db").write_text("")
    (store / "memriver.db").symlink_to(tmp_path / "real.db")
    report = SqliteStoreInspector(store, busy_timeout_ms=2000).inspect()
    assert [f.kind for f in report.findings] == ["unsafe-database"]


@pytest.mark.parametrize("name", ["store.toml", "projects", "memories", "registry", "global"])
def test_a_leftover_file_store_is_legacy_layout(world, name):
    target = world["store"] / name
    if name.endswith(".toml"):
        target.write_text("global_project = \"x\"\n")
    else:
        target.mkdir()
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert [(f.kind, f.location_hint) for f in report.findings] == [("legacy-layout", name)]


def test_only_a_legacy_store_is_degraded_but_not_initialized(tmp_path):
    store = tmp_path / "store"
    (store / "memories").mkdir(parents=True)
    report = SqliteStoreInspector(store, busy_timeout_ms=2000).inspect()
    assert not report.initialized
    assert [f.kind for f in report.findings] == ["legacy-layout"]


def test_one_inspection_reads_one_snapshot(world, monkeypatch):
    """A peer cannot commit between the inspector's queries: one read transaction.

    Under the rollback journal a writer needs every reader gone before it can
    commit, so the peer (with a tiny timeout) is refused while the inspection
    runs, and the report's counts and entries agree.
    """
    from memriver_core.repository.sqlite import inspector as inspector_module

    real_projects = inspector_module.SqliteStoreInspector._projects
    peer: list[str] = []

    def projects_then_a_peer_tries_to_commit(self, conn, findings):
        result = real_projects(self, conn, findings)
        memory = _memory(world["project"], "landed mid-inspection")
        try:
            with closing(sqlite3.connect(world["store"] / "memriver.db", timeout=0.1)) as other, \
                    other:
                other.execute(f"INSERT INTO memories ({MEMORY_COLUMNS}) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (memory.id, memory.project_id, memory.type, "t", "agent",
                               memory.trust, 1, "", memory.body, memory.created,
                               memory.updated, 1, None, None))
            peer.append("committed")
        except sqlite3.OperationalError:
            peer.append("blocked")
        return result

    monkeypatch.setattr(inspector_module.SqliteStoreInspector, "_projects",
                        projects_then_a_peer_tries_to_commit)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    counted = {p.id: p.active_memories for p in report.projects}[world["project"]]
    assert peer == ["blocked"]
    assert counted == len(report.entries) == 0


def test_a_failing_pragma_still_closes_the_connection(world, monkeypatch):
    import sqlite3 as sqlite

    from memriver_core.models.errors import StorageFailure
    from memriver_core.repository.sqlite import inspector as inspector_module

    closed: list[bool] = []
    real_connect = sqlite.connect

    class Failing:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, statement, *args):
            if statement.startswith("PRAGMA query_only"):
                raise sqlite.OperationalError("injected")
            return self._conn.execute(statement, *args)

        def close(self):
            closed.append(True)
            self._conn.close()

    monkeypatch.setattr(inspector_module.sqlite3, "connect",
                        lambda *a, **k: Failing(real_connect(*a, **k)))
    with pytest.raises(StorageFailure):
        SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    # the module-wide patch also wraps upgrade_if_needed's own connection, which
    # closes cleanly (it never touches query_only) before the inspector's own fails
    assert closed == [True, True]


def test_inspection_never_writes(world):
    before = (world["store"] / "memriver.db").read_bytes()
    SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert (world["store"] / "memriver.db").read_bytes() == before


def test_directory_checks_run_after_the_read_transaction(world, monkeypatch):
    """A hung mount under root_state must not hold the read lock: a writer commits meanwhile."""
    from memriver_core.repository import directories

    real_root_state = directories.root_state
    peer: list[str] = []

    def root_state_while_a_peer_commits(root):
        memory = _memory(world["project"], "landed during the directory checks")
        try:
            with closing(sqlite3.connect(world["store"] / "memriver.db", timeout=0.1)) as other, \
                    other:
                other.execute(f"INSERT INTO memories ({MEMORY_COLUMNS}) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (memory.id, memory.project_id, memory.type, "t", "agent",
                               memory.trust, 1, "", memory.body, memory.created,
                               memory.updated, 1, None, None))
            peer.append("committed")
        except sqlite3.OperationalError:
            peer.append("blocked")
        return real_root_state(root)

    monkeypatch.setattr(directories, "root_state", root_state_while_a_peer_commits)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert peer == ["committed"]
    # the report is still the snapshot read before the peer's commit
    assert {p.id: p.active_memories for p in report.projects}[world["project"]] == 0
    assert report.entries == ()


@pytest.mark.parametrize("table, statement, location", [
    ("memory_source_sets",
     "INSERT INTO memory_source_sets VALUES ('not an id', 1)", "memory_source_sets"),
    ("memory_sources",
     "INSERT INTO memory_sources VALUES ('bbbbbbbbbb', 1, 'cccccccccc', 1, 'dddddddddd', 'x')",
     "memory_sources/bbbbbbbbbb"),
    ("memory_reads",
     "INSERT INTO memory_reads VALUES ('bbbbbbbbbb', 1, 'yesterday', 'codex', NULL)",
     "memory_reads/bbbbbbbbbb"),
    ("dream_changes",
     ("INSERT INTO dream_changes VALUES ('bbbbbbbbbb', 'r', 'merge', 'dddddddddd', "
      "'2026-09-25T00:00:00.000000Z', '[]', 'why', NULL)"),
     "dream_changes/bbbbbbbbbb"),
    ("dream_reviews",
     ("INSERT INTO dream_reviews VALUES ('bbbbbbbbbb', 1, 'yesterday', 'keep', 'r', 0, "
      "'2026-09-25T00:00:00.000000Z', 'r', 'claude', 'dream-1')"),
     "dream_reviews/bbbbbbbbbb"),
    ("dream_state", "INSERT INTO dream_state VALUES ('consolidate:x', '', 'later')",
     "dream_state"),
    ("dream_runs",
     ("INSERT INTO dream_runs VALUES ('bbbbbbbbbb', 'later', NULL, 'manual', NULL, "
      "'running', '{}')"),
     "dream_runs/bbbbbbbbbb"),
])
def test_an_invalid_dream_row_is_reported_as_invalid_row(world, table, statement, location):
    # a raw connection has foreign keys off, so a row naming no memory plants cleanly
    _sql(world["store"], statement)
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    found = [(f.kind, f.location_hint) for f in report.findings]
    assert ("invalid-row", location) in found, table


def test_valid_dream_rows_are_not_findings(world):
    _sql(world["store"], "INSERT INTO dream_state VALUES ('consolidate:x', 'abc', "
                         "'2026-09-25T00:00:00.000000Z')")
    report = SqliteStoreInspector(world["store"], busy_timeout_ms=2000).inspect()
    assert report.findings == ()
