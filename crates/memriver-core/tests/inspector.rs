//! The whole-store inspection: it reads every row, keeps what the serving reads skip,
//! names each with a fixed reason, and writes nothing. Each store here is made by the
//! services and then damaged by a connection that is not memriver's.

mod common;

use std::fs;
use std::os::unix::fs::symlink;
use std::path::Path;

use common::{Env, body, draft, setup};
use memriver_core::models::diagnostics::{RootState, StoreReport};
use memriver_core::models::errors::CoreError;
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::repository::sqlite::inspector::StoreInspector;
use rusqlite::types::Value;

const AT: &str = "2026-09-24T00:00:00.000000Z";

/// The fixed words of each kind the inspector reports: its reason, and what the
/// diagnosis suggests.
fn words(kind: &str) -> (&'static str, &'static str) {
    let backup = "restore the database from a backup";
    match kind {
        "unknown-schema" => (
            "database schema is not one this version knows",
            "restore the database from a backup made by this memriver version",
        ),
        "unsafe-database" => (
            "memriver.db is a symlink or not a regular file; it is not followed",
            "replace it with the real database file",
        ),
        "integrity" => ("SQLite integrity check failed", backup),
        "orphan" => (
            "memory belongs to a project that does not exist",
            "restore the missing project from a backup; until then the memory stays hidden",
        ),
        "invalid-row" => (
            "row holds a value memriver could not have written",
            "fix or remove the row",
        ),
        "non-canonical-root" => (
            "bound directory is no longer a canonical path",
            "bind the real directory with memriver project unbind and adopt",
        ),
        "legacy-layout" => (
            "file-store layout from an earlier version; this version does not read it",
            "migrate it, or remove it once migrated",
        ),
        "session-orphan" => (
            "session refers to a project that does not exist",
            "restore the missing project from a backup; until then the session's project is \
             unreachable",
        ),
        "session-move-damaged" => ("session move record is damaged or names no session", backup),
        "version-gap" => (
            "stored versions of this memory are not numbered 1 to its current version",
            backup,
        ),
        "version-mismatch" => ("memory row differs from its latest stored version", backup),
        "unrecorded-version" => ("a version names a change that holds no step for it", backup),
        "dangling-source" => (
            "a version cites a memory version that is not stored",
            backup,
        ),
        "source-cycle" => ("this memory's sources lead back to itself", backup),
        other => panic!("no words for {other}"),
    }
}

/// One finding as the diagnosis gives it: kind, where, the memory, the project, the
/// reason and the suggestion.
type Found = (String, String, Vec<String>, Vec<String>, String, String);

fn finding(kind: &str, location: &str, memory: Option<&str>, project: Option<&str>) -> Found {
    let (reason, suggestion) = words(kind);
    let ids = |id: Option<&str>| id.map(str::to_string).into_iter().collect();
    (
        kind.to_string(),
        location.to_string(),
        ids(memory),
        ids(project),
        reason.to_string(),
        suggestion.to_string(),
    )
}

fn inspect(env: &Env) -> StoreReport {
    StoreInspector::with_busy_timeout(&env.store, 2000)
        .inspect()
        .unwrap()
}

/// What the diagnosis of the store finds, and its incomplete changes. Nothing written a
/// moment ago is stale, and no two bodies of these tests are alike, so every finding is
/// one of the inspector's, with its suggestion.
fn diagnosed(env: &Env) -> (Vec<Found>, Vec<String>) {
    let report = env.services().maintenance.diagnose(None, 90).unwrap();
    let found = report
        .findings
        .into_iter()
        .map(|found| {
            let [location] = &found.location_hints[..] else {
                panic!("one location: {found:?}")
            };
            (
                found.kind,
                location.clone(),
                found.memory_ids,
                found.project_ids,
                found.reason,
                found.suggestion,
            )
        })
        .collect();
    (found, report.incomplete_changes)
}

fn found(env: &Env) -> Vec<Found> {
    diagnosed(env).0
}

fn kinds(env: &Env) -> Vec<(String, Option<String>)> {
    inspect(env)
        .findings
        .into_iter()
        .map(|found| (found.kind, found.memory_id))
        .collect()
}

fn memory(env: &Env, text: &str) -> String {
    env.manage(draft(&env.project.id, text).op())
}

/// A statement of someone who is not memriver, with no key and no CHECK enforced.
fn damage(env: &Env, sql: &str, bound: impl rusqlite::Params) {
    let conn = env.unkeyed();
    conn.execute_batch("PRAGMA ignore_check_constraints = ON")
        .unwrap();
    conn.execute(sql, bound).unwrap();
}

fn text(value: &str) -> Value {
    Value::Text(value.to_string())
}

