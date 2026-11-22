//! The one database file: creation, schema, connections, row decoding, and the refusal
//! of anything unexpected.

use std::cell::Cell;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Barrier, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use memriver_core::models::errors::CoreError;
use memriver_core::models::helpers::{new_id, now};
use memriver_core::models::memory::{Memory, MemoryType, Trust};
use memriver_core::repository::sqlite::database::{
    DATABASE_FILENAME, Database, MEMORY_COLUMNS, PROJECT_COLUMNS, memory_from_row, memory_to_row,
    project_from_row, text, values,
};
use memriver_core::repository::sqlite::schema::{SCHEMA, SCHEMA_VERSION};
use memriver_test_support::Sandbox;
use rusqlite::Connection;
use rusqlite::types::Value;

const TABLES: [&str; 11] = [
    "change_steps",
    "changes",
    "memories",
    "memory_reads",
    "memory_sources",
    "memory_versions",
    "projects",
    "session_moves",
    "sessions",
    "tags",
    "tool_calls",
];

fn file(root: &Path) -> PathBuf {
    root.join(DATABASE_FILENAME)
}

/// A connection of someone who is not memriver: no pragma, no check.
fn raw(root: &Path) -> Connection {
    Connection::open(file(root)).unwrap()
}

/// A sandbox whose store exists and is empty.
fn store() -> (Sandbox, Database) {
    let sandbox = Sandbox::new();
    let database = Database::new(&sandbox.root);
    database.write(true, |_| Ok(())).unwrap();
    (sandbox, database)
}

fn insert_project(conn: &Connection, id: &str, name: &str) -> Result<(), CoreError> {
    conn.execute(
        "INSERT INTO projects (id, name, root, is_global) VALUES (?1, ?2, NULL, 0)",
        (id, name),
    )?;
    Ok(())
}

fn failure<T>() -> Result<T, CoreError> {
    Err(CoreError::StorageFailure)
}

fn names_beside(root: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(root)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    names
}

fn mode(path: &Path) -> u32 {
    fs::metadata(path).unwrap().permissions().mode() & 0o7777
}

fn pragma(conn: &Connection, name: &str) -> i64 {
    conn.query_row(&format!("PRAGMA {name}"), [], |row| row.get(0))
        .unwrap()
}

fn set_version(root: &Path, version: i64) {
    raw(root)
        .execute_batch(&format!("PRAGMA user_version = {version}"))
        .unwrap();
}

// --- creation and schema ---

