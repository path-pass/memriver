//! The one SQLite database behind every store: connecting, schema, rows.
//!
//! One connection per operation, closed at its end: a long-lived server never holds a
//! handle across a file that was replaced. Reads never create anything; the first
//! creating write makes the directory, the file and the schema, deciding "fresh or not"
//! only after it holds the write lock, so two first writers cannot both create the
//! schema.
//!
//! A read opens the existing file for reading and writing and then turns `query_only`
//! on: a writer that crashed in the middle of a transaction leaves a hot journal, and
//! only a connection with write access can roll it back. Such an open never creates a
//! database, so reads still create nothing, and `query_only` keeps the connection itself
//! from writing.
//!
//! Only schema version 6 is used. An older store is refused with its version and is
//! never read, written or brought forward here.

use std::fs::{self, DirBuilder, OpenOptions};
use std::io::ErrorKind;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::config::DbConfig;
use rusqlite::types::{Value, ValueRef};
use rusqlite::{Connection, OpenFlags, Row};

use super::schema::{SCHEMA, SCHEMA_VERSION};
use crate::models::errors::{CoreError, ValueError};
use crate::models::helpers::{is_id, is_timestamp, py_normpath, single_line};
use crate::models::memory::{
    Memory, MemoryType, Trust, checked_level, tags_from_json, tags_to_json,
};
use crate::models::project::Project;
use crate::repository::directories::DATABASE_FILENAME;
use crate::settings::{BUSY_TIMEOUT_MS, lexical_absolute};

/// The column order of every memory SELECT and INSERT, which the row codec follows.
pub const MEMORY_COLUMNS: &str = "id, project_id, type, source_harness, source_method, trust, \
                                  sync, description, body, created, updated, version, \
                                  deleted_at, last_read_at, level, tags";
pub const PROJECT_COLUMNS: &str = "id, name, root, is_global";

/// Whatever the driver reports while we talk to the file is a storage failure: its
/// message, SQL and path do not cross this boundary. A store that wants to name a
/// constraint it hit looks at the driver's error inside its body, before `?`.
impl From<rusqlite::Error> for CoreError {
    fn from(_: rusqlite::Error) -> CoreError {
        CoreError::StorageFailure
    }
}

// --- rows ---

/// A row as raw values. A TEXT value that is not UTF-8 comes back as the bytes it is:
/// a STRICT table does not stop a raw writer from planting one, and failing here would
/// turn one damaged row into the failure of the whole query. As bytes it fails the text
/// checks of the row codecs instead -- one invalid row.
pub fn values(row: &Row<'_>) -> rusqlite::Result<Vec<Value>> {
    (0..row.as_ref().column_count())
        .map(|idx| {
            Ok(match row.get_ref(idx)? {
                ValueRef::Null => Value::Null,
                ValueRef::Integer(number) => Value::Integer(number),
                ValueRef::Real(number) => Value::Real(number),
                ValueRef::Text(bytes) => match std::str::from_utf8(bytes) {
                    Ok(text) => Value::Text(text.to_string()),
                    Err(_) => Value::Blob(bytes.to_vec()),
                },
                ValueRef::Blob(bytes) => Value::Blob(bytes.to_vec()),
            })
        })
        .collect()
}

fn not_text() -> ValueError {
    ValueError::new("a text column holds something else")
}

/// The text of column `idx`, None for NULL; an error for anything else, a value that
/// is not UTF-8 and a column that is not there included.
pub fn text(row: &[Value], idx: usize) -> Result<Option<&str>, ValueError> {
    match row.get(idx) {
        Some(Value::Null) => Ok(None),
        Some(Value::Text(text)) => Ok(Some(text)),
        _ => Err(not_text()),
    }
}

fn required_text(row: &[Value], idx: usize) -> Result<&str, ValueError> {
    text(row, idx)?.ok_or_else(not_text)
}

fn optional_text(text: Option<&str>) -> Value {
    text.map_or(Value::Null, |text| Value::Text(text.to_string()))
}