/// A raw `sessions` row: a registered Codex session `s1` with no project, with `sets`
/// in place of those columns.
fn session(env: &Env, sets: &[(&str, Value)]) {
    let mut row = vec![
        ("harness", text("codex")),
        ("session_id", text("s1")),
        ("status", text("registered")),
        ("origin", text("start")),
        ("project_id", Value::Null),
        ("candidate_id", Value::Null),
        ("candidate_root", Value::Null),
        ("entry_cwd", text("/tmp/x")),
        ("started_at", text(AT)),
        ("last_active_at", text(AT)),
        ("prompt_count", Value::Integer(0)),
        ("last_write_prompt_count", Value::Integer(0)),
        ("first_prompt", Value::Null),
        ("recent_prompts", text("[]")),
    ];
    for (column, value) in sets {
        let at = row.iter().position(|(name, _)| name == column).unwrap();
        row[at].1 = value.clone();
    }
    insert(env, "sessions", &row);
}

/// A raw `session_moves` row of the Codex session `s1`, with `sets` in place.
fn moved(env: &Env, sets: &[(&str, Value)]) {
    let mut row = vec![
        ("harness", text("codex")),
        ("session_id", text("s1")),
        ("seq", Value::Integer(1)),
        ("moved_at", text("2026-11-16T00:00:00.000000Z")),
        ("from_entry_cwd", text("/a")),
        ("from_project_id", Value::Null),
        ("to_entry_cwd", text("/b")),
        ("to_project_id", Value::Null),
    ];
    for (column, value) in sets {
        let at = row.iter().position(|(name, _)| name == column).unwrap();
        row[at].1 = value.clone();
    }
    insert(env, "session_moves", &row);
}

fn insert(env: &Env, table: &str, row: &[(&str, Value)]) {
    let columns: Vec<&str> = row.iter().map(|(name, _)| *name).collect();
    let marks = vec!["?"; row.len()].join(", ");
    damage(
        env,
        &format!(
            "INSERT INTO {table} ({}) VALUES ({marks})",
            columns.join(", ")
        ),
        rusqlite::params_from_iter(row.iter().map(|(_, value)| value)),
    );
}

/// A change row with no step: `step_count` says how many it should have.
fn change(env: &Env, step_count: i64) {
    damage(
        env,
        "INSERT INTO changes (change_id, at, changed_by, step_count) \
         VALUES ('cccccccccc', '2026-09-27T00:00:00.000000Z', 'human', ?1)",
        [step_count],
    );
}

fn cites(env: &Env, memory_id: &str, version: i64, source_id: &str) {
    damage(
        env,
        "INSERT INTO memory_sources (memory_id, version, source_id, source_version) \
         VALUES (?1, ?2, ?3, 1)",
        (memory_id, version, source_id),
    );
}

// --- the store as a whole ---

#[test]
fn a_missing_store_is_uninitialized_and_nothing_is_created() {
    let env = setup();
    let none = env.tmp.join("none");
    let report = StoreInspector::new(&none).inspect().unwrap();
    assert_eq!(
        report,
        StoreReport {
            initialized: false,
            entries: Vec::new(),
            projects: Vec::new(),
            findings: Vec::new(),
            incomplete_changes: Vec::new(),
        }
    );
    assert!(!none.exists());
    // a directory without a database is the same, and stays empty
    fs::create_dir(&none).unwrap();
    assert_eq!(StoreInspector::new(&none).inspect().unwrap(), report);
    assert_eq!(fs::read_dir(&none).unwrap().count(), 0);
}

#[test]
fn a_root_that_is_no_directory_is_a_storage_failure() {
    let env = setup();
    let file = env.tmp.join("file");
    fs::write(&file, "").unwrap();
    assert_eq!(
        StoreInspector::new(&file).inspect(),
        Err(CoreError::StorageFailure)
    );
}

#[test]
fn a_healthy_store_lists_entries_and_projects_with_counts() {
    let env = setup();
    let kept = memory(&env, "kept");
    let gone = memory(&env, "gone");
    env.manage(draft(&env.other.id, "one elsewhere").op());
    env.manage(draft(&env.other.id, "another elsewhere").op());
    env.apply(&[memriver_core::models::changes::Op::SoftDelete {
        memory_id: gone,
        expected_version: 1,
        unread_since: None,
    }])
    .unwrap();
    let report = inspect(&env);
    assert!(report.initialized);
    assert_eq!(report.findings, Vec::new());
    assert_eq!(report.incomplete_changes, Vec::<String>::new());
    let entries: Vec<_> = report
        .entries
        .iter()
        .map(|entry| (entry.memory.id.as_str(), entry.location_hint.clone()))
        .collect();
    assert_eq!(entries.len(), 3);
    assert!(entries.contains(&(kept.as_str(), format!("memories/{kept}"))));
    // by id
    assert!(entries.is_sorted());
    // global last, the others by name
    let listed: Vec<_> = report
        .projects
        .iter()
        .map(|project| {
            (
                project.name.as_str(),
                project.root.clone(),
                project.is_global,
                project.root_state,
                project.active_memories,
                project.deleted_memories,
            )
        })
        .collect();
    let root = |name: &str| Some(env.tmp.join(name).to_str().unwrap().to_string());
    assert_eq!(
        listed,
        [
            ("elsewhere", root("elsewhere"), false, RootState::Ok, 2, 0),
            ("work", root("work"), false, RootState::Ok, 1, 1),
            ("global", None, true, RootState::Unbound, 0, 0),
        ]
    );
    assert_eq!(report.projects[1].id, env.project.id);
}