#[test]
fn fresh_store_gets_the_exact_schema() {
    let (sandbox, _) = store();
    let conn = raw(&sandbox.root);
    let stored: Vec<String> = conn
        .prepare("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid")
        .unwrap()
        .query_map([], |row| row.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(stored, SCHEMA);
    assert_eq!((SCHEMA.len(), SCHEMA_VERSION), (18, 6));
    assert_eq!(pragma(&conn, "user_version"), 6);
    let tables: Vec<String> = conn
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        .unwrap()
        .query_map([], |row| row.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(tables, TABLES);
    let extras: i64 = conn
        .query_row(
            "SELECT count(*) FROM sqlite_master WHERE type IN ('trigger', 'view')",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(extras, 0);
}

#[test]
fn first_creating_write_makes_root_0700_and_db_0600() {
    let sandbox = Sandbox::new();
    let root = sandbox.dir.path().join("a/b/store");
    Database::new(&root)
        .write(true, |conn| insert_project(conn, &new_id(), "g"))
        .unwrap();
    assert_eq!((mode(&root), mode(&file(&root))), (0o700, 0o600));
    // the leaf only: the directories above it are made as any other directory
    let plain = sandbox.dir.path().join("plain");
    fs::create_dir(&plain).unwrap();
    assert_eq!(mode(&sandbox.dir.path().join("a/b")), mode(&plain));
    assert_eq!(mode(&sandbox.dir.path().join("a")), mode(&plain));
    assert_eq!(names_beside(&root), [DATABASE_FILENAME]);
}

#[test]
fn a_root_that_exists_keeps_its_mode() {
    let sandbox = Sandbox::new();
    fs::create_dir(&sandbox.root).unwrap();
    fs::set_permissions(&sandbox.root, fs::Permissions::from_mode(0o750)).unwrap();
    Database::new(&sandbox.root)
        .write(true, |_| Ok(()))
        .unwrap();
    assert_eq!(
        (mode(&sandbox.root), mode(&file(&sandbox.root))),
        (0o750, 0o600)
    );
}

#[test]
fn a_read_never_creates_a_file() {
    let sandbox = Sandbox::new();
    let database = Database::new(&sandbox.root);
    assert_eq!(database.read(|conn| Ok(conn.is_none())), Ok(true));
    assert_eq!(database.exists(), Ok(false));
    assert_eq!(database.schema_version(), Ok(None));
    assert!(!sandbox.root.exists());
    // nor beside a directory that is there
    fs::create_dir(&sandbox.root).unwrap();
    assert_eq!(database.read(|conn| Ok(conn.is_none())), Ok(true));
    assert_eq!(names_beside(&sandbox.root), Vec::<String>::new());
}

#[test]
fn write_without_create_on_a_missing_store_is_storage_failure() {
    let sandbox = Sandbox::new();
    let database = Database::new(&sandbox.root);
    let ran = Cell::new(false);
    let refused = database.write(false, |_| {
        ran.set(true);
        Ok(())
    });
    assert_eq!(refused, Err(CoreError::StorageFailure));
    assert!(!ran.get() && !sandbox.root.exists());
    fs::create_dir(&sandbox.root).unwrap();
    assert_eq!(
        database.write(false, |_| Ok(())),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(names_beside(&sandbox.root), Vec::<String>::new());
}

#[test]
fn a_failed_first_write_leaves_no_schema() {
    let sandbox = Sandbox::new();
    let database = Database::new(&sandbox.root);
    // the body's own error comes back as it is
    let failed: Result<(), CoreError> = database.write(true, |conn| {
        insert_project(conn, &new_id(), "lost")?;
        Err(CoreError::SessionMoved)
    });
    assert_eq!(failed, Err(CoreError::SessionMoved));
    assert_eq!(fs::metadata(file(&sandbox.root)).unwrap().len(), 0);
    // an empty file reads as an empty store
    assert_eq!(database.read(|conn| Ok(conn.is_none())), Ok(true));
    assert_eq!(database.schema_version(), Ok(Some(0)));
    // and the next creating write makes the schema
    database
        .write(false, |conn| insert_project(conn, &new_id(), "kept"))
        .unwrap();
    assert_eq!(pragma(&raw(&sandbox.root), "user_version"), 6);
}

#[test]
fn a_failed_later_write_is_rolled_back() {
    let (sandbox, database) = store();
    let kept = new_id();
    database
        .write(false, |conn| insert_project(conn, &kept, "kept"))
        .unwrap();
    let failed: Result<(), CoreError> = database.write(false, |conn| {
        conn.execute("UPDATE projects SET name = 'lost'", [])?;
        Err(CoreError::GlobalReadOnly)
    });
    assert_eq!(failed, Err(CoreError::GlobalReadOnly));
    let name: String = raw(&sandbox.root)
        .query_row("SELECT name FROM projects", [], |row| row.get(0))
        .unwrap();
    assert_eq!(name, "kept");
    assert_eq!(names_beside(&sandbox.root), [DATABASE_FILENAME]);
}

#[test]
fn two_first_writers_racing_from_nothing_both_succeed() {
    let sandbox = Sandbox::new();
    let barrier = Arc::new(Barrier::new(2));
    let writers: Vec<_> = ["a", "b"]
        .into_iter()
        .map(|name| {
            let (root, barrier) = (sandbox.root.clone(), Arc::clone(&barrier));
            thread::spawn(move || {
                barrier.wait();
                Database::new(&root).write(true, |conn| insert_project(conn, &new_id(), name))
            })
        })
        .collect();
    for writer in writers {
        assert_eq!(writer.join().unwrap(), Ok(()));
    }
    let count = Database::new(&sandbox.root).read(|conn| {
        Ok(conn
            .unwrap()
            .query_row("SELECT count(*) FROM projects", [], |row| {
                row.get::<_, i64>(0)
            })?)
    });
    assert_eq!(count, Ok(2));
}

#[test]
fn symlinked_database_is_refused() {
    for kind in ["symlink", "dangling", "directory"] {
        let (elsewhere, _) = store();
        let sandbox = Sandbox::new();
        fs::create_dir(&sandbox.root).unwrap();
        match kind {
            // a real store behind the link: it is refused because it is a link
            "symlink" => symlink(file(&elsewhere.root), file(&sandbox.root)).unwrap(),
            "dangling" => symlink(sandbox.dir.path().join("nothing"), file(&sandbox.root)).unwrap(),
            _ => fs::create_dir(file(&sandbox.root)).unwrap(),
        }
        let database = Database::new(&sandbox.root);
        assert_eq!(database.exists(), failure(), "{kind}");
        assert_eq!(database.read(|_| Ok(())), failure(), "{kind}");
        assert_eq!(database.write(true, |_| Ok(())), failure(), "{kind}");
        assert_eq!(database.write(false, |_| Ok(())), failure(), "{kind}");
        assert_eq!(database.schema_version(), failure(), "{kind}");
        assert!(!sandbox.dir.path().join("nothing").exists());
    }
}

// --- versions ---

#[test]
fn versions_1_to_5_need_upgrade_on_read_and_on_write_and_are_left_byte_identical() {
    for version in 1..=5 {
        let (sandbox, database) = store();
        database
            .write(false, |conn| insert_project(conn, &new_id(), "kept"))
            .unwrap();
        set_version(&sandbox.root, version);
        let before = fs::read(file(&sandbox.root)).unwrap();
        let refused = Err(CoreError::StoreNeedsUpgrade(version));
        let ran = Cell::new(false);
        assert_eq!(
            database.read(|_| {
                ran.set(true);
                Ok(())
            }),
            refused
        );
        assert_eq!(
            database.write(true, |_| {
                ran.set(true);
                Ok(())
            }),
            refused
        );
        assert_eq!(
            database.write(false, |_| {
                ran.set(true);
                Ok(())
            }),
            refused
        );
        assert!(!ran.get(), "version {version}");
        assert_eq!(database.schema_version(), Ok(Some(version)));
        assert_eq!(fs::read(file(&sandbox.root)).unwrap(), before);
        assert_eq!(names_beside(&sandbox.root), [DATABASE_FILENAME]);
    }
}

#[test]
fn user_version_is_set_only_when_a_fresh_store_is_created() {
    fn count(dir: &Path, token: &str) -> usize {
        let mut found = 0;
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                found += count(&path, token);
            } else if path.extension().is_some_and(|ext| ext == "rs") {
                found += fs::read_to_string(&path).unwrap().matches(token).count();
            }
        }
        found
    }
    // product code only: a test may set the number to build its fixture
    let crates = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let mut found = 0;
    for entry in fs::read_dir(crates).unwrap() {
        let src = entry.unwrap().path().join("src");
        if src.is_dir() {
            found += count(&src, "user_version =");
        }
    }
    assert_eq!(found, 1);
    // and the store's own version is still what it was created with after later writes
    let (sandbox, database) = store();
    database
        .write(false, |conn| insert_project(conn, &new_id(), "n"))
        .unwrap();
    assert_eq!(pragma(&raw(&sandbox.root), "user_version"), 6);
}

#[test]
fn version_7_and_nonempty_version_0_are_unknown() {
    let foreign = "CREATE TABLE other (x)";
    for (version, extra) in [(7, ""), (-1, ""), (0, foreign), (600, foreign)] {
        let sandbox = Sandbox::new();
        fs::create_dir(&sandbox.root).unwrap();
        raw(&sandbox.root).execute_batch(extra).unwrap();
        set_version(&sandbox.root, version);
        let before = fs::read(file(&sandbox.root)).unwrap();
        let database = Database::new(&sandbox.root);
        let failure = Err(CoreError::StorageFailure);
        assert_eq!(database.read(|_| Ok(())), failure, "{version}");
        assert_eq!(database.write(true, |_| Ok(())), failure, "{version}");
        assert_eq!(database.write(false, |_| Ok(())), failure, "{version}");
        assert_eq!(database.schema_version(), Ok(Some(version)));
        assert_eq!(fs::read(file(&sandbox.root)).unwrap(), before);
        assert_eq!(names_beside(&sandbox.root), [DATABASE_FILENAME]);
    }
}

#[test]
fn schema_version_reads_the_number_and_changes_nothing() {
    let (sandbox, database) = store();
    let before = fs::read(file(&sandbox.root)).unwrap();
    assert_eq!(database.schema_version(), Ok(Some(6)));
    assert_eq!(fs::read(file(&sandbox.root)).unwrap(), before);
    // a file that is no database at all
    fs::write(file(&sandbox.root), "not a database, not even close").unwrap();
    assert_eq!(database.schema_version(), Err(CoreError::StorageFailure));
    assert_eq!(database.read(|_| Ok(())), Err(CoreError::StorageFailure));
    assert_eq!(
        database.write(true, |_| Ok(())),
        Err(CoreError::StorageFailure)
    );
}

// --- connections ---

#[test]
fn read_connection_is_query_only_and_rw() {
    let (_sandbox, database) = store();
    let seen = database.read(|conn| {
        let conn = conn.expect("a store");
        Ok((
            pragma(conn, "query_only"),
            pragma(conn, "foreign_keys"),
            pragma(conn, "secure_delete"),
            conn.is_autocommit(),
        ))
    });
    // inside one read transaction
    assert_eq!(seen, Ok((1, 1, 1, false)));
    let refused = database.read(|conn| insert_project(conn.expect("a store"), &new_id(), "n"));
    assert_eq!(refused, Err(CoreError::StorageFailure));
    // the body's own error comes back as it is
    let own: Result<(), CoreError> = database.read(|_| Err(CoreError::SessionMoved));
    assert_eq!(own, Err(CoreError::SessionMoved));
    let count = database.read(|conn| {
        Ok(conn
            .unwrap()
            .query_row("SELECT count(*) FROM projects", [], |row| {
                row.get::<_, i64>(0)
            })?)
    });
    assert_eq!(count, Ok(0));
}

#[test]
fn write_connection_enforces_foreign_keys_and_a_driver_error_is_storage_failure() {
    let (sandbox, database) = store();
    let seen = database.write(false, |conn| {
        Ok((
            pragma(conn, "query_only"),
            pragma(conn, "foreign_keys"),
            pragma(conn, "secure_delete"),
            conn.is_autocommit(),
        ))
    });
    assert_eq!(seen, Ok((0, 1, 1, false)));
    // a memory of a project that is not there
    let orphan = database.write(false, |conn| {
        conn.execute(
            "INSERT INTO memories (id, project_id, type, source_harness, source_method, trust, \
             sync, description, body, created, updated, version) \
             VALUES (?1, ?2, 'user', 'h', 'agent', 'agent', 1, '', 'b', 'c', 'u', 1)",
            (new_id(), new_id()),
        )?;
        Ok(())
    });
    assert_eq!(orphan, Err(CoreError::StorageFailure));
    // a key checked only at the commit fails the commit, and nothing of the transaction stays
    let project = new_id();
    let deferred = database.write(false, |conn| {
        insert_project(conn, &project, "p")?;
        conn.execute_batch("PRAGMA defer_foreign_keys = ON")?;
        conn.execute(
            "INSERT INTO memory_sources (memory_id, version, source_id, source_version) \
             VALUES ('m', 1, 's', 1)",
            [],
        )?;
        Ok(())
    });
    assert_eq!(deferred, Err(CoreError::StorageFailure));
    let conn = raw(&sandbox.root);
    let rows = |table: &str| -> i64 {
        conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
            row.get(0)
        })
        .unwrap()
    };
    assert_eq!(
        (rows("projects"), rows("memories"), rows("memory_sources")),
        (0, 0, 0)
    );
    assert_eq!(names_beside(&sandbox.root), [DATABASE_FILENAME]);
}