pub fn memory_to_row(memory: &Memory) -> Vec<Value> {
    let text = |text: &str| Value::Text(text.to_string());
    vec![
        text(&memory.id),
        text(&memory.project_id),
        text(memory.type_.as_str()),
        text(&memory.source_harness),
        text(&memory.source_method),
        text(memory.trust.as_str()),
        Value::Integer(i64::from(memory.sync)),
        text(&memory.description),
        text(&memory.body),
        text(&memory.created),
        text(&memory.updated),
        Value::Integer(memory.version),
        optional_text(memory.deleted_at.as_deref()),
        optional_text(memory.last_read_at.as_deref()),
        memory.level.map_or(Value::Null, Value::Integer),
        text(&tags_to_json(&memory.tags)),
    ]
}

/// A validated memory; an error for a row memriver could not have written.
///
/// Table constraints do not replace this: `length(id) = 10` accepts `../../evil`, and a
/// writer with foreign keys off is not stopped at all.
pub fn memory_from_row(row: &[Value]) -> Result<Memory, ValueError> {
    if row.len() != 16 {
        return Err(ValueError::new("a memory row has sixteen columns"));
    }
    let need = |idx| required_text(row, idx);
    let (id, project_id, type_, harness, method, trust) =
        (need(0)?, need(1)?, need(2)?, need(3)?, need(4)?, need(5)?);
    let (description, body, created, updated) = (need(7)?, need(8)?, need(9)?, need(10)?);
    if !(is_id(id) && is_id(project_id)) {
        return Err(ValueError::new("an id is not addressable"));
    }
    let (Some(type_), Some(trust)) = (MemoryType::parse(type_), Trust::parse(trust)) else {
        return Err(ValueError::new("unknown type or trust"));
    };
    let sync = match row[6] {
        Value::Integer(0) => false,
        Value::Integer(1) => true,
        _ => return Err(ValueError::new("sync is not 0 or 1")),
    };
    let version = match row[11] {
        Value::Integer(version) if version >= 1 => version,
        _ => return Err(ValueError::new("version is not a positive integer")),
    };
    let deleted_at = text(row, 12).map_err(|_| ValueError::new("deleted_at is not text"))?;
    let last_read_at = match text(row, 13) {
        Ok(None) => None,
        Ok(Some(at)) if is_timestamp(at) => Some(at),
        _ => return Err(ValueError::new("last_read_at is not a timestamp")),
    };
    let level = match row[14] {
        Value::Null => None,
        Value::Integer(level) => Some(level),
        _ => {
            return Err(ValueError::new(
                "a level is None or an integer of at least 1",
            ));
        }
    };
    let Value::Text(tags) = &row[15] else {
        return Err(ValueError::new("tags is not text"));
    };
    Ok(Memory {
        id: id.to_string(),
        project_id: project_id.to_string(),
        type_,
        source_harness: harness.to_string(),
        source_method: method.to_string(),
        trust,
        sync,
        created: created.to_string(),
        updated: updated.to_string(),
        description: description.to_string(),
        body: body.to_string(),
        version,
        deleted_at: deleted_at.map(str::to_string),
        last_read_at: last_read_at.map(str::to_string),
        level: checked_level(level)?,
        tags: tags_from_json(tags)?,
    })
}