#[test]
fn a_store_without_global_is_not_initialized() {
    let env = setup();
    damage(&env, "DELETE FROM projects WHERE is_global = 1", []);
    let report = inspect(&env);
    assert!(!report.initialized);
    assert_eq!((report.findings.len(), report.projects.len()), (0, 2));
}

#[test]
fn inspector_never_writes() {
    let env = setup();
    memory(&env, "a memory");
    session(&env, &[("project_id", text(&env.project.id))]);
    let database = env.store.join(DATABASE_FILENAME);
    let before = fs::read(&database).unwrap();
    let names = |root: &Path| {
        let mut names: Vec<_> = fs::read_dir(root)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect();
        names.sort();
        names
    };
    let beside = names(&env.store);
    inspect(&env);
    env.services().maintenance.diagnose(None, 90).unwrap();
    env.services().maintenance.scan_policy().unwrap();
    assert_eq!(fs::read(&database).unwrap(), before);
    assert_eq!(names(&env.store), beside);
}

/// A store in WAL mode whose last write is still in the log: reading it -- the
/// inspection, the diagnosis, the policy scan -- sees that write and leaves the database
/// file as it is. Closing the last connection would otherwise write the log back.
#[test]
fn a_store_in_wal_mode_is_read_without_writing_its_log_back() {
    let env = setup();
    let id = memory(&env, "as it was written");
    let database = env.store.join(DATABASE_FILENAME);
    let log = env.store.join(format!("{DATABASE_FILENAME}-wal"));
    {
        let conn = env.raw();
        // this writer leaves its log behind, as one that was killed does
        conn.set_db_config(
            rusqlite::config::DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,
            true,
        )
        .unwrap();
        let mode: String = conn
            .query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
        conn.execute("UPDATE memories SET body = 'only in the log'", [])
            .unwrap();
        conn.execute(
            "UPDATE memory_versions SET description = ?1",
            [common::SECRET],
        )
        .unwrap();
    }
    let before = fs::read(&database).unwrap();
    let logged = fs::read(&log).unwrap();
    assert!(!logged.is_empty());
    let mismatch = [("version-mismatch".to_string(), Some(id.clone()))];
    // each of the three reads what is in the log
    assert_eq!(kinds(&env), mismatch);
    assert!(
        fs::read(&database).unwrap() == before,
        "after the inspection"
    );
    assert_eq!(found(&env).len(), 1);
    assert!(
        fs::read(&database).unwrap() == before,
        "after the diagnosis"
    );
    let hits = env.services().maintenance.scan_policy().unwrap();
    assert_eq!((hits.len(), hits[0].memory_id.as_str()), (1, id.as_str()));
    assert!(fs::read(&database).unwrap() == before, "after the scan");
    assert!(fs::read(&log).unwrap() == logged, "the log itself");
}

#[test]
fn an_unknown_schema_is_one_finding() {
    let env = setup();
    memory(&env, "not looked at");
    env.raw().execute_batch("PRAGMA user_version = 9").unwrap();
    let report = inspect(&env);
    assert!(report.initialized && report.entries.is_empty() && report.projects.is_empty());
    assert_eq!(
        found(&env),
        [finding("unknown-schema", "memriver.db", None, None)]
    );
}

#[test]
fn a_store_with_tables_and_no_version_is_an_unknown_schema() {
    let env = setup();
    env.raw().execute_batch("PRAGMA user_version = 0").unwrap();
    assert_eq!(
        found(&env),
        [finding("unknown-schema", "memriver.db", None, None)]
    );
}

#[test]
fn versions_1_to_5_are_unknown_schema_to_the_inspector() {
    for version in 1..=5 {
        let env = setup();
        memory(&env, "not looked at");
        env.raw()
            .execute_batch(&format!("PRAGMA user_version = {version}"))
            .unwrap();
        let database = env.store.join(DATABASE_FILENAME);
        let before = fs::read(&database).unwrap();
        let report = inspect(&env);
        let kinds: Vec<_> = report.findings.iter().map(|f| f.kind.as_str()).collect();
        assert_eq!(kinds, ["unknown-schema"], "version {version}");
        assert!(report.initialized && report.entries.is_empty() && report.projects.is_empty());
        // nothing was brought forward
        assert_eq!(fs::read(&database).unwrap(), before, "version {version}");
        assert_eq!(fs::read_dir(&env.store).unwrap().count(), 1);
    }
}

#[test]
fn an_empty_database_file_is_uninitialized() {
    let env = setup();
    let store = env.tmp.join("fresh");
    fs::create_dir(&store).unwrap();
    fs::write(store.join(DATABASE_FILENAME), "").unwrap();
    let report = StoreInspector::new(&store).inspect().unwrap();
    assert!(!report.initialized && report.findings.is_empty());
}