#[test]
fn no_journal_mode_pragma_is_issued() {
    let (sandbox, database) = store();
    database
        .write(false, |conn| insert_project(conn, &new_id(), "n"))
        .unwrap();
    let mode: String = raw(&sandbox.root)
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .unwrap();
    assert_eq!(mode, "delete");
    assert_eq!(names_beside(&sandbox.root), [DATABASE_FILENAME]);
}

#[test]
fn a_session_summary_needs_its_time_and_the_reverse() {
    let (sandbox, _) = store();
    let conn = raw(&sandbox.root);
    let insert = "INSERT INTO sessions (harness, session_id, status, origin, entry_cwd, \
                  started_at, last_active_at, summary, summary_at) \
                  VALUES ('codex', 's', 'registered', 'start', '/w', 'a', 'b', ?1, ?2)";
    let at = "2026-09-27T00:00:00.000000Z";
    assert!(conn.execute(insert, (Some("s"), None::<&str>)).is_err());
    assert!(conn.execute(insert, (None::<&str>, Some(at))).is_err());
    assert_eq!(conn.execute(insert, (Some("s"), Some(at))), Ok(1));
}

/// A store at schema `version` as a writer that crashed in the middle of a transaction
/// leaves it: uncommitted pages in the database file and the journal that undoes them,
/// with nobody's lock. Its directory, the file as it was before that writer began, and
/// the id of the one project that was committed.
fn crashed(sandbox: &Sandbox, version: i64) -> (PathBuf, Vec<u8>, String) {
    let database = Database::new(&sandbox.root);
    let kept = new_id();
    database
        .write(true, |conn| insert_project(conn, &kept, "kept"))
        .unwrap();
    set_version(&sandbox.root, version);
    let committed = fs::read(file(&sandbox.root)).unwrap();
    // a tiny page cache forces the uncommitted pages into the database file
    let writer = raw(&sandbox.root);
    writer
        .execute_batch("PRAGMA cache_size = 1; BEGIN IMMEDIATE; UPDATE projects SET name = 'lost'")
        .unwrap();
    for i in 0..8 {
        insert_project(&writer, &format!("{i:010}"), &"x".repeat(100_000)).unwrap();
    }
    // the two files as a crash at this moment would leave them
    let crashed = sandbox.dir.path().join("crashed");
    fs::create_dir(&crashed).unwrap();
    let journal = format!("{DATABASE_FILENAME}-journal");
    fs::copy(file(&sandbox.root), file(&crashed)).unwrap();
    fs::copy(sandbox.root.join(&journal), crashed.join(&journal)).unwrap();
    // the uncommitted change really is in the copied file
    let dirty = fs::read(file(&crashed)).unwrap();
    assert!(dirty.len() > 400_000 && dirty != committed);
    (crashed, committed, kept)
}