/// A validated project and whether it is the global one; an error for an impossible row.
pub fn project_from_row(row: &[Value]) -> Result<(Project, bool), ValueError> {
    let [id, name, root, is_global] = row else {
        return Err(ValueError::new("a project row has four columns"));
    };
    let id = match id {
        Value::Text(id) if is_id(id) => id,
        _ => return Err(ValueError::new("project id is not addressable")),
    };
    let name = match name {
        Value::Text(name) if !name.is_empty() && single_line(name) == *name => name,
        _ => return Err(ValueError::new("project name is not one non-empty line")),
    };
    // lexical shape only: whether the directory still exists or was pointed elsewhere
    // is the directory rules' question, and a root that is offline stays valid.
    // Normalizing keeps a leading "//", so that is refused on its own
    let canonical = |root: &str| {
        root.starts_with('/')
            && !root.starts_with("//")
            && !root.contains('\0')
            && py_normpath(root.as_bytes()) == root.as_bytes()
    };
    let root = match root {
        Value::Null => None,
        Value::Text(root) if canonical(root) => Some(root.clone()),
        _ => {
            return Err(ValueError::new(
                "root is not an absolute, canonical-shaped, addressable path",
            ));
        }
    };
    let is_global = match is_global {
        Value::Integer(0) => false,
        Value::Integer(1) => true,
        _ => return Err(ValueError::new("is_global is not 0 or 1")),
    };
    let project = Project {
        id: id.clone(),
        name: name.clone(),
        root,
    };
    Ok((project, is_global))
}

// --- the file and its connections ---

/// Whether the database is a file without a schema. Anything that is neither that nor a
/// version 6 store is an error: an older store with its version, the rest as unknown.
fn is_fresh(conn: &Connection) -> Result<bool, CoreError> {
    let version: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    let objects = || {
        conn.query_row("SELECT count(*) FROM sqlite_master", [], |row| {
            row.get::<_, i64>(0)
        })
    };
    match version {
        SCHEMA_VERSION => Ok(false),
        0 if objects()? == 0 => Ok(true),
        // an older store is refused, never read or written in place
        1..SCHEMA_VERSION => Err(CoreError::StoreNeedsUpgrade(version)),
        _ => Err(CoreError::StorageFailure),
    }
}

fn rollback(conn: &Connection) -> Result<(), CoreError> {
    if !conn.is_autocommit() {
        conn.execute_batch("ROLLBACK")?;
    }
    Ok(())
}

/// Whether a database file stands at `path`; a storage failure for anything but a
/// regular file. A symlink is never followed: its target is chosen outside the store,
/// and SQLite would open whatever it points at.
fn is_file(path: &Path) -> Result<bool, CoreError> {
    match fs::symlink_metadata(path) {
        Ok(found) if found.is_file() => Ok(true),
        Err(err) if err.kind() == ErrorKind::NotFound => Ok(false),
        _ => Err(CoreError::StorageFailure),
    }
}

/// The store's directory and an empty database file at `path`, unless they are there.
fn create_file(path: &Path) -> Result<(), CoreError> {
    let made = || -> std::io::Result<()> {
        let root = path.parent().unwrap_or(Path::new("/"));
        // the mode is the leaf's alone; a directory that is there keeps its own
        if let Some(above) = root.parent() {
            fs::create_dir_all(above)?;
        }
        match DirBuilder::new().mode(0o700).create(root) {
            Err(err) if err.kind() == ErrorKind::AlreadyExists && root.is_dir() => {}
            made => made?,
        }
        let created = OpenOptions::new()
            .write(true)
            .create_new(true)
            .custom_flags(libc::O_NOFOLLOW)
            .mode(0o600)
            .open(path);
        match created {
            // a peer, or an earlier run, created it: use that file
            Err(err) if err.kind() == ErrorKind::AlreadyExists => Ok(()),
            created => created.map(drop),
        }
    };
    made().map_err(|_| CoreError::StorageFailure)?;
    // whatever stands there now must be a regular file
    is_file(path).map(drop)
}

#[cfg(test)]
thread_local! {
    static BEFORE_OPEN: std::cell::RefCell<Option<Box<dyn FnOnce()>>> =
        const { std::cell::RefCell::new(None) };
}

/// For a test, on its thread, once: `then` runs when a database file has been checked
/// and is about to be opened -- where someone else can put another file in its place.
#[cfg(test)]
pub(crate) fn before_open(then: impl FnOnce() + 'static) {
    BEFORE_OPEN.set(Some(Box::new(then)));
}