#[test]
fn a_symlinked_database_is_unsafe_and_not_followed() {
    let env = setup();
    let store = env.tmp.join("linked");
    fs::create_dir(&store).unwrap();
    // the link leads to a real store: it is not read
    symlink(
        env.store.join(DATABASE_FILENAME),
        store.join(DATABASE_FILENAME),
    )
    .unwrap();
    let report = StoreInspector::new(&store).inspect().unwrap();
    assert!(report.initialized && report.projects.is_empty());
    let mut settings = env.settings.clone();
    settings.root = store.clone();
    let services = memriver_core::build_services(&settings, &env.home, &env.env, None);
    let diagnosis = services.maintenance.diagnose(None, 90).unwrap();
    let (reason, suggestion) = words("unsafe-database");
    let [only] = &diagnosis.findings[..] else {
        panic!("one finding: {diagnosis:?}")
    };
    assert_eq!(
        (
            only.kind.as_str(),
            &only.location_hints[..],
            only.reason.as_str(),
            only.suggestion.as_str()
        ),
        (
            "unsafe-database",
            &["memriver.db".to_string()][..],
            reason,
            suggestion
        )
    );
    // a directory in its place is no database either
    let other = env.tmp.join("directory");
    fs::create_dir_all(other.join(DATABASE_FILENAME)).unwrap();
    let report = StoreInspector::new(&other).inspect().unwrap();
    assert_eq!(report.findings[0].kind, "unsafe-database");
}

#[test]
fn a_leftover_file_store_is_legacy_layout() {
    for name in ["store.toml", "projects", "memories", "registry", "global"] {
        let env = setup();
        let target = env.store.join(name);
        if name.ends_with(".toml") {
            fs::write(&target, "global_project = \"x\"\n").unwrap();
        } else {
            fs::create_dir(&target).unwrap();
        }
        assert_eq!(found(&env), [finding("legacy-layout", name, None, None)]);
    }
    // a dangling link by one of the names counts: the name is there
    let env = setup();
    symlink(env.tmp.join("nowhere"), env.store.join("registry")).unwrap();
    assert_eq!(
        found(&env),
        [finding("legacy-layout", "registry", None, None)]
    );
}

#[test]
fn only_a_legacy_store_is_degraded_but_not_initialized() {
    let env = setup();
    let store = env.tmp.join("old");
    fs::create_dir_all(store.join("memories")).unwrap();
    fs::create_dir_all(store.join("global")).unwrap();
    let report = StoreInspector::new(&store).inspect().unwrap();
    assert!(!report.initialized);
    let found: Vec<_> = report
        .findings
        .iter()
        .map(|found| (found.kind.as_str(), found.location_hint.as_str()))
        .collect();
    assert_eq!(
        found,
        [("legacy-layout", "global"), ("legacy-layout", "memories")]
    );
}

#[test]
fn integrity_check_failing_is_a_finding_and_the_inspection_goes_on() {
    let env = setup();
    let bad = memory(&env, "b");
    // a CHECK that was passed by is what the integrity check reports
    damage(
        &env,
        "UPDATE memories SET trust = 'odd' WHERE id = ?1",
        [&bad],
    );
    assert_eq!(
        found(&env),
        [
            finding("invalid-row", &format!("memories/{bad}"), Some(&bad), None),
            finding("integrity", "memriver.db", None, None),
        ]
    );
    assert_eq!(inspect(&env).projects.len(), 3);
}

// --- projects and their directories ---

#[test]
fn an_offline_directory_is_a_state_not_a_finding() {
    let env = setup();
    fs::remove_dir(env.tmp.join("work")).unwrap();
    let report = inspect(&env);
    assert_eq!(report.findings, Vec::new());
    assert_eq!(report.projects[1].root_state, RootState::Missing);
}

#[test]
fn a_re_pointed_directory_is_a_finding() {
    let env = setup();
    let (work, moved) = (env.tmp.join("work"), env.tmp.join("moved"));
    fs::rename(&work, &moved).unwrap();
    symlink(&moved, &work).unwrap();
    let project = &env.project.id;
    assert_eq!(
        found(&env),
        [finding(
            "non-canonical-root",
            &format!("projects/{project}"),
            None,
            Some(project)
        )]
    );
    assert_eq!(
        inspect(&env).projects[1].root_state,
        RootState::NotCanonical
    );
}

#[test]
fn an_undecodable_project_column_is_invalid_row_not_a_crash() {
    let env = setup();
    let project = &env.project.id;
    damage(
        &env,
        "UPDATE projects SET name = CAST(X'80' AS TEXT) WHERE id = ?1",
        [project],
    );
    assert_eq!(
        found(&env),
        [finding(
            "invalid-row",
            &format!("projects/{project}"),
            None,
            Some(project)
        )]
    );
    assert!(inspect(&env).projects.iter().all(|p| p.id != *project));
    // an id that is none is not shown
    damage(
        &env,
        "UPDATE projects SET id = CAST(X'80808080808080808080' AS TEXT) WHERE id = ?1",
        [project],
    );
    let rows: Vec<_> = found(&env)
        .into_iter()
        .filter(|found| found.0 != "integrity")
        .collect();
    assert_eq!(rows, [finding("invalid-row", "projects", None, None)]);
}

// --- memories ---