#[test]
fn a_read_rolls_back_the_hot_journal_of_a_crashed_writer() {
    let sandbox = Sandbox::new();
    let (crashed, committed, kept) = crashed(&sandbox, 6);
    let rows = Database::new(&crashed).read(|conn| {
        let conn = conn.expect("a store");
        let mut statement = conn.prepare("SELECT id, name FROM projects")?;
        let rows = statement
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    });
    assert_eq!(rows, Ok(vec![(kept, "kept".to_string())]));
    assert_eq!(names_beside(&crashed), [DATABASE_FILENAME]);
    assert_eq!(fs::read(file(&crashed)).unwrap(), committed);
}

#[test]
fn an_older_store_with_a_hot_journal_is_refused_with_what_was_committed_intact() {
    // the journal is played back when the file is opened, before its version can be
    // read: the refusal comes after, and leaves the store as its last commit left it
    type Attempt = fn(&Database) -> Result<(), CoreError>;
    let attempts: [Attempt; 4] = [
        |database| database.read(|_| Ok(())),
        |database| database.write(false, |_| Ok(())),
        |database| database.write(true, |_| Ok(())),
        |database| {
            database
                .schema_version()
                .map(|version| assert_eq!(version, Some(5)))
        },
    ];
    for (nth, attempt) in attempts.into_iter().enumerate() {
        let sandbox = Sandbox::new();
        let (crashed, committed, kept) = crashed(&sandbox, 5);
        let database = Database::new(&crashed);
        let expected = if nth == 3 {
            Ok(())
        } else {
            Err(CoreError::StoreNeedsUpgrade(5))
        };
        assert_eq!(attempt(&database), expected, "attempt {nth}");
        assert_eq!(
            fs::read(file(&crashed)).unwrap(),
            committed,
            "attempt {nth}"
        );
        assert_eq!(names_beside(&crashed), [DATABASE_FILENAME]);
        let conn = raw(&crashed);
        let rows: (String, String, i64) = conn
            .query_row("SELECT id, name, count(*) FROM projects", [], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?))
            })
            .unwrap();
        assert_eq!(rows, (kept, "kept".to_string(), 1));
        assert_eq!(pragma(&conn, "user_version"), 5);
    }
}

