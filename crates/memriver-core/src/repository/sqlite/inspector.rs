//! Whole-store administrative inspection of the SQLite store.
//!
//! The serving read paths skip what they cannot trust, which is right for an agent and
//! wrong for a doctor. This inspector walks the same tables and keeps what reads drop,
//! each finding with a fixed reason. It never creates the database and never changes
//! it: a store of another schema version is one finding, not something to bring forward.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::Connection;
use rusqlite::types::Value;

use super::database::{
    self, MEMORY_COLUMNS, PROJECT_COLUMNS, memory_from_row, project_from_row, values,
};
use super::memory_store::history_state;
use super::schema::SCHEMA_VERSION;
use super::session_store::{
    SESSION_COLUMNS, SESSION_MOVE_COLUMNS, session_from_row, session_move_from_row,
};
use crate::models::diagnostics::{
    InspectedMemory, InspectedProject, RootState, StoreFinding, StoreReport,
};
use crate::models::errors::CoreError;
use crate::models::helpers::{is_id, py_repr};
use crate::models::memory::Memory;
use crate::models::session::{Harness, SessionKey};
use crate::repository::directories::{self, DATABASE_FILENAME};
use crate::settings::{BUSY_TIMEOUT_MS, lexical_absolute};

/// The names the file store of earlier versions used at the store root.
const LEGACY_NAMES: [&str; 5] = ["global", "memories", "projects", "registry", "store.toml"];

fn reason(kind: &str) -> &'static str {
    match kind {
        "unknown-schema" => "database schema is not one this version knows",
        "unsafe-database" => "memriver.db is a symlink or not a regular file; it is not followed",
        "integrity" => "SQLite integrity check failed",
        "orphan" => "memory belongs to a project that does not exist",
        "invalid-row" => "row holds a value memriver could not have written",
        "non-canonical-root" => "bound directory is no longer a canonical path",
        "unverifiable-root" => "bound directory could not be checked",
        "legacy-layout" => {
            "file-store layout from an earlier version; this version does not read it"
        }
        "root-conflict" => "two projects are bound to the same directory under different spellings",
        "session-orphan" => "session refers to a project that does not exist",
        "session-move-damaged" => "session move record is damaged or names no session",
        "version-gap" => "stored versions of this memory are not numbered 1 to its current version",
        "version-mismatch" => "memory row differs from its latest stored version",
        "unrecorded-version" => "a version names a change that holds no step for it",
        "dangling-source" => "a version cites a memory version that is not stored",
        "source-cycle" => "this memory's sources lead back to itself",
        other => unreachable!("no finding is of the kind {other}"),
    }
}

fn finding(
    kind: &str,
    location: impl Into<String>,
    project_id: Option<String>,
    memory_id: Option<String>,
) -> StoreFinding {
    StoreFinding {
        kind: kind.to_string(),
        project_id,
        location_hint: location.into(),
        memory_id,
        reason: reason(kind).to_string(),
    }
}

/// A finding about the memory `id`: at `memories/<id>`, or at the bare table when the
/// id is none a command could name.
fn about_memory(kind: &str, id: Option<String>, project_id: Option<String>) -> StoreFinding {
    let location = match &id {
        Some(id) => format!("memories/{id}"),
        None => "memories".to_string(),
    };
    finding(kind, location, project_id, id)
}

fn sorted(mut findings: Vec<StoreFinding>) -> Vec<StoreFinding> {
    findings.sort_by(|a, b| (&a.location_hint, &a.kind).cmp(&(&b.location_hint, &b.kind)));
    findings
}

/// A stored value as something to look up and to order by: a damaged key column holds
/// bytes that are not text, and is still one key.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
enum Raw {
    Null,
    Integer(i64),
    Real(u64),
    Text(String),
    Bytes(Vec<u8>),
}

fn raw(value: &Value) -> Raw {
    match value {
        Value::Null => Raw::Null,
        Value::Integer(number) => Raw::Integer(*number),
        Value::Real(number) => Raw::Real(number.to_bits()),
        Value::Text(text) => Raw::Text(text.clone()),
        Value::Blob(bytes) => Raw::Bytes(bytes.clone()),
    }
}