/// A connection to the existing database file at `path`, which is absolute: never
/// creates it, never read-only (a hot journal can be rolled back only with write
/// access).
///
/// `reading`: the connection of something that only reads. It still rolls back what a
/// crashed writer left in a rollback journal -- nothing can be read before that.
pub(crate) fn open(
    path: &Path,
    busy_timeout: Duration,
    reading: bool,
) -> Result<Connection, CoreError> {
    // The file was checked a moment ago, by name; whatever stands at that name now is
    // what would be opened. SQLite refuses a link when it is told to follow none -- but
    // then it refuses one anywhere in the name, and a store may lie below a linked
    // directory or be one. So the directory is resolved first and the file named in it:
    // a link at the file itself, or one that takes a directory's place after this
    // line, fails the open
    let (Some(directory), Some(name)) = (path.parent(), path.file_name()) else {
        return Err(CoreError::StorageFailure);
    };
    let directory = fs::canonicalize(directory).map_err(|_| CoreError::StorageFailure)?;
    #[cfg(test)]
    if let Some(then) = BEFORE_OPEN.take() {
        then();
    }
    let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
        | OpenFlags::SQLITE_OPEN_NO_MUTEX
        | OpenFlags::SQLITE_OPEN_NOFOLLOW;
    let conn = Connection::open_with_flags(directory.join(name), flags)?;
    conn.busy_timeout(busy_timeout)?;
    if reading {
        // the last connection to a store in WAL mode writes the log back into the
        // database when it closes, `query_only` or not: a reader leaves that to a writer
        conn.set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)?;
    }
    Ok(conn)
}

pub struct Database {
    /// As given: where it lies is decided anew by every operation.
    path: PathBuf,
    busy_timeout: Duration,
}

impl Database {
    pub fn new(root: &Path) -> Database {
        Database::with_busy_timeout(root, BUSY_TIMEOUT_MS)
    }

    /// For a test that must not wait the whole `BUSY_TIMEOUT_MS` for a lock. The driver
    /// counts milliseconds in 32 bits: a longer wait is the longest it can hold.
    pub fn with_busy_timeout(root: &Path, busy_timeout_ms: u64) -> Database {
        Database {
            path: root.join(DATABASE_FILENAME),
            busy_timeout: Duration::from_millis(busy_timeout_ms.min(i32::MAX as u64)),
        }
    }

    /// The one place of the database file for a whole operation: absolute and
    /// normalized by its text, by the rule the settings file is found by. The check, the creation and the connection all name
    /// this path, so the file that was checked is the file that is opened -- a root
    /// written through a link and `..` cannot have one file examined and another used.
    fn located(&self) -> Result<PathBuf, CoreError> {
        lexical_absolute(&self.path).map_err(|_| CoreError::StorageFailure)
    }

    /// Whether the database file is there; a storage failure for anything but a regular
    /// file, a symlink included.
    pub fn exists(&self) -> Result<bool, CoreError> {
        is_file(&self.located()?)
    }

    /// The store's schema version as stored, None without a database file. Refuses no
    /// version: the caller decides what the number means. Writes nothing of its own;
    /// what a crashed writer left unfinished is rolled back first, as on every open.
    pub fn schema_version(&self) -> Result<Option<i64>, CoreError> {
        let path = self.located()?;
        if !is_file(&path)? {
            return Ok(None);
        }
        let conn = self.connect(&path, true)?;
        Ok(Some(
            conn.query_row("PRAGMA user_version", [], |row| row.get(0))?,
        ))
    }

    /// Runs `body` on a read-only connection inside one read transaction, or on None
    /// for an empty store (no file, or a file without a schema).
    ///
    /// The schema check and the body share one snapshot, so a peer's first write
    /// cannot land between them. The body's own error comes back as it is.
    pub fn read<T>(
        &self,
        body: impl FnOnce(Option<&Connection>) -> Result<T, CoreError>,
    ) -> Result<T, CoreError> {
        let path = self.located()?;
        if !is_file(&path)? {
            return body(None);
        }
        let conn = self.connect(&path, true)?;
        let result = conn
            .execute_batch("BEGIN")
            .map_err(CoreError::from)
            .and_then(|()| is_fresh(&conn))
            .and_then(|fresh| body(if fresh { None } else { Some(&conn) }));
        // nothing to keep: the connection is query_only
        rollback(&conn)?;
        result
    }