#[test]
fn a_store_in_another_journal_mode_keeps_it() {
    let (sandbox, database) = store();
    let set: String = raw(&sandbox.root)
        .query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))
        .unwrap();
    assert_eq!(set, "wal");
    database
        .write(false, |conn| insert_project(conn, &new_id(), "n"))
        .unwrap();
    let count = database.read(|conn| {
        Ok(conn
            .unwrap()
            .query_row("SELECT count(*) FROM projects", [], |row| {
                row.get::<_, i64>(0)
            })?)
    });
    assert_eq!(count, Ok(1));
    assert_eq!(database.schema_version(), Ok(Some(6)));
    let mode: String = raw(&sandbox.root)
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .unwrap();
    assert_eq!(mode, "wal");
}

#[test]
fn a_wait_longer_than_the_driver_counts_is_its_longest() {
    let (sandbox, _) = store();
    for (asked, held) in [
        (2_147_483_648, 2_147_483_647),
        (u64::MAX, 2_147_483_647),
        (2_147_483_647, 2_147_483_647),
        (200, 200),
    ] {
        let database = Database::with_busy_timeout(&sandbox.root, asked);
        let read = database.read(|conn| Ok(pragma(conn.expect("a store"), "busy_timeout")));
        assert_eq!(read, Ok(held));
        assert_eq!(
            database.write(false, |conn| Ok(pragma(conn, "busy_timeout"))),
            Ok(held)
        );
        assert_eq!(database.schema_version(), Ok(Some(6)));
    }
}

// --- one path for the check, the creation and the connection ---

/// `<dir>/lex/link` leads to `<dir>/real/child`, so the root `<dir>/lex/link/../store`
/// is `<dir>/real/store` to the file system and `<dir>/lex/store` by its text.
fn two_places(sandbox: &Sandbox) -> (PathBuf, PathBuf, PathBuf) {
    let dir = sandbox.dir.path();
    fs::create_dir_all(dir.join("real/child")).unwrap();
    fs::create_dir(dir.join("lex")).unwrap();
    symlink(dir.join("real/child"), dir.join("lex/link")).unwrap();
    (
        dir.join("lex/link/../store"),
        dir.join("lex/store"),
        dir.join("real/store"),
    )
}

fn marked(root: &Path, mark: &str) {
    Database::new(root)
        .write(true, |conn| insert_project(conn, &new_id(), mark))
        .unwrap();
}

fn marks(database: &Database) -> Result<Option<Vec<String>>, CoreError> {
    database.read(|conn| {
        let Some(conn) = conn else { return Ok(None) };
        let mut statement = conn.prepare("SELECT name FROM projects ORDER BY name")?;
        let names = statement
            .query_map([], |row| row.get(0))?
            .collect::<Result<Vec<String>, _>>()?;
        Ok(Some(names))
    })
}