#[test]
fn an_orphan_is_reported_and_kept_out_of_entries() {
    let env = setup();
    let orphan = memory(&env, "b");
    damage(
        &env,
        "UPDATE memories SET project_id = 'zzzzzzzzzz' WHERE id = ?1",
        [&orphan],
    );
    assert_eq!(
        found(&env),
        [finding(
            "orphan",
            &format!("memories/{orphan}"),
            Some(&orphan),
            None
        )]
    );
    assert!(inspect(&env).entries.is_empty());
}

#[test]
fn two_orphans_one_with_an_undecodable_id_do_not_crash_the_sort() {
    let env = setup();
    let (good, bad) = (memory(&env, "one"), memory(&env, "two"));
    damage(&env, "UPDATE memories SET project_id = 'zzzzzzzzzz'", []);
    damage(
        &env,
        "UPDATE memories SET id = CAST(X'80808080808080808080' AS TEXT) WHERE id = ?1",
        [&bad],
    );
    let orphans: Vec<_> = found(&env)
        .into_iter()
        .filter(|found| found.0 == "orphan")
        .collect();
    assert_eq!(
        orphans,
        [
            finding("orphan", "memories", None, None),
            finding("orphan", &format!("memories/{good}"), Some(&good), None),
        ]
    );
    // text that is no id is not shown either
    damage(
        &env,
        "UPDATE memories SET id = '../../evil' WHERE id = ?1",
        [&good],
    );
    let orphans: Vec<_> = found(&env)
        .into_iter()
        .filter(|found| found.0 == "orphan")
        .collect();
    assert_eq!(orphans, vec![finding("orphan", "memories", None, None); 2]);
}

#[test]
fn an_invalid_row_is_reported_and_kept_out_of_entries() {
    // an unknown trust; a read time that is none; a body that is not text; and the
    // same damage to a deleted row
    let damages = [
        ("trust = 'odd'", false),
        ("last_read_at = 'not-a-timestamp'", false),
        ("body = CAST(X'80' AS TEXT)", false),
        ("trust = 'odd'", true),
    ];
    for (edit, deleted) in damages {
        let env = setup();
        let bad = memory(&env, "b");
        if deleted {
            env.apply(&[memriver_core::models::changes::Op::SoftDelete {
                memory_id: bad.clone(),
                expected_version: 1,
                unread_since: None,
            }])
            .unwrap();
        }
        damage(
            &env,
            &format!("UPDATE memories SET {edit} WHERE id = ?1"),
            [&bad],
        );
        let rows: Vec<_> = found(&env)
            .into_iter()
            .filter(|found| found.0 != "integrity")
            .collect();
        // a row that is no memory is not compared with its history
        assert_eq!(
            rows,
            [finding(
                "invalid-row",
                &format!("memories/{bad}"),
                Some(&bad),
                None
            )],
            "{edit}"
        );
        assert!(inspect(&env).entries.is_empty());
    }
}

// --- sessions ---

#[test]
fn a_healthy_session_row_is_not_a_finding() {
    let env = setup();
    session(&env, &[("project_id", text(&env.project.id))]);
    assert_eq!(found(&env), Vec::new());
}

#[test]
fn an_invalid_session_row_is_reported_as_invalid_row() {
    let env = setup();
    session(
        &env,
        &[("session_id", text("bad-1")), ("status", text("odd"))],
    );
    // a save mark above the prompts there are
    session(
        &env,
        &[
            ("session_id", text("bad-2")),
            ("project_id", text(&env.project.id)),
            ("prompt_count", Value::Integer(1)),
            ("last_write_prompt_count", Value::Integer(2)),
        ],
    );
    let rows: Vec<_> = found(&env)
        .into_iter()
        .filter(|found| found.0 != "integrity")
        .collect();
    assert_eq!(
        rows,
        [
            finding("invalid-row", "sessions/codex/bad-1", None, None),
            finding("invalid-row", "sessions/codex/bad-2", None, None),
        ]
    );
}

#[test]
fn a_session_whose_key_is_none_is_located_by_its_table_alone() {
    let env = setup();
    // a session id no harness sends, and a harness that is none: neither is shown
    session(&env, &[("session_id", text("two words"))]);
    session(&env, &[("harness", text("cursor\u{1b}[31m"))]);
    let rows: Vec<_> = found(&env)
        .into_iter()
        .filter(|found| found.0 != "integrity")
        .collect();
    assert_eq!(
        rows,
        vec![finding("invalid-row", "sessions", None, None); 2]
    );
}

#[test]
fn a_session_with_a_dangling_candidate_id_is_a_session_orphan_finding() {
    let env = setup();
    session(
        &env,
        &[
            ("session_id", text("pending-1")),
            ("status", text("pending")),
            ("origin", text("first-seen")),
            ("candidate_id", text("zzzzzzzzzz")),
            ("candidate_root", text("/z")),
        ],
    );
    // and one that is no session row besides: a save mark above its prompts
    session(
        &env,
        &[
            ("session_id", text("reg-2")),
            ("project_id", text("zzzzzzzzzz")),
            ("prompt_count", Value::Integer(1)),
            ("last_write_prompt_count", Value::Integer(2)),
        ],
    );
    // each is filed once, as an orphan: not as an invalid row too
    assert_eq!(
        found(&env),
        [
            finding("session-orphan", "sessions/codex/pending-1", None, None),
            finding("session-orphan", "sessions/codex/reg-2", None, None),
        ]
    );
}