    /// Runs `body` inside one `BEGIN IMMEDIATE` transaction and commits; with `create`
    /// the directory, the file and the schema are made on demand.
    ///
    /// Any error rolls the transaction back. The body's own error comes back as it is;
    /// a failed commit (a deferred key, say) is a storage failure.
    ///
    /// Without `create` nothing is ever created: a missing store -- one that
    /// disappears between the check and the connection included -- is a storage
    /// failure.
    pub fn write<T>(
        &self,
        create: bool,
        body: impl FnOnce(&Connection) -> Result<T, CoreError>,
    ) -> Result<T, CoreError> {
        let path = self.located()?;
        if !is_file(&path)? {
            if !create {
                return Err(CoreError::StorageFailure);
            }
            create_file(&path)?;
        }
        let conn = self.connect(&path, false)?;
        let run = || {
            conn.execute_batch("BEGIN IMMEDIATE")?;
            // decided under the write lock: a peer that made the schema since we
            // looked is seen here, never made again
            if is_fresh(&conn)? {
                for statement in SCHEMA {
                    conn.execute_batch(statement)?;
                }
                conn.execute_batch(&format!("PRAGMA user_version = {SCHEMA_VERSION}"))?;
            }
            let value = body(&conn)?;
            conn.execute_batch("COMMIT")?;
            Ok(value)
        };
        let result = run();
        if result.is_err() {
            rollback(&conn)?;
        }
        result
    }