#[test]
fn the_store_that_is_checked_is_the_store_that_is_opened() {
    // two stores: every operation reads and writes the one the path names by its text
    let sandbox = Sandbox::new();
    let (root, by_text, by_links) = two_places(&sandbox);
    marked(&by_text, "by-text");
    marked(&by_links, "by-links");
    assert_eq!(
        fs::canonicalize(&root).unwrap(),
        fs::canonicalize(&by_links).unwrap()
    );
    let untouched = fs::read(file(&by_links)).unwrap();
    let database = Database::new(&root);
    assert_eq!(database.exists(), Ok(true));
    assert_eq!(marks(&database), Ok(Some(vec!["by-text".to_string()])));
    assert_eq!(
        database.write(false, |conn| insert_project(conn, &new_id(), "written")),
        Ok(())
    );
    assert_eq!(
        marks(&Database::new(&by_text)),
        Ok(Some(vec!["by-text".to_string(), "written".to_string()]))
    );
    assert_eq!(fs::read(file(&by_links)).unwrap(), untouched);
}

#[test]
fn a_link_where_the_database_is_opened_is_refused_whatever_stands_elsewhere() {
    // the file the path names by its text is a link to a third store; a regular
    // database stands where the file system would go. What is opened is what is checked
    let sandbox = Sandbox::new();
    let (root, by_text, by_links) = two_places(&sandbox);
    let third = sandbox.dir.path().join("third");
    marked(&third, "third");
    marked(&by_links, "by-links");
    fs::create_dir(&by_text).unwrap();
    symlink(file(&third), file(&by_text)).unwrap();
    let before = (
        fs::read(file(&third)).unwrap(),
        fs::read(file(&by_links)).unwrap(),
    );
    let database = Database::new(&root);
    assert_eq!(database.exists(), failure());
    assert_eq!(marks(&database), failure());
    assert_eq!(database.write(false, |_| Ok(())), failure());
    assert_eq!(database.write(true, |_| Ok(())), failure());
    assert_eq!(database.schema_version(), failure());
    let after = (
        fs::read(file(&third)).unwrap(),
        fs::read(file(&by_links)).unwrap(),
    );
    assert_eq!(after, before);
}

#[test]
fn a_store_is_looked_for_and_created_where_it_will_be_opened() {
    // only the other place has a store: by its text the path names none
    let sandbox = Sandbox::new();
    let (root, by_text, by_links) = two_places(&sandbox);
    marked(&by_links, "by-links");
    let untouched = fs::read(file(&by_links)).unwrap();
    let database = Database::new(&root);
    assert_eq!(database.exists(), Ok(false));
    assert_eq!(marks(&database), Ok(None));
    assert_eq!(database.schema_version(), Ok(None));
    assert_eq!(database.write(false, |_| Ok(())), failure());
    assert!(!by_text.exists());
    // a creating write makes it there, private, and uses it
    assert_eq!(
        database.write(true, |conn| insert_project(conn, &new_id(), "made")),
        Ok(())
    );
    assert_eq!((mode(&by_text), mode(&file(&by_text))), (0o700, 0o600));
    assert_eq!(marks(&database), Ok(Some(vec!["made".to_string()])));
    assert_eq!(fs::read(file(&by_links)).unwrap(), untouched);
    assert_eq!(names_beside(&by_links), [DATABASE_FILENAME]);

    // and the other way round: a store only where the text leads is found
    let sandbox = Sandbox::new();
    let (root, by_text, by_links) = two_places(&sandbox);
    marked(&by_text, "by-text");
    assert_eq!(
        marks(&Database::new(&root)),
        Ok(Some(vec!["by-text".to_string()]))
    );
    assert!(!by_links.exists());
}

#[test]
fn a_store_below_a_linked_directory_is_used_as_before() {
    // a link above the store, or the store's own directory as a link, is followed
    let sandbox = Sandbox::new();
    let dir = sandbox.dir.path();
    fs::create_dir_all(dir.join("real/deep")).unwrap();
    symlink(dir.join("real"), dir.join("above")).unwrap();
    symlink(dir.join("real/deep"), dir.join("store-link")).unwrap();
    marked(&dir.join("above/new"), "through-a-parent");
    assert_eq!(
        marks(&Database::new(&dir.join("real/new"))),
        Ok(Some(vec!["through-a-parent".to_string()]))
    );
    marked(&dir.join("store-link"), "through-the-root");
    assert_eq!(
        marks(&Database::new(&dir.join("real/deep"))),
        Ok(Some(vec!["through-the-root".to_string()]))
    );
    assert_eq!(names_beside(&dir.join("real/deep")), [DATABASE_FILENAME]);
}