/// What ids that are text and ids that are not are ordered by together: text as Python
/// writes a string, bytes as it writes bytes.
fn written(id: &Raw) -> String {
    match id {
        Raw::Text(text) => py_repr(text),
        Raw::Bytes(bytes) => {
            let quote = if bytes.contains(&b'\'') && !bytes.contains(&b'"') {
                '"'
            } else {
                '\''
            };
            let mut repr = format!("b{quote}");
            for &byte in bytes {
                match byte {
                    b'\\' => repr.push_str("\\\\"),
                    b'\t' => repr.push_str("\\t"),
                    b'\n' => repr.push_str("\\n"),
                    b'\r' => repr.push_str("\\r"),
                    byte if byte as char == quote => {
                        repr.push('\\');
                        repr.push(quote);
                    }
                    b' '..=b'~' => repr.push(byte as char),
                    byte => repr.push_str(&format!("\\x{byte:02x}")),
                }
            }
            repr.push(quote);
            repr
        }
        other => format!("{other:?}"),
    }
}

/// `value` as an addressable id, or None.
fn shaped(value: &Raw) -> Option<String> {
    match value {
        Raw::Text(text) if is_id(text) => Some(text.clone()),
        _ => None,
    }
}

/// `sessions/<harness>/<session_id>`, or the bare table name when either column is not
/// one of a session memriver could have written: a location never shows a raw control
/// character or a value that is not text.
fn session_location(harness: &Value, session_id: &Value, table: &str) -> String {
    if let (Value::Text(harness), Value::Text(session_id)) = (harness, session_id)
        && let Some(harness) = Harness::parse(harness)
        && let Ok(key) = SessionKey::new(harness, session_id)
    {
        return format!("{table}/{}/{}", key.harness(), key.session_id());
    }
    table.to_string()
}

/// The nodes a walk along `edges` can leave and come back to.
fn on_cycles(edges: &HashMap<Raw, HashSet<Raw>>) -> Vec<&Raw> {
    // ponytail: one walk per node, quadratic in the worst case; fine at local-store
    // scale, a strongly-connected-components pass if stores grow large
    let mut looped = Vec::new();
    for (start, successors) in edges {
        let mut seen = HashSet::new();
        let mut stack: Vec<&Raw> = successors.iter().collect();
        while let Some(node) = stack.pop() {
            if node == start {
                looped.push(start);
                break;
            }
            if seen.insert(node) {
                stack.extend(edges.get(node).into_iter().flatten());
            }
        }
    }
    looped
}