    /// A connection to the existing file at `path`, which is absolute: never creates
    /// it, never read-only.
    fn connect(&self, path: &Path, read_only: bool) -> Result<Connection, CoreError> {
        let conn = open(path, self.busy_timeout, read_only)?;
        // secure_delete: a hard delete's freed pages are overwritten with zeros instead
        // of holding the plaintext until something else reuses them
        conn.execute_batch("PRAGMA foreign_keys = ON; PRAGMA secure_delete = ON")?;
        if read_only {
            conn.execute_batch("PRAGMA query_only = ON")?;
        }
        Ok(conn)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use memriver_test_support::Sandbox;

    #[test]
    fn a_connection_never_creates_the_file() {
        // what a write without `create` meets when the store went away after its check
        let sandbox = Sandbox::new();
        let database = Database::new(&sandbox.root);
        let path = database.located().unwrap();
        for read_only in [true, false] {
            assert!(matches!(
                database.connect(&path, read_only),
                Err(CoreError::StorageFailure)
            ));
        }
        assert!(!sandbox.root.exists());
        fs::create_dir(&sandbox.root).unwrap();
        assert!(database.connect(&path, false).is_err());
        assert_eq!(fs::read_dir(&sandbox.root).unwrap().count(), 0);
    }

    /// A store with one project row, and beside it another store whose project has
    /// another name: (the first store's root, the other's database file).
    fn two_stores(sandbox: &Sandbox) -> (PathBuf, PathBuf) {
        let other = sandbox.dir.path().join("other");
        for (root, name) in [(&sandbox.root, "mine"), (&other, "theirs")] {
            Database::new(root)
                .write(true, |conn| {
                    conn.execute(
                        "INSERT INTO projects (id, name) VALUES ('pppppppppp', ?1)",
                        [name],
                    )?;
                    Ok(())
                })
                .unwrap();
        }
        (sandbox.root.clone(), other.join(DATABASE_FILENAME))
    }

    /// From now on, once: when a database has been checked and is about to be opened,
    /// the file at `mine` is moved away and a link to `theirs` stands in its place.
    fn swap_before_the_open(mine: &Path, theirs: &Path) {
        let (mine, theirs) = (mine.join(DATABASE_FILENAME), theirs.to_path_buf());
        before_open(move || {
            fs::rename(&mine, mine.with_extension("moved")).unwrap();
            std::os::unix::fs::symlink(&theirs, &mine).unwrap();
        });
    }

    #[test]
    fn a_database_swapped_for_a_link_after_its_check_is_not_opened() {
        let name = |conn: Option<&Connection>| {
            let conn = conn.expect("a store");
            Ok(conn.query_row("SELECT name FROM projects", [], |row| {
                row.get::<_, String>(0)
            })?)
        };
        // a read, the version alone, and both kinds of write
        for way in 0..4 {
            let sandbox = Sandbox::new();
            let (root, theirs) = two_stores(&sandbox);
            let database = Database::new(&root);
            let before = fs::read(&theirs).unwrap();
            assert_eq!(database.read(name).unwrap(), "mine");
            swap_before_the_open(&root, &theirs);
            let mut ran = false;
            let refused = match way {
                0 => database.read(|_| {
                    ran = true;
                    Ok(())
                }),
                1 => database.schema_version().map(drop),
                _ => database.write(way == 3, |conn| {
                    ran = true;
                    conn.execute("UPDATE projects SET name = 'written'", [])?;
                    Ok(())
                }),
            };
            assert_eq!(refused, Err(CoreError::StorageFailure), "way {way}");
            assert!(!ran, "way {way}");
            // the store the link leads to was not touched, and nothing stands beside it
            assert_eq!(fs::read(&theirs).unwrap(), before, "way {way}");
            assert_eq!(fs::read_dir(theirs.parent().unwrap()).unwrap().count(), 1);
        }
    }

    #[test]
    fn a_store_directory_swapped_for_a_link_after_its_check_is_not_opened() {
        let sandbox = Sandbox::new();
        let (root, theirs) = two_stores(&sandbox);
        let before = fs::read(&theirs).unwrap();
        let (mine, other) = (root.clone(), theirs.parent().unwrap().to_path_buf());
        before_open(move || {
            fs::rename(&mine, mine.with_extension("moved")).unwrap();
            std::os::unix::fs::symlink(&other, &mine).unwrap();
        });
        let database = Database::new(&root);
        assert_eq!(database.read(|_| Ok(())), Err(CoreError::StorageFailure));
        assert!(fs::read(&theirs).unwrap() == before);
        // from the next operation on the link is where the store is, as any alias is
        assert_eq!(database.schema_version(), Ok(Some(SCHEMA_VERSION)));
    }

    #[test]
    fn a_store_reached_through_a_linked_directory_is_still_opened() {
        // the directory may be an alias; only the database file itself may not be one
        let sandbox = Sandbox::new();
        let (root, _) = two_stores(&sandbox);
        let alias = sandbox.dir.path().join("alias");
        std::os::unix::fs::symlink(&root, &alias).unwrap();
        let database = Database::new(&alias);
        database
            .write(false, |conn| {
                conn.execute("UPDATE projects SET name = 'through the alias'", [])?;
                Ok(())
            })
            .unwrap();
        let name = Database::new(&root).read(|conn| {
            let conn = conn.expect("a store");
            Ok(conn.query_row("SELECT name FROM projects", [], |row| {
                row.get::<_, String>(0)
            })?)
        });
        assert_eq!(name.unwrap(), "through the alias");
        assert_eq!(database.schema_version(), Ok(Some(SCHEMA_VERSION)));
    }

    #[test]
    fn a_relative_store_is_opened_from_the_working_directory() {
        let here = std::env::current_dir().unwrap();
        assert_eq!(
            lexical_absolute(Path::new("a/./b/../c")).unwrap(),
            here.join("a/c")
        );
        assert_eq!(
            lexical_absolute(Path::new("/a//b/../c/")).unwrap(),
            Path::new("/a/c")
        );
    }
}