#[test]
fn busy_writer_gives_up_after_the_timeout_with_storage_failure() {
    let (sandbox, database) = store();
    let (holding, held) = mpsc::channel();
    let (release, released) = mpsc::channel::<()>();
    let holder = thread::spawn(move || {
        database.write(false, |conn| {
            insert_project(conn, &new_id(), "held")?;
            holding.send(()).unwrap();
            released.recv().unwrap();
            Ok(())
        })
    });
    held.recv().unwrap();
    let impatient = Database::with_busy_timeout(&sandbox.root, 200);
    let started = Instant::now();
    let ran = Cell::new(false);
    assert_eq!(
        impatient.write(false, |_| {
            ran.set(true);
            Ok(())
        }),
        Err(CoreError::StorageFailure)
    );
    let waited = started.elapsed();
    assert!(!ran.get());
    assert!(
        waited >= Duration::from_millis(200) && waited < Duration::from_secs(4),
        "{waited:?}"
    );
    // a reader is not held up by a writer that has not committed, and sees none of it
    let count = impatient.read(|conn| {
        Ok(conn
            .unwrap()
            .query_row("SELECT count(*) FROM projects", [], |row| {
                row.get::<_, i64>(0)
            })?)
    });
    assert_eq!(count, Ok(0));
    release.send(()).unwrap();
    assert_eq!(holder.join().unwrap(), Ok(()));
    assert_eq!(impatient.write(false, |_| Ok(())), Ok(()));
}

// --- rows ---

fn memory() -> Memory {
    Memory::new(
        "b",
        MemoryType::Project,
        &new_id(),
        "h",
        "agent",
        Trust::Agent,
        true,
        "d",
    )
    .unwrap()
}

fn text_value(text: &str) -> Value {
    Value::Text(text.to_string())
}

#[test]
fn a_memory_round_trips_through_its_row() {
    let plain = memory();
    assert_eq!(memory_from_row(&memory_to_row(&plain)), Ok(plain.clone()));
    let mut full = memory();
    full.last_read_at = Some(now());
    full.deleted_at = Some(now());
    full.level = Some(1);
    full.tags = vec!["a".to_string(), "b-1".to_string()];
    full.sync = false;
    full.version = 7;
    full.trust = Trust::UntrustedDerived;
    assert_eq!(memory_from_row(&memory_to_row(&full)), Ok(full.clone()));

    // and through the table, in the order the column list names
    let (_sandbox, database) = store();
    let marks = (1..=16)
        .map(|n| format!("?{n}"))
        .collect::<Vec<_>>()
        .join(", ");
    database
        .write(false, |conn| {
            for memory in [&plain, &full] {
                conn.execute(
                    "INSERT OR IGNORE INTO projects (id, name) VALUES (?1, 'p')",
                    [&memory.project_id],
                )?;
                conn.execute(
                    &format!("INSERT INTO memories ({MEMORY_COLUMNS}) VALUES ({marks})"),
                    rusqlite::params_from_iter(memory_to_row(memory)),
                )?;
            }
            Ok(())
        })
        .unwrap();
    let read = database.read(|conn| {
        let conn = conn.expect("a store");
        let mut statement = conn.prepare(&format!(
            "SELECT {MEMORY_COLUMNS} FROM memories ORDER BY version"
        ))?;
        let rows = statement
            .query_map([], values)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows
            .iter()
            .map(|row| memory_from_row(row))
            .collect::<Vec<_>>())
    });
    assert_eq!(read, Ok(vec![Ok(plain), Ok(full)]));
}