fn rows(conn: &Connection, sql: &str) -> Result<Vec<Vec<Value>>, CoreError> {
    let mut statement = conn.prepare(sql)?;
    let rows = statement.query_map([], values)?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

#[cfg(test)]
type Planted = Box<dyn FnOnce()>;

#[cfg(test)]
thread_local! {
    /// What a test runs in the middle of an inspection: (inside the read transaction,
    /// after it and before the directories are looked at).
    static MEANWHILE: std::cell::RefCell<[Option<Planted>; 2]> =
        const { std::cell::RefCell::new([None, None]) };
}

/// Runs what a test planted for this moment of the inspection, once.
#[cfg(test)]
fn meanwhile(moment: usize) {
    if let Some(then) = MEANWHILE.with_borrow_mut(|planted| planted[moment].take()) {
        then();
    }
}

/// What one read transaction found: a finished report for an empty or unknown store,
/// else the rows the directory checks go on from.
enum Snapshot {
    Finished {
        initialized: bool,
    },
    Read {
        initialized: bool,
        entries: Vec<InspectedMemory>,
        projects: Vec<InspectedProject>,
        incomplete_changes: Vec<String>,
    },
}

/// `StoreReport` of the SQLite store: every row, read-only.
pub struct StoreInspector {
    root: PathBuf,
    busy_timeout_ms: u64,
}

impl StoreInspector {
    pub fn new(root: &Path) -> StoreInspector {
        StoreInspector::with_busy_timeout(root, BUSY_TIMEOUT_MS)
    }

    /// For a test that must not wait the whole `BUSY_TIMEOUT_MS` for a lock.
    pub fn with_busy_timeout(root: &Path, busy_timeout_ms: u64) -> StoreInspector {
        StoreInspector {
            root: root.to_path_buf(),
            busy_timeout_ms,
        }
    }

    pub fn inspect(&self) -> Result<StoreReport, CoreError> {
        let finished = |initialized, findings| StoreReport {
            initialized,
            entries: Vec::new(),
            projects: Vec::new(),
            findings: sorted(findings),
            incomplete_changes: Vec::new(),
        };
        // the one place of the store, as every other operation finds it
        let root = lexical_absolute(&self.root).map_err(|_| CoreError::StorageFailure)?;
        match fs::metadata(&root) {
            Ok(found) if found.is_dir() => {}
            Err(err) if err.kind() == ErrorKind::NotFound => {
                return Ok(finished(false, Vec::new()));
            }
            _ => return Err(CoreError::StorageFailure),
        }
        let mut findings: Vec<StoreFinding> = LEGACY_NAMES
            .into_iter()
            .filter(|name| fs::symlink_metadata(root.join(name)).is_ok())
            .map(|name| finding("legacy-layout", name, None, None))
            .collect();
        let path = root.join(DATABASE_FILENAME);
        match fs::symlink_metadata(&path) {
            Ok(found) if found.is_file() => {}
            Ok(_) => {
                findings.push(finding("unsafe-database", DATABASE_FILENAME, None, None));
                return Ok(finished(true, findings));
            }
            Err(err) if err.kind() == ErrorKind::NotFound => return Ok(finished(false, findings)),
            Err(_) => return Err(CoreError::StorageFailure),
        }
        // the settings of a read of the database (write access, so that a hot journal a
        // crashed writer left can be rolled back; query_only keeps it from writing) --
        // not `Database::read`, which would turn an unknown schema into a failure
        // instead of a finding
        let patience = Duration::from_millis(self.busy_timeout_ms.min(i32::MAX as u64));
        let conn = database::open(&path, patience, true)?;
        conn.execute_batch("PRAGMA foreign_keys = ON; PRAGMA query_only = ON")?;
        // one read transaction: schema, integrity, projects, counts and entries all come
        // from the same snapshot
        conn.execute_batch("BEGIN")?;
        let snapshot = snapshot(&conn, &mut findings);
        if !conn.is_autocommit() {
            conn.execute_batch("ROLLBACK")?;
        }
        drop(conn);
        match snapshot? {
            Snapshot::Finished { initialized } => Ok(finished(initialized, findings)),
            Snapshot::Read {
                initialized,
                entries,
                mut projects,
                incomplete_changes,
            } => {
                // the directory checks look at the file system, so they run only after
                // the transaction: a hung network mount must not hold the read lock and
                // block writers
                #[cfg(test)]
                meanwhile(1);
                root_states(&mut projects, &mut findings);
                root_pairs(&projects, &mut findings);
                Ok(StoreReport {
                    initialized,
                    entries,
                    projects,
                    findings: sorted(findings),
                    incomplete_changes,
                })
            }
        }
    }
}

fn snapshot(conn: &Connection, findings: &mut Vec<StoreFinding>) -> Result<Snapshot, CoreError> {
    let version: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    let objects: i64 =
        conn.query_row("SELECT count(*) FROM sqlite_master", [], |row| row.get(0))?;
    if version == 0 && objects == 0 {
        return Ok(Snapshot::Finished { initialized: false });
    }
    if version != SCHEMA_VERSION {
        // an older store too: nothing here brings one forward
        findings.push(finding("unknown-schema", DATABASE_FILENAME, None, None));
        return Ok(Snapshot::Finished { initialized: true });
    }
    if rows(conn, "PRAGMA integrity_check")? != [[Value::Text("ok".to_string())]] {
        findings.push(finding("integrity", DATABASE_FILENAME, None, None));
    }
    let orphans: BTreeSet<Raw> = rows(
        conn,
        "SELECT m.id FROM memories m LEFT JOIN projects p ON p.id = m.project_id \
         WHERE p.id IS NULL",
    )?
    .iter()
    .map(|row| raw(&row[0]))
    .collect();
    for memory_id in &orphans {
        findings.push(about_memory("orphan", shaped(memory_id), None));
    }
    let (projects, initialized) = projects(conn, findings)?;
    #[cfg(test)]
    meanwhile(0);
    let mut entries = Vec::new();
    let mut valid = Vec::new();
    // every row is validated, deleted ones too; only active valid rows are entries
    let found = rows(
        conn,
        &format!("SELECT {MEMORY_COLUMNS} FROM memories ORDER BY id"),
    )?;
    for row in &found {
        let memory_id = raw(&row[0]);
        if orphans.contains(&memory_id) {
            continue;
        }
        let Ok(memory) = memory_from_row(row) else {
            findings.push(about_memory("invalid-row", shaped(&memory_id), None));
            continue;
        };
        if memory.deleted_at.is_none() {
            entries.push(InspectedMemory {
                location_hint: format!("memories/{}", memory.id),
                memory: memory.clone(),
            });
        }
        valid.push(memory);
    }
    sessions(conn, findings)?;
    session_moves(conn, findings)?;
    let incomplete_changes = history(conn, findings, &valid)?;
    Ok(Snapshot::Read {
        initialized,
        entries,
        projects,
        incomplete_changes,
    })
}

/// Every valid project row with its counts, and whether a valid global one is among
/// them; each root's state is filled in later, outside the transaction.
fn projects(
    conn: &Connection,
    findings: &mut Vec<StoreFinding>,
) -> Result<(Vec<InspectedProject>, bool), CoreError> {
    let counts: HashMap<(Raw, Raw), i64> = rows(
        conn,
        "SELECT project_id, deleted_at IS NOT NULL, count(*) FROM memories \
         GROUP BY project_id, deleted_at IS NOT NULL",
    )?
    .iter()
    .map(|row| match row[2] {
        Value::Integer(count) => Ok(((raw(&row[0]), raw(&row[1])), count)),
        _ => Err(CoreError::StorageFailure),
    })
    .collect::<Result<_, _>>()?;
    let mut projects = Vec::new();
    let mut initialized = false;
    let found = rows(
        conn,
        &format!("SELECT {PROJECT_COLUMNS} FROM projects ORDER BY is_global, name, id"),
    )?;
    for row in &found {
        let Ok((project, is_global)) = project_from_row(row) else {
            let id = shaped(&raw(&row[0]));
            let location = match &id {
                Some(id) => format!("projects/{id}"),
                None => "projects".to_string(),
            };
            findings.push(finding("invalid-row", location, id, None));
            continue;
        };
        initialized = initialized || is_global;
        let count = |deleted: i64| {
            let key = (Raw::Text(project.id.clone()), Raw::Integer(deleted));
            counts.get(&key).copied().unwrap_or(0)
        };
        projects.push(InspectedProject {
            active_memories: count(0),
            deleted_memories: count(1),
            id: project.id,
            name: project.name,
            root: project.root,
            is_global,
            root_state: RootState::Unbound,
        });
    }
    Ok((projects, initialized))
}

/// Session-row findings only: the doctor's own entries and counts stay memory-only.
fn sessions(conn: &Connection, findings: &mut Vec<StoreFinding>) -> Result<(), CoreError> {
    let orphans = rows(
        conn,
        "SELECT s.harness, s.session_id FROM sessions s \
         LEFT JOIN projects p ON p.id = s.project_id \
         WHERE s.project_id IS NOT NULL AND p.id IS NULL \
         UNION \
         SELECT s.harness, s.session_id FROM sessions s \
         LEFT JOIN projects p ON p.id = s.candidate_id \
         WHERE s.candidate_id IS NOT NULL AND p.id IS NULL",
    )?;
    let mut orphaned = HashSet::new();
    for row in &orphans {
        findings.push(finding(
            "session-orphan",
            session_location(&row[0], &row[1], "sessions"),
            None,
            None,
        ));
        orphaned.insert((raw(&row[0]), raw(&row[1])));
    }
    let found = rows(
        conn,
        &format!("SELECT {SESSION_COLUMNS} FROM sessions ORDER BY harness, session_id"),
    )?;
    for row in &found {
        // one already filed as an orphan is not an invalid row too
        if !orphaned.contains(&(raw(&row[0]), raw(&row[1]))) && session_from_row(row).is_err() {
            findings.push(finding(
                "invalid-row",
                session_location(&row[0], &row[1], "sessions"),
                None,
                None,
            ));
        }
    }
    Ok(())
}

/// A move row that does not decode, or whose session is gone.
fn session_moves(conn: &Connection, findings: &mut Vec<StoreFinding>) -> Result<(), CoreError> {
    let found = rows(
        conn,
        &format!(
            "SELECT {SESSION_MOVE_COLUMNS} FROM session_moves ORDER BY harness, session_id, seq"
        ),
    )?;
    let mut held = conn.prepare("SELECT 1 FROM sessions WHERE harness = ?1 AND session_id = ?2")?;
    for row in &found {
        if session_move_from_row(row).is_err() || !held.exists((&row[0], &row[1]))? {
            findings.push(finding(
                "session-move-damaged",
                session_location(&row[0], &row[1], "session_moves"),
                None,
                None,
            ));
        }
    }
    Ok(())
}

/// History findings for every valid memory; the ids of incomplete changes.
///
/// Invalid and orphaned `memories` rows are already findings of their own and are not
/// compared again; every `memory_versions` row of a valid memory is still decoded and
/// checked, history under a healthy current row included.
fn history(
    conn: &Connection,
    findings: &mut Vec<StoreFinding>,
    memories: &[Memory],
) -> Result<Vec<String>, CoreError> {
    // every memory's project, raw: a source finding names the citing memory's project
    // even when that memory is not one of the valid `memories`
    let project_of: HashMap<Raw, Raw> = rows(conn, "SELECT id, project_id FROM memories")?
        .iter()
        .map(|row| (raw(&row[0]), raw(&row[1])))
        .collect();
    let project_of = |memory_id: &Raw| project_of.get(memory_id).and_then(shaped);
    // per memory: (version, type .. tags, change_id), oldest first
    let mut versions: HashMap<Raw, Vec<Vec<Value>>> = HashMap::new();
    for mut row in rows(
        conn,
        "SELECT memory_id, version, type, trust, sync, description, body, deleted, level, \
         tags, change_id FROM memory_versions ORDER BY memory_id, version",
    )? {
        let state = row.split_off(1);
        versions.entry(raw(&row[0])).or_default().push(state);
    }
    let change_ids: HashSet<Raw> = rows(conn, "SELECT change_id FROM changes")?
        .iter()
        .map(|row| raw(&row[0]))
        .collect();
    let steps: HashSet<[Raw; 3]> = rows(
        conn,
        "SELECT change_id, memory_id, after_version FROM change_steps",
    )?
    .iter()
    .map(|row| [raw(&row[0]), raw(&row[1]), raw(&row[2])])
    .collect();
    for memory in memories {
        let id = Raw::Text(memory.id.clone());
        let stored = versions.get(&id).map_or(&[][..], Vec::as_slice);
        let about = |kind| {
            about_memory(
                kind,
                Some(memory.id.clone()),
                Some(memory.project_id.clone()),
            )
        };
        // a gap is judged from the rows that are stored -- never by counting up to the
        // memory's own current version, which may be damaged and arbitrarily large
        let numbered = stored
            .iter()
            .zip(1..)
            .all(|(row, position)| row[0] == Value::Integer(position));
        let ends = stored.last().map(|row| &row[0]) == Some(&Value::Integer(memory.version));
        if !(numbered && ends) {
            findings.push(about("version-gap"));
            continue;
        }
        // every stored version, not only the latest, must still read as a version
        let Ok(states) = stored
            .iter()
            .map(|row| history_state(&row[1..9]))
            .collect::<Result<Vec<_>, _>>()
        else {
            findings.push(about("invalid-row"));
            continue;
        };
        if states.last().is_some_and(|latest| !latest.is_of(memory)) {
            findings.push(about("version-mismatch"));
        }
        // a version that names a change needs both the change's row and its step
        let recorded = |row: &Vec<Value>| {
            let change_id = raw(&row[9]);
            change_id == Raw::Null
                || (change_ids.contains(&change_id)
                    && steps.contains(&[change_id, id.clone(), raw(&row[0])]))
        };
        if !stored.iter().all(recorded) {
            findings.push(about("unrecorded-version"));
        }
    }
    for row in rows(
        conn,
        "SELECT DISTINCT s.memory_id FROM memory_sources s \
         LEFT JOIN memory_versions vc \
         ON vc.memory_id = s.memory_id AND vc.version = s.version \
         LEFT JOIN memory_versions vs \
         ON vs.memory_id = s.source_id AND vs.version = s.source_version \
         WHERE vc.memory_id IS NULL OR vs.memory_id IS NULL",
    )? {
        let memory_id = raw(&row[0]);
        findings.push(about_memory(
            "dangling-source",
            shaped(&memory_id),
            project_of(&memory_id),
        ));
    }
    let mut edges: HashMap<Raw, HashSet<Raw>> = HashMap::new();
    for row in rows(
        conn,
        "SELECT DISTINCT memory_id, source_id FROM memory_sources",
    )? {
        edges.entry(raw(&row[0])).or_default().insert(raw(&row[1]));
    }
    let mut looped = on_cycles(&edges);
    looped.sort_by_cached_key(|memory_id| written(memory_id));
    for memory_id in looped {
        findings.push(about_memory(
            "source-cycle",
            shaped(memory_id),
            project_of(memory_id),
        ));
    }
    let mut incomplete = Vec::new();
    for row in rows(
        conn,
        "SELECT c.change_id FROM changes c LEFT JOIN change_steps s \
         ON s.change_id = c.change_id GROUP BY c.change_id \
         HAVING count(s.change_id) < c.step_count ORDER BY c.change_id",
    )? {
        match shaped(&raw(&row[0])) {
            Some(change_id) => incomplete.push(change_id),
            // an id no write makes is never handed on as one
            None => findings.push(finding("invalid-row", "changes", None, None)),
        }
    }
    Ok(incomplete)
}

fn about_root(kind: &str, project: &InspectedProject) -> StoreFinding {
    finding(
        kind,
        format!("projects/{}", project.id),
        Some(project.id.clone()),
        None,
    )
}

/// Each bound root's state, from the file system.
fn root_states(projects: &mut [InspectedProject], findings: &mut Vec<StoreFinding>) {
    for project in projects {
        let Some(root) = &project.root else {
            continue;
        };
        project.root_state = match directories::root_state(root) {
            directories::RootState::Ok => RootState::Ok,
            directories::RootState::Missing => RootState::Missing,
            directories::RootState::NotCanonical => RootState::NotCanonical,
            directories::RootState::Unverifiable => RootState::Unverifiable,
        };
        match project.root_state {
            RootState::NotCanonical => findings.push(about_root("non-canonical-root", project)),
            RootState::Unverifiable => findings.push(about_root("unverifiable-root", project)),
            _ => {}
        }
    }
}

/// Every two roots that are one directory under two names.
///
/// The resolver pools exact and alias matches; a physical alias between two projects'
/// roots degrades it, so doctor must name it. A root already unverifiable on its own was
/// reported once; pairing it further would blame a healthy neighbour, so such a project
/// sits out every pair, and a pair that only turns unverifiable on the comparison itself
/// is filed once per project, ever.
fn root_pairs(projects: &[InspectedProject], findings: &mut Vec<StoreFinding>) {
    let bound: Vec<(&InspectedProject, &Path)> = projects
        .iter()
        .filter(|project| project.root_state != RootState::Unverifiable)
        .filter_map(|project| Some((project, Path::new(project.root.as_deref()?))))
        .collect();
    let mut unverifiable = HashSet::new();
    for (index, (first, first_root)) in bound.iter().enumerate() {
        for (second, second_root) in &bound[index + 1..] {
            match directories::same_directory(first_root, second_root) {
                None => {
                    for project in [first, second] {
                        if unverifiable.insert(&project.id) {
                            findings.push(about_root("unverifiable-root", project));
                        }
                    }
                }
                Some(true) => findings.push(about_root("root-conflict", second)),
                Some(false) => {}
            }
        }
    }
}

#[cfg(test)]
mod tests;