#[test]
fn a_session_with_a_dangling_project_id_is_a_session_orphan_finding() {
    let env = setup();
    session(
        &env,
        &[
            ("harness", text("claude-code")),
            ("session_id", text("reg-1")),
            ("project_id", text("zzzzzzzzzz")),
        ],
    );
    assert_eq!(
        found(&env),
        [finding(
            "session-orphan",
            "sessions/claude-code/reg-1",
            None,
            None
        )]
    );
}

#[test]
fn a_healthy_session_move_is_not_a_finding() {
    let env = setup();
    session(&env, &[("project_id", text(&env.project.id))]);
    moved(&env, &[("to_project_id", text(&env.project.id))]);
    assert_eq!(found(&env), Vec::new());
}

#[test]
fn damaged_session_move_reported() {
    let env = setup();
    session(&env, &[("project_id", text(&env.project.id))]);
    moved(&env, &[("moved_at", text("not a time"))]);
    assert_eq!(
        found(&env),
        [finding(
            "session-move-damaged",
            "session_moves/codex/s1",
            None,
            None
        )]
    );
}

#[test]
fn session_move_without_session_reported() {
    let env = setup();
    moved(&env, &[("session_id", text("ghost"))]);
    moved(&env, &[("session_id", text("two words"))]);
    assert_eq!(
        found(&env),
        [
            finding("session-move-damaged", "session_moves", None, None),
            finding(
                "session-move-damaged",
                "session_moves/codex/ghost",
                None,
                None
            ),
        ]
    );
}

// --- history ---

#[test]
fn a_gap_in_the_versions_is_a_finding() {
    let env = setup();
    let id = memory(&env, "one");
    env.apply(&[body(&id, 1, "two")]).unwrap();
    env.apply(&[body(&id, 2, "three")]).unwrap();
    damage(
        &env,
        "DELETE FROM memory_versions WHERE memory_id = ?1 AND version = 2",
        [&id],
    );
    let project = Some(env.project.id.as_str());
    let gap = [finding(
        "version-gap",
        &format!("memories/{id}"),
        Some(&id),
        project,
    )];
    assert_eq!(found(&env), gap);
    // nothing else is said of a memory with a gap: not that its row differs from its
    // latest version, nor that a version is none
    damage(&env, "UPDATE memories SET body = 'edited by hand'", []);
    damage(&env, "UPDATE memory_versions SET trust = 'odd'", []);
    assert_eq!(found(&env), gap);
    // no version at all, and versions that do not start at 1
    damage(&env, "DELETE FROM memory_versions WHERE version = 1", []);
    assert_eq!(found(&env), gap);
    damage(&env, "DELETE FROM memory_versions", []);
    assert_eq!(found(&env), gap);
}

#[test]
fn a_huge_current_version_with_one_stored_row_is_a_gap_not_a_crash() {
    let env = setup();
    let id = memory(&env, "b");
    damage(
        &env,
        "UPDATE memories SET version = ?1 WHERE id = ?2",
        (i64::MAX, &id),
    );
    assert_eq!(kinds(&env), [("version-gap".to_string(), Some(id))]);
}

#[test]
fn a_row_that_differs_from_its_latest_version_is_a_finding() {
    let edits = [
        "body = 'edited by hand'",
        "description = 'edited by hand'",
        "type = 'user'",
        "trust = 'user'",
        "sync = 0",
        "deleted_at = '2026-09-24T00:00:00.000000Z'",
        "level = 1",
        "tags = '[\"git\"]'",
    ];
    for edit in edits {
        let env = setup();
        let id = memory(&env, "b");
        damage(
            &env,
            &format!("UPDATE memories SET {edit} WHERE id = ?1"),
            [&id],
        );
        assert_eq!(
            found(&env),
            [finding(
                "version-mismatch",
                &format!("memories/{id}"),
                Some(&id),
                Some(&env.project.id)
            )],
            "{edit}"
        );
    }
}

#[test]
fn a_version_memriver_could_not_have_written_is_a_finding() {
    // in the latest version, and in an older one under a healthy current row
    let edits = [
        ("tags = '[\"Bad\"]'", 2),
        ("body = CAST(X'80' AS TEXT)", 1),
        ("trust = 'invalid-trust'", 1),
        ("type = 'odd'", 1),
        ("level = 0", 1),
        ("sync = 2", 1),
        ("deleted = 2", 1),
    ];
    for (edit, version) in edits {
        let env = setup();
        let id = memory(&env, "one");
        env.apply(&[body(&id, 1, "two")]).unwrap();
        damage(
            &env,
            &format!("UPDATE memory_versions SET {edit} WHERE memory_id = ?1 AND version = ?2"),
            (&id, version),
        );
        let rows: Vec<_> = found(&env)
            .into_iter()
            .filter(|found| found.0 != "integrity")
            .collect();
        assert_eq!(
            rows,
            [finding(
                "invalid-row",
                &format!("memories/{id}"),
                Some(&id),
                Some(&env.project.id)
            )],
            "{edit}"
        );
    }
}