#[test]
fn row_with_a_malformed_id_or_root_is_refused() {
    let columns: Vec<&str> = MEMORY_COLUMNS.split(", ").collect();
    assert_eq!(columns.len(), 16);
    let fullwidth: String = "2026-09-24T00:00:01.000000Z"
        .chars()
        .map(|c| match c.to_digit(10) {
            Some(digit) => char::from_u32(0xff10 + digit).unwrap(),
            None => c,
        })
        .collect();
    let long_tag = format!("[\"{}\"]", "a".repeat(33));
    let changes: Vec<(&str, Value)> = vec![
        ("id", text_value("../../evil")),
        ("id", Value::Blob(b"0123456789".to_vec())),
        ("project_id", text_value("ABCDEFGHJK")),
        ("project_id", Value::Null),
        ("type", text_value("note")),
        ("trust", text_value("high")),
        ("source_harness", Value::Integer(1)),
        ("source_method", Value::Null),
        ("sync", Value::Integer(2)),
        ("sync", Value::Real(1.0)),
        ("sync", text_value("1")),
        ("sync", Value::Null),
        ("version", Value::Integer(0)),
        ("version", text_value("1")),
        ("description", Value::Null),
        ("body", Value::Blob(b"bytes".to_vec())),
        ("created", Value::Integer(5)),
        ("updated", Value::Null),
        ("deleted_at", Value::Integer(5)),
        ("deleted_at", Value::Blob(vec![0x80])),
        ("last_read_at", Value::Integer(5)),
        ("last_read_at", text_value("not-a-timestamp")),
        ("last_read_at", text_value("9999-99-99T99:99:99.999999Z")),
        ("last_read_at", text_value("2026-02-30T00:00:00.000000Z")),
        ("last_read_at", text_value(&fullwidth)),
        ("level", Value::Integer(0)),
        ("level", text_value("1")),
        ("level", Value::Real(1.0)),
        ("tags", Value::Null),
        ("tags", text_value("x")),
        ("tags", text_value("{\"a\": 1}")),
        ("tags", text_value("[\"Bad\"]")),
        ("tags", text_value("[\"a\", \"a\"]")),
        ("tags", text_value(&long_tag)),
    ];
    for (column, value) in changes {
        let mut row = memory_to_row(&memory());
        let at = columns.iter().position(|name| *name == column).unwrap();
        row[at] = value.clone();
        assert!(memory_from_row(&row).is_err(), "{column} = {value:?}");
    }
    // a row of another width is no memory row
    let row = memory_to_row(&memory());
    assert!(memory_from_row(&row[..15]).is_err());
    assert!(memory_from_row(&[row.clone(), vec![Value::Null]].concat()).is_err());
    // what is only text here is checked elsewhere: these are read
    let mut loose = row;
    loose[9] = text_value("not a time");
    loose[12] = text_value("whenever");
    assert!(memory_from_row(&loose).is_ok());

    let project = |id: &str, name: &str, root: Option<&str>, is_global: i64| {
        vec![
            text_value(id),
            text_value(name),
            root.map_or(Value::Null, text_value),
            Value::Integer(is_global),
        ]
    };
    assert_eq!(PROJECT_COLUMNS, "id, name, root, is_global");
    let id = new_id();
    for row in [
        project("../../evil", "n", None, 0),
        project(&id, "", None, 0),
        project(&id, "two\nlines", None, 0),
        project(&id, " padded", None, 0),
        project(&id, "two  spaces", None, 0),
        project(&id, "n", Some("relative/path"), 0),
        project(&id, "n", Some(""), 0),
        project(&id, "n", Some("/nul\0"), 0),
        project(&id, "n", None, 2),
        project(&id, "n", Some("/work/x/../a"), 0),
        project(&id, "n", Some("/work/./a"), 0),
        project(&id, "n", Some("/work/"), 0),
        project(&id, "n", Some("/work//a"), 0),
        project(&id, "n", Some("//work"), 0),
        project(&id, "n", Some("///work"), 0),
        project(&id, "n", Some("/.."), 0),
        vec![
            Value::Blob(id.clone().into_bytes()),
            text_value("n"),
            Value::Null,
            Value::Integer(0),
        ],
        vec![
            text_value(&id),
            Value::Blob(vec![0x80]),
            Value::Null,
            Value::Integer(0),
        ],
        vec![
            text_value(&id),
            text_value("n"),
            Value::Integer(1),
            Value::Integer(0),
        ],
        vec![
            text_value(&id),
            text_value("n"),
            Value::Null,
            text_value("0"),
        ],
        vec![text_value(&id), text_value("n"), Value::Null, Value::Null],
        vec![text_value(&id), text_value("n"), Value::Null],
    ] {
        assert!(project_from_row(&row).is_err(), "{row:?}");
    }
    for (root, is_global) in [
        (Some("/w"), 0),
        (Some("/"), 0),
        (Some("/a/b.c/..d"), 0),
        (None, 1),
    ] {
        let (read, global) = project_from_row(&project(&id, "demo", root, is_global)).unwrap();
        assert_eq!(
            (
                read.id.as_str(),
                read.name.as_str(),
                read.root.as_deref(),
                global
            ),
            (id.as_str(), "demo", root, is_global == 1)
        );
    }
}

#[test]
fn invalid_utf8_text_fails_one_row_not_the_query() {
    let (sandbox, database) = store();
    let (good, bad) = ("0000000000".to_string(), "1111111111".to_string());
    database
        .write(false, |conn| {
            insert_project(conn, &good, "good")?;
            insert_project(conn, &bad, "bad")
        })
        .unwrap();
    raw(&sandbox.root)
        .execute(
            "UPDATE projects SET name = CAST(X'80' AS TEXT) WHERE id = ?1",
            [&bad],
        )
        .unwrap();
    let select = format!("SELECT {PROJECT_COLUMNS} FROM projects ORDER BY id");
    let decode = |conn: &Connection| -> Result<Vec<Vec<Value>>, CoreError> {
        let mut statement = conn.prepare(&select)?;
        let rows = statement
            .query_map([], values)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    };
    let from_read = database
        .read(|conn| decode(conn.expect("a store")))
        .unwrap();
    let from_write = database.write(false, decode).unwrap();
    assert_eq!(from_read, from_write);
    // the whole query answered; the damaged value came back as the bytes it is
    assert_eq!(from_read.len(), 2);
    assert_eq!(from_read[1][1], Value::Blob(vec![0x80]));
    assert_eq!(project_from_row(&from_read[0]).unwrap().0.name, "good");
    assert!(project_from_row(&from_read[1]).is_err());
    assert_eq!(text(&from_read[0], 1), Ok(Some("good")));
    assert_eq!(text(&from_read[0], 2), Ok(None));
    assert!(text(&from_read[1], 1).is_err());
    // a number, and a column that is not there, are not text either
    assert!(text(&from_read[0], 3).is_err());
    assert!(text(&from_read[0], 4).is_err());
}