#[test]
fn a_version_naming_a_change_without_its_step_is_a_finding() {
    let env = setup();
    let id = memory(&env, "b");
    let change_id: String = env
        .raw()
        .query_row("SELECT change_id FROM changes", [], |row| row.get(0))
        .unwrap();
    damage(&env, "DELETE FROM change_steps", []);
    let unrecorded = [finding(
        "unrecorded-version",
        &format!("memories/{id}"),
        Some(&id),
        Some(&env.project.id),
    )];
    // the change is listed as incomplete beside it: that is no finding
    assert_eq!(diagnosed(&env), (unrecorded.to_vec(), vec![change_id]));
    // a step for another version of the memory is not its step
    damage(
        &env,
        "INSERT INTO change_steps (change_id, step, memory_id, op, before_version, \
         after_version) SELECT change_id, 1, ?1, 'update', 1, 2 FROM changes",
        [&id],
    );
    assert_eq!(diagnosed(&env), (unrecorded.to_vec(), Vec::new()));
}

#[test]
fn a_version_naming_a_change_whose_row_is_gone_is_unrecorded_version() {
    let env = setup();
    let id = memory(&env, "b");
    damage(&env, "DELETE FROM changes", []);
    assert_eq!(
        diagnosed(&env),
        (
            vec![finding(
                "unrecorded-version",
                &format!("memories/{id}"),
                Some(&id),
                Some(&env.project.id)
            )],
            Vec::new()
        )
    );
}

#[test]
fn an_imported_version_names_no_change_and_needs_none() {
    let env = setup();
    memory(&env, "b");
    damage(&env, "UPDATE memory_versions SET change_id = NULL", []);
    damage(&env, "DELETE FROM change_steps", []);
    damage(&env, "DELETE FROM changes", []);
    assert_eq!(diagnosed(&env), (Vec::new(), Vec::new()));
}

#[test]
fn a_source_citing_a_version_that_is_not_stored_is_a_finding() {
    let env = setup();
    let id = memory(&env, "b");
    cites(&env, &id, 1, "zzzzzzzzzz");
    let dangling = finding(
        "dangling-source",
        &format!("memories/{id}"),
        Some(&id),
        Some(&env.project.id),
    );
    assert_eq!(found(&env), std::slice::from_ref(&dangling));
    // one finding for a memory, however many of its rows dangle
    cites(&env, &id, 1, "yyyyyyyyyy");
    assert_eq!(found(&env), [dangling]);
}

#[test]
fn a_source_finding_never_carries_a_damaged_project_id() {
    let env = setup();
    let id = memory(&env, "b");
    cites(&env, &id, 1, "zzzzzzzzzz");
    damage(
        &env,
        "UPDATE memories SET project_id = CAST(X'80' AS TEXT) WHERE id = ?1",
        [&id],
    );
    let location = format!("memories/{id}");
    let both = [
        finding("dangling-source", &location, Some(&id), None),
        finding("orphan", &location, Some(&id), None),
    ];
    assert_eq!(found(&env), both);
    // nor text that is no id
    damage(&env, "UPDATE memories SET project_id = '../../evil'", []);
    assert_eq!(found(&env), both);
}

#[test]
fn a_source_row_whose_own_citing_version_is_not_stored_is_also_a_finding() {
    let env = setup();
    let (citer, cited) = (memory(&env, "citer"), memory(&env, "cited"));
    cites(&env, &citer, 9, &cited);
    assert_eq!(kinds(&env), [("dangling-source".to_string(), Some(citer))]);
    // a citing memory that is not there at all is named by its table
    cites(&env, "yyyyyyyyy!", 1, &cited);
    let dangling: Vec<_> = found(&env).into_iter().map(|found| found.1).collect();
    assert_eq!(dangling.len(), 2);
    assert_eq!(dangling[0], "memories");
}

#[test]
fn a_source_cycle_is_a_finding_for_every_memory_on_it() {
    let env = setup();
    let (first, second) = (memory(&env, "first"), memory(&env, "second"));
    let aside = memory(&env, "cites the cycle, is not on it");
    cites(&env, &first, 1, &second);
    cites(&env, &second, 1, &first);
    cites(&env, &aside, 1, &first);
    let project = Some(env.project.id.as_str());
    let mut on_it = [first, second];
    on_it.sort();
    let cycle = |id: &String| finding("source-cycle", &format!("memories/{id}"), Some(id), project);
    assert_eq!(found(&env), [cycle(&on_it[0]), cycle(&on_it[1])]);
}

#[test]
fn a_memory_citing_itself_is_on_a_cycle() {
    let env = setup();
    let id = memory(&env, "b");
    cites(&env, &id, 1, &id);
    assert_eq!(kinds(&env), [("source-cycle".to_string(), Some(id))]);
}

#[test]
fn a_change_with_fewer_steps_than_recorded_is_listed_not_a_finding() {
    let env = setup();
    change(&env, 2);
    assert_eq!(
        diagnosed(&env),
        (Vec::new(), vec!["cccccccccc".to_string()])
    );
    let report = env.services().maintenance.diagnose(None, 90).unwrap();
    assert_eq!(report.state.as_str(), "empty");
}

#[test]
fn a_change_with_an_undecodable_id_is_invalid_row_not_incomplete() {
    let env = setup();
    damage(
        &env,
        "INSERT INTO changes (change_id, at, changed_by, step_count) VALUES \
         (CAST(X'80616161616161616161' AS TEXT), '2026-09-27T00:00:00.000000Z', 'human', 2)",
        [],
    );
    assert_eq!(
        diagnosed(&env),
        (
            vec![finding("invalid-row", "changes", None, None)],
            Vec::new()
        )
    );
}

// --- the report ---

#[test]
fn findings_sorted_by_location_then_kind() {
    let env = setup();
    let (first, second) = (memory(&env, "first"), memory(&env, "second"));
    let mut ids = [first, second];
    ids.sort();
    // on each memory: a cycle and a dangling source; and around them findings of other
    // places, made in another order than they are reported in
    cites(&env, &ids[0], 1, &ids[1]);
    cites(&env, &ids[1], 1, &ids[0]);
    cites(&env, &ids[0], 1, "zzzzzzzzzz");
    cites(&env, &ids[1], 1, "zzzzzzzzzz");
    moved(&env, &[("session_id", text("ghost"))]);
    fs::create_dir(env.store.join("registry")).unwrap();
    session(&env, &[("project_id", text("zzzzzzzzzz"))]);
    let listed: Vec<_> = inspect(&env)
        .findings
        .into_iter()
        .map(|found| (found.location_hint, found.kind))
        .collect();
    let expected = [
        (format!("memories/{}", ids[0]), "dangling-source"),
        (format!("memories/{}", ids[0]), "source-cycle"),
        (format!("memories/{}", ids[1]), "dangling-source"),
        (format!("memories/{}", ids[1]), "source-cycle"),
        ("registry".to_string(), "legacy-layout"),
        (
            "session_moves/codex/ghost".to_string(),
            "session-move-damaged",
        ),
        ("sessions/codex/s1".to_string(), "session-orphan"),
    ];
    let expected: Vec<_> = expected
        .into_iter()
        .map(|(location, kind)| (location, kind.to_string()))
        .collect();
    assert_eq!(listed, expected);
}

/// The recorded columns no write of memriver makes: each is the one `invalid-row` of its
/// row, in the inspector's own words -- none of the decoder's.
#[test]
fn a_damaged_json_column_is_an_invalid_row_with_the_fixed_reason() {
    let env = setup();
    let id = memory(&env, "b");
    session(&env, &[("project_id", text(&env.project.id))]);
    let memory_row = finding("invalid-row", &format!("memories/{id}"), Some(&id), None);
    let session_row = finding("invalid-row", "sessions/codex/s1", None, None);
    let mut checked = 0;
    for line in include_str!("fixtures/damaged_columns.jsonl").lines() {
        let sample: serde_json::Value = serde_json::from_str(line).unwrap();
        let (column, raw) = (
            sample["column"].as_str().unwrap(),
            sample["raw"].as_str().unwrap(),
        );
        let shown: String = raw.chars().take(60).collect();
        let (table, healthy, expected) = match column {
            "tags" => ("memories", Value::Text("[]".to_string()), &memory_row),
            "first_prompt" => ("sessions", Value::Null, &session_row),
            "recent_prompts" => ("sessions", Value::Text("[]".to_string()), &session_row),
            other => panic!("unknown column {other}"),
        };
        let set = format!("UPDATE {table} SET {column} = ?1");
        damage(&env, &set, [raw]);
        assert_eq!(
            found(&env),
            std::slice::from_ref(expected),
            "{column} {shown:?}"
        );
        damage(&env, &set, [healthy]);
        checked += 1;
    }
    assert_eq!(checked, 117);
    assert_eq!(found(&env), Vec::new());
}

/// Tags nested ten thousand deep: the one row is reported, the inspection goes on.
#[test]
fn tags_nested_ten_thousand_deep_are_one_invalid_row() {
    let env = setup();
    let (id, other) = (memory(&env, "deep"), memory(&env, "beside it"));
    let deep = format!("{}{}", "[".repeat(10_000), "]".repeat(10_000));
    damage(
        &env,
        "UPDATE memories SET tags = ?1 WHERE id = ?2",
        (&deep, &id),
    );
    assert_eq!(
        found(&env),
        [finding(
            "invalid-row",
            &format!("memories/{id}"),
            Some(&id),
            None
        )]
    );
    let entries: Vec<_> = inspect(&env)
        .entries
        .into_iter()
        .map(|entry| entry.memory.id)
        .collect();
    assert_eq!(entries, [other]);
    // and in a stored version
    damage(&env, "UPDATE memories SET tags = '[]'", []);
    damage(
        &env,
        "UPDATE memory_versions SET tags = ?1 WHERE memory_id = ?2",
        (&deep, &id),
    );
    assert_eq!(kinds(&env), [("invalid-row".to_string(), Some(id))]);
}
