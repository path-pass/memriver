//! The session store: one row per harness session, every change one short transaction.

use std::path::PathBuf;
use std::sync::Barrier;
use std::thread;

use memriver_core::models::errors::{CoreError, WriteError};
use memriver_core::models::session::{
    Harness, PromptEntry, PromptOmission, Session, SessionKey, SessionMove, SessionOrigin,
    SessionStatus, prompt_from_json,
};
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::repository::sqlite::database::Database;
use memriver_core::repository::sqlite::session_store::{Activity, MoveTo, SessionStore};
use memriver_test_support::Sandbox;
use rusqlite::Connection;
use rusqlite::types::Value;

const PROJECT: &str = "pppppppppp";
const OTHER: &str = "qqqqqqqqqq";
const GLOBAL: &str = "gggggggggg";
const PROJECT_ROOT: &str = "/work/app";
const HOUR: i64 = 3600;

fn key() -> SessionKey {
    SessionKey::new(Harness::ClaudeCode, "session-1").unwrap()
}

fn other_key() -> SessionKey {
    SessionKey::new(Harness::ClaudeCode, "session-2").unwrap()
}

fn codex(session_id: &str) -> SessionKey {
    SessionKey::new(Harness::Codex, session_id).unwrap()
}

fn at(second: u32) -> String {
    format!(
        "2026-09-24T10:{:02}:{:02}.000000Z",
        second / 60,
        second % 60
    )
}

fn hour(hour: u32, minute: u32) -> String {
    format!("2026-09-24T{hour:02}:{minute:02}:00.000000Z")
}

fn own(text: &str) -> Option<String> {
    Some(text.to_string())
}

fn session_of(key: SessionKey, change: impl FnOnce(&mut Session)) -> Session {
    let mut session = Session {
        key,
        status: SessionStatus::Registered,
        origin: SessionOrigin::Start,
        project_id: own(PROJECT),
        candidate_id: None,
        candidate_root: None,
        entry_cwd: "/work/app/src".to_string(),
        branch: own("main"),
        transcript_path: None,
        started_at: at(0),
        last_active_at: at(0),
        ended_at: None,
        prompt_count: 0,
        last_write_prompt_count: 0,
        last_nudge_prompt_count: 0,
        first_prompt: None,
        recent_prompts: Vec::new(),
        summary: None,
        summary_at: None,
    };
    change(&mut session);
    session
}

fn session(change: impl FnOnce(&mut Session)) -> Session {
    session_of(key(), change)
}

fn pending_of(key: SessionKey, change: impl FnOnce(&mut Session)) -> Session {
    session_of(key, |session| {
        session.status = SessionStatus::Pending;
        session.origin = SessionOrigin::FirstSeen;
        session.project_id = None;
        session.candidate_id = own(PROJECT);
        session.candidate_root = own(PROJECT_ROOT);
        change(session);
    })
}

fn pending() -> Session {
    pending_of(key(), |_| {})
}

fn no_candidate(session: &mut Session) {
    (session.candidate_id, session.candidate_root) = (None, None);
}

fn prompt(second: u32, text: &str) -> PromptEntry {
    PromptEntry::text(&at(second), text).unwrap()
}

struct Fixture {
    _sandbox: Sandbox,
    root: PathBuf,
    store: SessionStore,
}

/// No store at all.
fn absent() -> Fixture {
    let sandbox = Sandbox::new();
    let root = sandbox.root.clone();
    let store = SessionStore::with_busy_timeout(&root, 5000);
    Fixture {
        _sandbox: sandbox,
        root,
        store,
    }
}

/// A store holding two projects, no global.
fn initialized() -> Fixture {
    let fixture = absent();
    Database::new(&fixture.root)
        .write(true, |conn| {
            conn.execute(
                "INSERT INTO projects (id, name, root) VALUES (?1, 'app', ?2)",
                [PROJECT, PROJECT_ROOT],
            )?;
            conn.execute(
                "INSERT INTO projects (id, name, root) VALUES (?1, 'other', '/work/other')",
                [OTHER],
            )?;
            Ok(())
        })
        .unwrap();
    fixture
}

impl Fixture {
    fn raw(&self) -> Connection {
        let conn = Connection::open(self.root.join(DATABASE_FILENAME)).unwrap();
        // behind the store's back: nothing a raw writer plants is checked
        conn.execute_batch("PRAGMA foreign_keys = OFF; PRAGMA ignore_check_constraints = ON")
            .unwrap();
        conn
    }

    fn sql(&self, sql: &str) {
        self.raw().execute_batch(sql).unwrap();
    }

    fn count(&self, sql: &str) -> i64 {
        self.raw().query_row(sql, [], |row| row.get(0)).unwrap()
    }

    fn get(&self) -> Session {
        self.store.get(&key()).unwrap().unwrap()
    }

    fn register(&self, session: Session) -> Session {
        self.store.register(&session).unwrap().unwrap()
    }

    fn add(&self, key: &SessionKey, entry: &PromptEntry, seed: &Session) -> (Session, bool) {
        self.store
            .add_prompt(key, entry, seed, 5, Activity::First)
            .unwrap()
            .unwrap()
    }

    fn prompts_until(&self, count: i64) {
        for second in self.get().prompt_count + 1..=count {
            self.add(&key(), &prompt(second as u32, "fix the build"), &pending());
        }
    }

    fn nudge(&self, second: u32) -> bool {
        self.store.nudge_if_due(&key(), &at(second), 5, 5).unwrap()
    }

    fn confirm(&self, key: &SessionKey) -> Result<Option<Session>, CoreError> {
        let stored = self.store.get(key).unwrap();
        let stored = stored.as_ref();
        self.store.confirm(
            key,
            stored.and_then(|stored| stored.candidate_id.as_deref()),
            stored.and_then(|stored| stored.candidate_root.as_deref()),
        )
    }

    fn assign(&self, project_id: &str) -> Option<Session> {
        let assigned = match self.store.get(&key()).unwrap() {
            None => self.store.assign_project(
                &key(),
                project_id,
                SessionStatus::Registered,
                "/work/app/src",
                None,
            ),
            Some(stored) => self.store.assign_project(
                &key(),
                project_id,
                stored.status,
                &stored.entry_cwd,
                stored.transcript_path.as_deref(),
            ),
        };
        assigned.unwrap()
    }

    fn call_key(&self, harness: &str, call_id: &str) -> Option<SessionKey> {
        let record = self
            .store
            .call_record(harness, call_id, &hour(11, 0), HOUR)
            .unwrap();
        record.map(|record| record.key)
    }

    fn record(&self, key: &SessionKey, call_id: &str, at: &str, cwd: Option<&str>) {
        self.store.record_call(key, call_id, at, HOUR, cwd).unwrap();
    }

    fn ids(&self, project_id: Option<&str>, query: &str, limit: usize) -> Vec<String> {
        let found = self.store.search(project_id, query, limit).unwrap();
        found
            .iter()
            .map(|session| session.key.session_id().to_string())
            .collect()
    }
}

fn to_other<'a>() -> MoveTo<'a> {
    MoveTo {
        expected_transcript_path: Some("/t/a"),
        entry_cwd: "/work/other/x",
        branch: Some("dev"),
        transcript_path: "/t/b",
        project_id: Some(OTHER),
        project_root: Some("/work/other"),
        at: "2026-09-24T10:00:50.000000Z",
    }
}

fn is_value_error<T: std::fmt::Debug>(result: Result<T, WriteError>) {
    assert!(matches!(result, Err(WriteError::Value(_))), "{result:?}");
}

fn refused<T: std::fmt::Debug>(result: Result<T, CoreError>, reason: &str) {
    let expected = CoreError::ProjectUnavailable {
        reason: reason.to_string(),
    };
    assert_eq!(result.unwrap_err(), expected);
}

/// `work(index)` from two threads released at once; their results in order.
fn run_together<T: Send>(work: impl Fn(usize) -> T + Sync) -> Vec<T> {
    let barrier = Barrier::new(2);
    thread::scope(|scope| {
        let threads: Vec<_> = (0..2)
            .map(|index| {
                let (barrier, work) = (&barrier, &work);
                scope.spawn(move || {
                    barrier.wait();
                    work(index)
                })
            })
            .collect();
        threads
            .into_iter()
            .map(|thread| thread.join().unwrap())
            .collect()
    })
}

/// Two handles on one store, or one handle used twice.
fn stores(fixture: &Fixture, shared: bool) -> Vec<SessionStore> {
    let count = if shared { 1 } else { 2 };
    (0..count)
        .map(|_| SessionStore::with_busy_timeout(&fixture.root, 5000))
        .collect()
}

// --- get / register ---

#[test]
fn get_answers_none_for_an_unknown_key() {
    assert_eq!(initialized().store.get(&key()).unwrap(), None);
}

#[test]
fn reads_of_an_absent_store_find_nothing_and_create_nothing() {
    let fixture = absent();
    assert_eq!(fixture.store.get(&key()).unwrap(), None);
    assert!(!fixture.store.store_exists().unwrap());
    assert_eq!(fixture.store.search(None, "x", 10).unwrap(), []);
    assert_eq!(fixture.store.bound().unwrap(), []);
    assert_eq!(fixture.store.moves(&key()).unwrap(), []);
    assert!(!fixture.root.exists());
    assert!(initialized().store.store_exists().unwrap());
}

#[test]
fn register_stores_the_row_and_returns_it() {
    let fixture = initialized();
    let session = session(|session| session.transcript_path = own("/tmp/t.jsonl"));
    assert_eq!(fixture.register(session.clone()), session);
    assert_eq!(fixture.get(), session);
}

#[test]
fn register_never_overwrites_an_existing_row() {
    let fixture = initialized();
    let first = fixture.register(session(|_| {}));
    let second = fixture.register(session(|session| {
        session.project_id = own(OTHER);
        session.entry_cwd = "/elsewhere".to_string();
        session.branch = own("dev");
        session.origin = SessionOrigin::FirstSeen;
    }));
    assert_eq!(second, first);
    assert_eq!(fixture.get(), first);
}

#[test]
fn concurrent_registers_of_one_key_agree_on_the_stored_row() {
    for shared in [true, false] {
        let fixture = initialized();
        let stores = stores(&fixture, shared);
        let candidates = [
            session(|_| {}),
            session(|session| {
                session.project_id = own(OTHER);
                session.entry_cwd = "/work/other".to_string();
            }),
        ];
        let results = run_together(|index| {
            stores[index % stores.len()]
                .register(&candidates[index])
                .unwrap()
                .unwrap()
        });
        assert_eq!(results[0], results[1]);
        assert!(candidates.contains(&results[0]));
        assert_eq!(fixture.count("SELECT count(*) FROM sessions"), 1);
    }
}

#[test]
fn registering_a_row_that_would_not_read_back_is_a_value_error_and_writes_nothing() {
    let fixture = initialized();
    let impossible: [fn(&mut Session); 8] = [
        |session| session.started_at = "yesterday".to_string(),
        |session| session.last_write_prompt_count = 1,
        |session| session.last_nudge_prompt_count = 1,
        |session| session.prompt_count = -1,
        |session| session.project_id = own("../../evil"),
        |session| session.status = SessionStatus::Pending,
        |session| session.summary = own("a summary without its time"),
        |session| (session.summary, session.summary_at) = (own(" \u{1f} "), own(&at(1))),
    ];
    for change in impossible {
        is_value_error(fixture.store.register(&session(change)));
    }
    assert_eq!(fixture.count("SELECT count(*) FROM sessions"), 0);
    // and on a store that is not there: the row is judged before the store is looked for
    let nowhere = absent();
    is_value_error(nowhere.store.register(&session(impossible[0])));
}

#[test]
fn registering_watermarks_equal_to_the_prompt_count_is_accepted() {
    let fixture = initialized();
    let session = session(|session| {
        session.prompt_count = 5;
        session.last_write_prompt_count = 5;
        session.last_nudge_prompt_count = 5;
    });
    assert_eq!(fixture.register(session.clone()), session);
    assert_eq!(fixture.get(), session);
}

// --- touch ---

#[test]
fn touch_moves_last_active_at_forward_only() {
    let fixture = initialized();
    fixture.register(session(|session| session.last_active_at = at(10)));
    let touch = |second| {
        fixture
            .store
            .touch(&key(), &at(second), None, Activity::First)
            .unwrap()
    };
    assert_eq!(touch(20).unwrap().last_active_at, at(20));
    // a clock that went back: never back with it, and not standing still either
    let touched = touch(5).unwrap().last_active_at;
    assert!(touched > at(20), "{touched}");
    assert!(touch(5).unwrap().last_active_at > touched);
}

#[test]
fn touch_replaces_the_transcript_path_only_when_one_is_given() {
    let fixture = initialized();
    fixture.register(session(|session| {
        session.transcript_path = own("/tmp/a.jsonl")
    }));
    let touched = fixture
        .store
        .touch(&key(), &at(1), None, Activity::First)
        .unwrap()
        .unwrap();
    assert_eq!(touched.transcript_path.as_deref(), Some("/tmp/a.jsonl"));
    let touched = fixture
        .store
        .touch(&key(), &at(2), Some("/tmp/b.jsonl"), Activity::First);
    assert_eq!(
        touched.unwrap().unwrap().transcript_path.as_deref(),
        Some("/tmp/b.jsonl")
    );
    assert_eq!(
        fixture.get().transcript_path.as_deref(),
        Some("/tmp/b.jsonl")
    );
}

#[test]
fn touch_of_an_unknown_key_is_none_and_writes_nothing() {
    let fixture = initialized();
    assert_eq!(
        fixture
            .store
            .touch(&key(), &at(1), None, Activity::First)
            .unwrap(),
        None
    );
    assert_eq!(fixture.count("SELECT count(*) FROM sessions"), 0);
}

// --- add_prompt ---

#[test]
fn the_first_prompt_inserts_the_seed_and_says_so() {
    let fixture = initialized();
    let (session, created) = fixture.add(&key(), &prompt(5, "fix the build"), &pending());
    assert!(created);
    assert_eq!(
        (session.status, session.prompt_count),
        (SessionStatus::Pending, 1)
    );
    assert_eq!(session.first_prompt, Some(prompt(5, "fix the build")));
    assert_eq!(session.recent_prompts, [prompt(5, "fix the build")]);
    assert_eq!(session.last_active_at, at(5));
    assert_eq!(fixture.get(), session);
}

#[test]
fn a_prompt_on_an_existing_row_ignores_the_seed() {
    let fixture = initialized();
    let registered = fixture.register(session(|_| {}));
    let (session, created) = fixture.add(&key(), &prompt(5, "x"), &pending());
    assert!(!created);
    assert_eq!(
        (session.status, session.project_id),
        (registered.status, registered.project_id)
    );
}

#[test]
fn recent_prompts_keeps_five_newest_last() {
    let fixture = initialized();
    let numbered = |second: u32| prompt(second, &format!("prompt {second}"));
    for second in 1..9 {
        fixture.add(&key(), &numbered(second), &pending());
    }
    let session = fixture.get();
    assert_eq!(session.prompt_count, 8);
    assert_eq!(session.first_prompt, Some(numbered(1)));
    assert_eq!(
        session.recent_prompts,
        (4..9).map(numbered).collect::<Vec<_>>()
    );
}

#[test]
fn an_omitted_prompt_is_counted_and_kept_without_text() {
    let fixture = initialized();
    let omitted = PromptEntry::omitted(&at(3), PromptOmission::Secret).unwrap();
    let (session, _) = fixture.add(&key(), &omitted, &pending());
    assert_eq!(session.first_prompt, Some(omitted.clone()));
    assert_eq!(
        (session.recent_prompts, session.prompt_count),
        (vec![omitted], 1)
    );
}

#[test]
fn an_earlier_prompt_never_moves_last_active_at_back() {
    let fixture = initialized();
    fixture.register(session(|session| session.last_active_at = at(30)));
    let (session, _) = fixture.add(&key(), &prompt(10, "x"), &pending());
    assert!(session.last_active_at > at(30));
    // the prompt keeps the time it was given
    assert_eq!(session.first_prompt, Some(prompt(10, "x")));
}

#[test]
fn activity_while_the_clock_is_behind_still_ends_what_a_summary_was_read_for() {
    // each event that counts as activity, arriving with a time a second before the one
    // the session was read at
    type Event = fn(&Fixture);
    let events: [(&str, Event); 5] = [
        ("a prompt", |fixture| {
            drop(fixture.add(&key(), &prompt(9, "x"), &pending()))
        }),
        ("a prompt at the same instant", |fixture| {
            drop(fixture.add(&key(), &prompt(10, "x"), &pending()))
        }),
        ("a start", |fixture| {
            drop(
                fixture
                    .store
                    .touch(&key(), &at(9), None, Activity::First)
                    .unwrap(),
            )
        }),
        ("a stop", |fixture| assert!(!fixture.nudge(9))),
        ("an end", |fixture| {
            fixture.store.end(&key(), &at(9)).unwrap()
        }),
    ];
    for (name, event) in events {
        let fixture = initialized();
        fixture.register(session(|session| session.last_active_at = at(10)));
        let store = &fixture.store;
        store
            .publish_summary(&key(), "what was done so far", &at(10), &at(10))
            .unwrap();
        event(&fixture);
        let after = fixture.get();
        assert!(after.last_active_at > at(10), "{name}");
        // a summary made from what was read before it is refused, the old one stays
        let published =
            store.publish_summary(&key(), "made from the old reading", &at(10), &at(11));
        assert_eq!(
            published.unwrap_err(),
            CoreError::SessionMoved.into(),
            "{name}"
        );
        assert_eq!(
            fixture.get().summary.as_deref(),
            Some("what was done so far"),
            "{name}"
        );
        assert_eq!(fixture.get(), after);
    }
}

#[test]
fn the_write_that_follows_a_rows_making_or_move_in_one_event_does_not_move_its_time_again() {
    // a first prompt: the row is made with a time taken after the prompt's own
    let fixture = initialized();
    let seed = pending_of(key(), |session| {
        (session.started_at, session.last_active_at) = (at(10), at(10));
    });
    let (made, created) = fixture.add(&key(), &prompt(9, "x"), &seed);
    assert!(created);
    assert_eq!((made.started_at, made.last_active_at), (at(10), at(10)));
    // on a row that is there the same prompt is an event of its own
    let (made, created) = fixture.add(&key(), &prompt(9, "x"), &seed);
    assert!(!created && made.last_active_at > at(10));
    // after a move of the same event: the time the move left, or the event's when later
    let fixture = initialized();
    fixture.register(session(|session| session.last_active_at = at(10)));
    let store = &fixture.store;
    let following = |second| {
        let added = store.add_prompt(
            &key(),
            &prompt(second, "x"),
            &pending(),
            5,
            Activity::Following,
        );
        added.unwrap().unwrap().0.last_active_at
    };
    assert_eq!(following(9), at(10));
    assert_eq!(following(10), at(10));
    assert_eq!(following(11), at(11));
    let touched = |second| {
        let touched = store.touch(&key(), &at(second), None, Activity::Following);
        touched.unwrap().unwrap().last_active_at
    };
    assert_eq!(touched(5), at(11));
    assert_eq!(touched(12), at(12));
    assert_eq!(fixture.get().prompt_count, 3);
}

#[test]
fn activity_on_a_session_whose_time_has_no_later_one_is_a_storage_failure_and_writes_nothing() {
    const LAST: &str = "9999-12-31T23:59:59.999999Z";
    type Event = fn(&Fixture) -> Result<(), WriteError>;
    let events: [(&str, Event); 5] = [
        ("a prompt", |fixture| {
            let added =
                fixture
                    .store
                    .add_prompt(&key(), &prompt(9, "x"), &pending(), 5, Activity::First);
            added.map(drop)
        }),
        ("a start", |fixture| {
            fixture
                .store
                .touch(&key(), &at(9), None, Activity::First)
                .map(drop)
        }),
        ("a stop", |fixture| {
            fixture.store.nudge_if_due(&key(), &at(9), 5, 5).map(drop)
        }),
        ("an end", |fixture| fixture.store.end(&key(), &at(9))),
        ("a move", |fixture| {
            fixture.store.move_(&key(), &to_other()).map(drop)
        }),
    ];
    for (name, event) in events {
        let fixture = initialized();
        let before = fixture.register(session(|session| {
            session.last_active_at = LAST.to_string();
            session.transcript_path = own("/t/a");
        }));
        // an event at that very instant, and one from a clock that is behind it
        assert_eq!(
            event(&fixture).unwrap_err(),
            CoreError::StorageFailure.into(),
            "{name}"
        );
        assert_eq!(fixture.get(), before, "{name}");
        assert_eq!(fixture.count("SELECT count(*) FROM session_moves"), 0);
    }
    // what follows a row's making or move in one event has nothing to move, and is written
    let fixture = initialized();
    fixture.register(session(|session| session.last_active_at = LAST.to_string()));
    let added =
        fixture
            .store
            .add_prompt(&key(), &prompt(9, "x"), &pending(), 5, Activity::Following);
    assert_eq!(added.unwrap().unwrap().0.last_active_at, LAST);
    // and the instant before the last still has one after it
    let fixture = initialized();
    fixture.register(session(|session| {
        session.last_active_at = "9999-12-31T23:59:59.999998Z".to_string()
    }));
    let touched = fixture
        .store
        .touch(&key(), &at(9), None, Activity::First)
        .unwrap()
        .unwrap();
    assert_eq!(touched.last_active_at, LAST);
}

#[test]
fn prompt_json_is_stored_compact_and_unescaped() {
    let fixture = initialized();
    fixture.add(
        &key(),
        &prompt(1, "\u{4fee}\u{590d}\u{6784}\u{5efa}"),
        &pending(),
    );
    let (first, recent): (String, String) = fixture
        .raw()
        .query_row(
            "SELECT first_prompt, recent_prompts FROM sessions",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        first,
        "{\"at\":\"2026-09-24T10:00:01.000000Z\",\"text\":\"\u{4fee}\u{590d}\u{6784}\u{5efa}\"}"
    );
    assert_eq!(recent, format!("[{first}]"));
}

#[test]
fn concurrent_first_prompts_create_one_row_and_exactly_one_reports_it() {
    for shared in [true, false] {
        let fixture = initialized();
        let stores = stores(&fixture, shared);
        let results = run_together(|index| {
            let entry = prompt(index as u32 + 1, "x");
            let added = stores[index % stores.len()].add_prompt(
                &key(),
                &entry,
                &pending(),
                5,
                Activity::First,
            );
            added.unwrap().unwrap().1
        });
        assert_eq!(results.iter().filter(|created| **created).count(), 1);
        assert_eq!(fixture.count("SELECT count(*) FROM sessions"), 1);
        assert_eq!(fixture.get().prompt_count, 2);
    }
}

#[test]
fn a_prompt_count_with_no_next_one_is_a_storage_failure_and_writes_nothing() {
    let fixture = initialized();
    fixture.register(session(|session| session.prompt_count = i64::MAX));
    let before = fixture.get();
    let added = fixture
        .store
        .add_prompt(&key(), &prompt(1, "x"), &pending(), 5, Activity::First);
    assert_eq!(added.unwrap_err(), CoreError::StorageFailure.into());
    assert_eq!(fixture.get(), before);
}

// --- end / mark_saved ---

#[test]
fn end_records_ended_at_and_touches_last_active_at() {
    let fixture = initialized();
    fixture.register(session(|session| session.last_active_at = at(10)));
    fixture.store.end(&key(), &at(40)).unwrap();
    let session = fixture.get();
    assert_eq!(
        (session.ended_at, session.last_active_at),
        (Some(at(40)), at(40))
    );
    // an unknown key writes nothing
    fixture.store.end(&other_key(), &at(1)).unwrap();
    assert_eq!(fixture.count("SELECT count(*) FROM sessions"), 1);
}

#[test]
fn mark_saved_moves_the_write_watermark_to_the_prompt_count() {
    let fixture = initialized();
    fixture.register(session(|_| {}));
    fixture.prompts_until(3);
    fixture.store.mark_saved(&key()).unwrap();
    assert_eq!(fixture.get().last_write_prompt_count, 3);
}

// --- nudge_if_due ---

#[test]
fn stop_nudge_arithmetic() {
    let fixture = initialized();
    fixture.register(session(|_| {}));
    let mut answers = Vec::new();
    for count in 4..16 {
        if count == 11 {
            fixture.store.mark_saved(&key()).unwrap();
        }
        fixture.prompts_until(count);
        answers.push((count, fixture.nudge(100 + count as u32)));
    }
    // after five unsaved prompts, then every five; a save starts the count again
    let due: Vec<i64> = answers
        .iter()
        .filter(|(_, due)| *due)
        .map(|(count, _)| *count)
        .collect();
    assert_eq!(due, [5, 10, 15]);
    assert_eq!(fixture.get().last_nudge_prompt_count, 15);
    // the first nudge waits for the unsaved prompts alone, not for the interval too
    let fixture = initialized();
    fixture.register(session(|_| {}));
    let mut due = Vec::new();
    for count in 1..12 {
        fixture.prompts_until(count);
        if fixture.store.nudge_if_due(&key(), &at(100), 3, 5).unwrap() {
            due.push(count);
        }
    }
    assert_eq!(due, [3, 8]);
}

#[test]
fn stop_advances_last_active_at_even_when_not_due() {
    let fixture = initialized();
    fixture.register(session(|_| {}));
    for second in [200, 201, 202] {
        assert!(!fixture.nudge(second));
        assert_eq!(fixture.get().last_active_at, at(second));
    }
}

#[test]
fn a_pending_row_is_never_nudged_or_touched() {
    let fixture = initialized();
    fixture.add(&key(), &prompt(1, "x"), &pending());
    fixture.prompts_until(6);
    let before = fixture.get();
    assert!(!fixture.nudge(300));
    assert_eq!(fixture.get(), before);
}

#[test]
fn a_session_without_a_project_is_never_nudged() {
    let fixture = initialized();
    fixture.register(session(|session| session.project_id = None));
    fixture.prompts_until(6);
    assert!(!fixture.nudge(300));
    assert_eq!(fixture.get().last_active_at, at(300));
}

#[test]
fn an_unknown_key_is_never_nudged() {
    let fixture = initialized();
    assert!(!fixture.nudge(1));
    assert_eq!(fixture.count("SELECT count(*) FROM sessions"), 0);
}

#[test]
fn two_concurrent_checks_at_one_watermark_nudge_at_most_once() {
    for shared in [true, false] {
        let fixture = initialized();
        fixture.register(session(|_| {}));
        fixture.prompts_until(5);
        let stores = stores(&fixture, shared);
        let results = run_together(|index| {
            let due = stores[index % stores.len()].nudge_if_due(&key(), &at(400), 5, 5);
            due.unwrap()
        });
        assert_eq!(results.iter().filter(|due| **due).count(), 1);
    }
}

// --- confirm ---

#[test]
fn confirm_binds_a_pending_row_to_its_unchanged_candidate() {
    let fixture = initialized();
    fixture.register(pending());
    let confirmed = fixture.confirm(&key()).unwrap().unwrap();
    assert_eq!(
        (confirmed.status, confirmed.project_id.as_deref()),
        (SessionStatus::Registered, Some(PROJECT))
    );
    assert_eq!(fixture.get(), confirmed);
    // with the candidate named outright
    let fixture = initialized();
    fixture.register(pending());
    let confirmed = fixture
        .store
        .confirm(&key(), Some(PROJECT), Some(PROJECT_ROOT));
    assert_eq!(
        confirmed.unwrap().unwrap().project_id.as_deref(),
        Some(PROJECT)
    );
}

#[test]
fn confirm_without_a_candidate_registers_the_row_with_no_project() {
    let fixture = initialized();
    fixture.register(pending_of(key(), no_candidate));
    let confirmed = fixture.confirm(&key()).unwrap().unwrap();
    assert_eq!(
        (confirmed.status, confirmed.project_id),
        (SessionStatus::Registered, None)
    );
}

#[test]
fn confirm_refuses_a_candidate_that_changed_and_leaves_the_row() {
    // a global project with a root keeps the is_global check apart from the root's
    for change in [
        "UPDATE projects SET root = '/work/moved' WHERE id = 'pppppppppp'",
        "UPDATE projects SET is_global = 1 WHERE id = 'pppppppppp'",
        "DELETE FROM projects WHERE id = 'pppppppppp'",
    ] {
        let fixture = initialized();
        let before = fixture.register(pending());
        fixture.sql(change);
        refused(fixture.confirm(&key()), "candidate-changed");
        assert_eq!(fixture.get(), before, "{change}");
    }
}

#[test]
fn confirm_refuses_candidate_moved() {
    for (candidate_id, root) in [(OTHER, "/work/other"), (PROJECT, "/work/elsewhere")] {
        let fixture = initialized();
        let before = fixture.register(pending());
        let confirmed = fixture
            .store
            .confirm(&key(), Some(candidate_id), Some(root));
        refused(confirmed, "candidate-moved");
        assert_eq!(fixture.get(), before);
    }
}

#[test]
fn a_candidate_without_a_root_is_not_confirmed_to_a_project_whose_root_is_not_text() {
    let fixture = initialized();
    let before = fixture.register(pending_of(key(), |session| session.candidate_root = None));
    // a root only a raw writer can leave: it is not "no root"
    fixture.sql("UPDATE projects SET root = CAST(X'2FFF' AS TEXT) WHERE id = 'pppppppppp'");
    refused(
        fixture.store.confirm(&key(), Some(PROJECT), None),
        "candidate-changed",
    );
    assert_eq!(fixture.get(), before);
    assert_eq!(fixture.store.bound().unwrap(), []);
    // a project that really has none is the candidate still
    fixture.sql("UPDATE projects SET root = NULL WHERE id = 'pppppppppp'");
    let confirmed = fixture.store.confirm(&key(), Some(PROJECT), None).unwrap();
    assert_eq!(confirmed.unwrap().project_id.as_deref(), Some(PROJECT));
}

#[test]
fn confirming_a_registered_row_returns_it_unchanged() {
    let fixture = initialized();
    fixture.register(pending());
    let confirmed = fixture.confirm(&key()).unwrap();
    assert_eq!(fixture.confirm(&key()).unwrap(), confirmed);
    let registered = fixture.register(session_of(codex("other"), |session| {
        session.project_id = own(OTHER)
    }));
    assert_eq!(fixture.confirm(&registered.key).unwrap(), Some(registered));
    // an unknown key
    assert_eq!(fixture.confirm(&other_key()).unwrap(), None);
}

// --- assign_project ---

#[test]
fn assign_project_binds_a_registered_row_with_no_project() {
    let fixture = initialized();
    let mut expected = fixture.register(session(|session| session.project_id = None));
    expected.project_id = own(PROJECT);
    assert_eq!(fixture.assign(PROJECT), Some(expected.clone()));
    assert_eq!(fixture.get(), expected);
}

#[test]
fn assign_project_registers_a_pending_row_without_a_candidate() {
    let fixture = initialized();
    let mut expected = fixture.register(pending_of(key(), no_candidate));
    expected.status = SessionStatus::Registered;
    expected.project_id = own(PROJECT);
    assert_eq!(fixture.assign(PROJECT), Some(expected.clone()));
    assert_eq!(expected.origin, SessionOrigin::FirstSeen);
}

#[test]
fn assign_project_leaves_a_row_with_a_project_or_candidate_unchanged() {
    for row in [
        session(|session| session.project_id = own(OTHER)),
        pending(),
    ] {
        let fixture = initialized();
        let before = fixture.register(row);
        assert_eq!(fixture.assign(PROJECT), Some(before.clone()));
        assert_eq!(fixture.get(), before);
    }
    // a candidate named for a pending row that has one is not replaced either
    let fixture = initialized();
    let before = fixture.register(pending());
    let assigned = fixture.store.assign_project(
        &key(),
        OTHER,
        SessionStatus::Pending,
        &before.entry_cwd,
        None,
    );
    assert_eq!(assigned.unwrap(), Some(before));
}

#[test]
fn assign_project_of_an_unknown_key_is_none() {
    let fixture = initialized();
    assert_eq!(fixture.assign(PROJECT), None);
    assert_eq!(fixture.count("SELECT count(*) FROM sessions"), 0);
}

#[test]
fn assign_project_is_compare_and_set_on_status_entry_and_transcript() {
    let fixture = initialized();
    let before = fixture.register(session(|session| {
        session.project_id = None;
        session.transcript_path = own("/t/new");
    }));
    let entry = before.entry_cwd.as_str();
    for (status, entry_cwd, transcript) in [
        (SessionStatus::Registered, "/elsewhere", Some("/t/new")),
        (SessionStatus::Registered, entry, Some("/t/old")),
        (SessionStatus::Registered, entry, None),
        (SessionStatus::Pending, entry, Some("/t/new")),
    ] {
        let assigned = fixture
            .store
            .assign_project(&key(), PROJECT, status, entry_cwd, transcript);
        assert_eq!(assigned.unwrap(), Some(before.clone()));
        assert_eq!(fixture.get(), before);
    }
}

#[test]
fn assign_project_ignores_leftover_candidate_on_registered_row() {
    let fixture = initialized();
    fixture.register(pending());
    fixture.confirm(&key()).unwrap();
    fixture.sql("UPDATE sessions SET project_id = NULL");
    assert_eq!(
        fixture.assign(OTHER).unwrap().project_id.as_deref(),
        Some(OTHER)
    );
}

// --- search ---

fn search_world(fixture: &Fixture) {
    fixture.register(session_of(codex("old"), |session| {
        session.last_active_at = at(10);
        session.branch = own("feature/Login");
    }));
    fixture.register(session_of(codex("new"), |session| {
        session.last_active_at = at(30);
        session.entry_cwd = "/work/app/LOGIN".to_string();
    }));
    fixture.register(session_of(codex("elsewhere"), |session| {
        session.project_id = own(OTHER);
        session.last_active_at = at(20);
        session.entry_cwd = "/work/other/login".to_string();
    }));
    fixture.register(pending_of(codex("pending"), |session| {
        session.last_active_at = at(40);
        session.entry_cwd = "/work/app/login".to_string();
    }));
    let seed = session_of(codex("prompted"), |session| {
        session.branch = None;
        session.entry_cwd = "/work/app".to_string();
    });
    fixture.add(&codex("prompted"), &prompt(25, "Fix the LOGIN form"), &seed);
}

#[test]
fn search_in_a_project_sees_only_its_registered_rows_newest_first() {
    let fixture = initialized();
    search_world(&fixture);
    assert_eq!(
        fixture.ids(Some(PROJECT), "login", 10),
        ["new", "prompted", "old"]
    );
}

#[test]
fn search_across_all_projects_sees_every_row() {
    let fixture = initialized();
    search_world(&fixture);
    assert_eq!(
        fixture.ids(None, "LOGIN", 10),
        ["pending", "new", "prompted", "elsewhere", "old"]
    );
    // the limit and the query
    assert_eq!(fixture.ids(None, "login", 2), ["pending", "new"]);
    assert!(fixture.ids(None, "no such thing", 10).is_empty());
}

fn summarized(session_id: &str, second: u32, summary: &str, branch: &str) -> Session {
    session_of(codex(session_id), |session| {
        session.last_active_at = at(second);
        session.branch = own(branch);
        if !summary.is_empty() {
            (session.summary, session.summary_at) = (own(summary), own(&at(second)));
        }
    })
}

#[test]
fn a_multi_keyword_search_finds_sessions_holding_any_term() {
    let fixture = initialized();
    search_world(&fixture);
    fixture.register(summarized(
        "summary",
        5,
        "Rotated the staging certificates.",
        "main",
    ));
    assert_eq!(
        fixture.ids(None, "form, CERTIFICATES", 10),
        ["prompted", "summary"]
    );
    assert!(fixture.ids(None, "zebra yak", 10).is_empty());
}

#[test]
fn keyword_hits_rank_by_phrase_then_terms_then_recency() {
    let fixture = initialized();
    fixture.register(summarized("phrase-old", 1, "Fix LOGIN FORM", "main"));
    fixture.register(summarized("one-term", 9, "", "login"));
    fixture.register(summarized("two-terms", 5, "login", "form"));
    fixture.register(summarized("phrase-new", 7, "the login form", "main"));
    fixture.register(summarized("none", 8, "", "main"));
    assert_eq!(
        fixture.ids(Some(PROJECT), "login form", 10),
        ["phrase-new", "phrase-old", "two-terms", "one-term"]
    );
    assert_eq!(
        fixture.ids(Some(PROJECT), "login form", 2),
        ["phrase-new", "phrase-old"]
    );
}

#[test]
fn the_empty_query_lists_every_session_and_a_keywordless_one_matches_nothing() {
    let fixture = initialized();
    search_world(&fixture);
    let everything = ["pending", "new", "prompted", "elsewhere", "old"];
    assert_eq!(fixture.ids(None, "", 10), everything);
    assert_eq!(fixture.ids(None, "", 2), everything[..2]);
    assert!(fixture.ids(None, "", 0).is_empty());
    for query in [" ,; ", "\0", " \u{3000}\u{3001} "] {
        assert!(fixture.ids(None, query, 10).is_empty(), "{query:?}");
    }
}

#[test]
fn a_bad_row_is_skipped_by_search_and_fails_a_direct_get() {
    let nested = format!("{}{}", "[".repeat(100_000), "]".repeat(100_000));
    let cases: [(&str, Value); 12] = [
        ("recent_prompts", "[ ]".to_string().into()), // not the compact form
        ("recent_prompts", "not json".to_string().into()),
        // neither text nor omitted
        (
            "first_prompt",
            "{\"at\":\"2026-09-24T10:00:01.000000Z\"}"
                .to_string()
                .into(),
        ),
        ("started_at", "yesterday".to_string().into()),
        ("recent_prompts", nested.into()),
        ("last_write_prompt_count", 1.into()), // above prompt_count (0)
        ("last_nudge_prompt_count", 1.into()),
        ("project_id", "../../evil".to_string().into()),
        ("status", "paused".to_string().into()),
        ("entry_cwd", Value::Blob(vec![0x2f, 0xff])),
        ("ended_at", "soon".to_string().into()),
        ("prompt_count", (-1).into()),
    ];
    for (column, value) in cases {
        let fixture = initialized();
        let x = |session: &mut Session| session.entry_cwd = "/work/app/x".to_string();
        fixture.register(session_of(codex("good"), x));
        fixture.register(session(x));
        let planted = match value {
            // text that is not UTF-8, as a raw writer can leave it
            Value::Blob(bytes) => fixture.raw().execute(
                &format!(
                    "UPDATE sessions SET {column} = CAST(?1 AS TEXT) WHERE session_id = 'session-1'"
                ),
                [bytes],
            ),
            value => fixture.raw().execute(
                &format!("UPDATE sessions SET {column} = ?1 WHERE session_id = 'session-1'"),
                [value],
            ),
        };
        assert_eq!(planted.unwrap(), 1);
        assert_eq!(fixture.ids(None, "", 10), ["good"], "{column}");
        assert_eq!(fixture.ids(None, "/work/app/x", 10), ["good"], "{column}");
        assert_eq!(fixture.store.bound().unwrap().len(), 1, "{column}");
        assert_eq!(fixture.store.get(&key()), Err(CoreError::StorageFailure));
    }
}

#[test]
fn a_bad_row_fails_a_write_too_and_is_left_as_it_was() {
    let fixture = initialized();
    fixture.register(session(|_| {}));
    fixture.sql("UPDATE sessions SET recent_prompts = 'not json'");
    let failure = WriteError::Core(CoreError::StorageFailure);
    let store = &fixture.store;
    assert_eq!(
        store
            .touch(&key(), &at(50), None, Activity::First)
            .unwrap_err(),
        failure
    );
    assert_eq!(store.end(&key(), &at(50)).unwrap_err(), failure);
    assert_eq!(
        store.nudge_if_due(&key(), &at(50), 5, 5).unwrap_err(),
        failure
    );
    assert_eq!(
        store
            .add_prompt(&key(), &prompt(50, "x"), &pending(), 5, Activity::First)
            .unwrap_err(),
        failure
    );
    assert_eq!(store.adopt_transcript(&key(), "/t/a").unwrap_err(), failure);
    assert_eq!(store.move_(&key(), &to_other()).unwrap_err(), failure);
    assert_eq!(
        store
            .publish_summary(&key(), "s", &at(0), &at(50))
            .unwrap_err(),
        failure
    );
    assert_eq!(
        store.confirm(&key(), None, None),
        Err(CoreError::StorageFailure)
    );
    let unchanged: (String, String) = fixture
        .raw()
        .query_row(
            "SELECT recent_prompts, last_active_at FROM sessions",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(unchanged, ("not json".to_string(), at(0)));
}

#[test]
fn session_row_with_reordered_prompt_keys_is_read_and_rewritten_at_first() {
    let fixture = initialized();
    fixture.register(session(|_| {}));
    let stamp = at(1);
    let swapped = format!("{{\"text\":\"first\",\"at\":\"{stamp}\"}}");
    let omitted = format!("{{\"omitted\":\"secret\",\"at\":\"{stamp}\"}}");
    fixture
        .raw()
        .execute(
            "UPDATE sessions SET first_prompt = ?1, recent_prompts = ?2, prompt_count = 2",
            [&swapped, &format!("[{swapped},{omitted}]")],
        )
        .unwrap();
    // read as it stands
    let read = fixture.get();
    assert_eq!(read.first_prompt, Some(prompt(1, "first")));
    assert_eq!(
        read.recent_prompts,
        [
            prompt(1, "first"),
            PromptEntry::omitted(&stamp, PromptOmission::Secret).unwrap()
        ]
    );
    // the next write of that session stores `at` first
    fixture
        .store
        .touch(&key(), &at(2), None, Activity::First)
        .unwrap();
    let (first, recent): (String, String) = fixture
        .raw()
        .query_row(
            "SELECT first_prompt, recent_prompts FROM sessions",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(first, format!("{{\"at\":\"{stamp}\",\"text\":\"first\"}}"));
    assert_eq!(
        recent,
        format!("[{first},{{\"at\":\"{stamp}\",\"omitted\":\"secret\"}}]")
    );
    assert_eq!(fixture.get().recent_prompts, read.recent_prompts);
}

#[test]
fn session_row_with_prompt_json_not_in_its_written_form_is_invalid() {
    let stamp = at(1);
    let written = format!("{{\"at\":\"{stamp}\",\"text\":\"caf\u{e9}\"}}");
    assert!(prompt_from_json(&written).is_ok());
    for damaged in [
        format!("{{\"at\": \"{stamp}\",\"text\":\"x\"}}"), // extra white space
        format!("{{\"at\":\"{stamp}\",\"text\":\"x\"}} "),
        format!("{{\"at\":\"{stamp}\",\"at\":\"{stamp}\",\"text\":\"x\"}}"), // a repeated key
        format!("{{\"at\":\"{stamp}\",\"text\":\"caf\\u00e9\"}}"), // escapes never written
        format!("{{\"at\":\"{stamp}\",\"text\":\"a\\/b\"}}"),
    ] {
        for (column, value) in [
            ("first_prompt", damaged.clone()),
            ("recent_prompts", format!("[{damaged}]")),
        ] {
            let fixture = initialized();
            fixture.register(session(|_| {}));
            fixture
                .raw()
                .execute(&format!("UPDATE sessions SET {column} = ?1"), [&value])
                .unwrap();
            assert_eq!(
                fixture.store.get(&key()),
                Err(CoreError::StorageFailure),
                "{value}"
            );
            assert!(fixture.ids(None, "", 10).is_empty());
        }
    }
}

// --- an absent store ---

#[test]
fn session_writes_into_a_missing_store_are_silent() {
    let fixture = absent();
    let store = &fixture.store;
    assert_eq!(store.register(&session(|_| {})).unwrap(), None);
    assert_eq!(
        store.touch(&key(), &at(1), None, Activity::First).unwrap(),
        None
    );
    assert_eq!(
        store
            .add_prompt(&key(), &prompt(1, "x"), &pending(), 5, Activity::First)
            .unwrap(),
        None
    );
    store.end(&key(), &at(1)).unwrap();
    assert!(!store.nudge_if_due(&key(), &at(1), 5, 5).unwrap());
    store.mark_saved(&key()).unwrap();
    assert_eq!(fixture.confirm(&key()).unwrap(), None);
    assert_eq!(fixture.assign(PROJECT), None);
    assert_eq!(store.adopt_transcript(&key(), "/t/a").unwrap(), None);
    assert_eq!(store.move_(&key(), &to_other()).unwrap(), None);
    store
        .record_call(&key(), "call-1", &hour(10, 0), HOUR, None)
        .unwrap();
    assert_eq!(fixture.call_key("claude-code", "call-1"), None);
    // a summary alone has an answer: there is no such session
    assert_eq!(
        store
            .publish_summary(&key(), "s", &at(0), &at(1))
            .unwrap_err(),
        CoreError::SessionMoved.into()
    );
    assert!(!fixture.root.exists());
}

#[test]
fn a_malformed_argument_is_a_value_error_and_writes_nothing() {
    let fixture = initialized();
    let before = fixture.register(session(|session| {
        session.last_active_at = at(10);
        session.transcript_path = own("/t/a");
    }));
    let store = &fixture.store;
    is_value_error(store.touch(&key(), "yesterday", None, Activity::First));
    is_value_error(store.end(&key(), "2026-09-24"));
    is_value_error(store.nudge_if_due(&key(), "", 5, 5));
    is_value_error(store.add_prompt(
        &key(),
        &prompt(1, "x"),
        &pending_of(codex("x"), |_| {}),
        5,
        Activity::First,
    ));
    is_value_error(store.add_prompt(&key(), &prompt(1, "x"), &pending(), 0, Activity::First));
    is_value_error(store.move_(
        &key(),
        &MoveTo {
            at: "soon",
            ..to_other()
        },
    ));
    is_value_error(store.publish_summary(&key(), "s", &at(10), "soon"));
    // a summary that would not read back, and a project that is no id
    is_value_error(store.publish_summary(&key(), " \n", &at(10), &at(11)));
    is_value_error(store.move_(
        &key(),
        &MoveTo {
            project_id: Some("nope"),
            ..to_other()
        },
    ));
    let unbound = fixture.register(session_of(codex("none"), |session| {
        session.project_id = None
    }));
    let registered = SessionStatus::Registered;
    is_value_error(store.assign_project(
        &unbound.key,
        "nope",
        registered,
        &unbound.entry_cwd,
        None,
    ));
    assert_eq!(store.get(&unbound.key).unwrap(), Some(unbound));
    assert_eq!(fixture.get(), before);
    assert_eq!(fixture.count("SELECT count(*) FROM session_moves"), 0);
}

// --- tool calls (the Claude Code call mapping) ---

#[test]
fn a_recorded_call_names_its_session() {
    let fixture = initialized();
    fixture.record(&key(), "call-1", &hour(10, 0), None);
    fixture.record(&other_key(), "call-2", &hour(10, 0), None);
    assert_eq!(fixture.call_key("claude-code", "call-1"), Some(key()));
    assert_eq!(fixture.call_key("claude-code", "call-2"), Some(other_key()));
    assert_eq!(fixture.call_key("claude-code", "call-3"), None);
    assert_eq!(fixture.call_key("codex", "call-1"), None);
    assert_eq!(fixture.call_key("cursor", "call-1"), None);
}

#[test]
fn recording_a_call_again_moves_it_to_the_latest_session() {
    let fixture = initialized();
    fixture.record(&key(), "call-1", &hour(10, 0), Some("/w"));
    fixture.record(&other_key(), "call-1", &hour(10, 5), None);
    let record = fixture
        .store
        .call_record("claude-code", "call-1", &hour(11, 0), HOUR)
        .unwrap()
        .unwrap();
    assert_eq!((record.key, record.cwd), (other_key(), None));
    assert_eq!(fixture.count("SELECT count(*) FROM tool_calls"), 1);
}

#[test]
fn tool_call_older_than_3600s_is_not_returned() {
    let fixture = initialized();
    fixture.record(&key(), "old", &hour(9, 0), None);
    fixture.record(&key(), "edge", &hour(10, 0), None);
    fixture.record(&key(), "new", &hour(11, 0), None);
    assert_eq!(fixture.call_key("claude-code", "old"), None);
    // exactly as old as the retention is kept
    assert_eq!(fixture.call_key("claude-code", "edge"), Some(key()));
    assert_eq!(fixture.call_key("claude-code", "new"), Some(key()));
    // across midnight and a month's end the limit is still an hour
    fixture.record(&key(), "eve", "2026-09-30T23:30:00.000000Z", None);
    fixture.record(&key(), "day", "2026-10-01T00:29:59.999999Z", None);
    assert_eq!(fixture.call_key("claude-code", "eve"), Some(key()));
    fixture.record(&key(), "later", "2026-10-01T00:30:00.000001Z", None);
    assert_eq!(fixture.call_key("claude-code", "eve"), None);
    assert_eq!(fixture.call_key("claude-code", "day"), Some(key()));
}

#[test]
fn a_tool_call_past_its_retention_is_not_found_although_nothing_was_recorded_since() {
    let fixture = initialized();
    fixture.record(&key(), "call-1", &hour(9, 0), Some("/w"));
    let found = |at: &str| {
        let record = fixture.store.call_record("claude-code", "call-1", at, HOUR);
        record.unwrap().map(|record| (record.key, record.cwd))
    };
    let known = Some((key(), Some("/w".to_string())));
    assert_eq!(found(&hour(9, 0)), known);
    assert_eq!(found("2026-09-24T09:59:59.999999Z"), known);
    // exactly as old as the retention is still known, a microsecond more is not
    assert_eq!(found(&hour(10, 0)), known);
    assert_eq!(found("2026-09-24T10:00:00.000001Z"), None);
    assert_eq!(found(&hour(23, 0)), None);
    // a look-up removes nothing: the row is there for the next record to drop
    assert_eq!(fixture.count("SELECT count(*) FROM tool_calls"), 1);
    assert_eq!(found(&hour(9, 30)), known);
    // a time that is none, or no retention, finds nothing
    assert_eq!(found("yesterday"), None);
    let record = fixture
        .store
        .call_record("claude-code", "call-1", &hour(9, 0), 0);
    assert_eq!(record.unwrap(), None);
}

#[test]
fn a_malformed_call_is_a_value_error_and_records_nothing() {
    let fixture = initialized();
    let store = &fixture.store;
    for call_id in ["a b", "", &"x".repeat(257)] {
        is_value_error(store.record_call(&key(), call_id, &hour(10, 0), HOUR, None));
        // looking such an id up finds nothing
        assert_eq!(
            store
                .call_record("claude-code", call_id, &hour(11, 0), HOUR)
                .unwrap(),
            None
        );
    }
    is_value_error(store.record_call(&key(), "call-1", "yesterday", HOUR, None));
    is_value_error(store.record_call(&key(), "call-1", &hour(10, 0), 0, None));
    assert_eq!(fixture.count("SELECT count(*) FROM tool_calls"), 0);
    assert_eq!(
        store
            .call_record("claude-code", "missing", &hour(11, 0), HOUR)
            .unwrap(),
        None
    );
}

#[test]
fn a_call_row_naming_an_invalid_session_is_ignored() {
    let fixture = initialized();
    fixture.record(&key(), "call-1", &hour(10, 0), None);
    // a working directory that is not text names none; the session is still known
    fixture.sql("UPDATE tool_calls SET cwd = CAST(X'2FFF' AS TEXT)");
    let record = fixture
        .store
        .call_record("claude-code", "call-1", &hour(11, 0), HOUR)
        .unwrap();
    assert_eq!(
        record.map(|record| (record.key, record.cwd)),
        Some((key(), None))
    );
    fixture.sql("UPDATE tool_calls SET session_id = 'a b'");
    assert_eq!(fixture.call_key("claude-code", "call-1"), None);
    fixture.sql("UPDATE tool_calls SET session_id = CAST(X'FF' AS TEXT)");
    assert_eq!(fixture.call_key("claude-code", "call-1"), None);
}

#[test]
fn record_call_keeps_an_absolute_cwd_and_drops_any_other() {
    let fixture = initialized();
    fixture.record(&key(), "call-1", &hour(10, 0), Some("/work/other"));
    let record = fixture
        .store
        .call_record("claude-code", "call-1", &hour(11, 0), HOUR)
        .unwrap()
        .unwrap();
    assert_eq!(
        (record.key, record.cwd.as_deref()),
        (key(), Some("/work/other"))
    );
    for cwd in [Some("rel/dir"), Some("/a\0b"), Some(""), None] {
        fixture.record(&key(), "call-1", &hour(10, 0), cwd);
        let record = fixture
            .store
            .call_record("claude-code", "call-1", &hour(11, 0), HOUR)
            .unwrap();
        assert_eq!(record.unwrap().cwd, None, "{cwd:?}");
    }
}

// --- the published summary ---

#[test]
fn the_sessions_table_holds_the_published_summary_and_nothing_about_its_production() {
    let fixture = initialized();
    let conn = fixture.raw();
    let mut statement = conn
        .prepare("SELECT name FROM pragma_table_info('sessions')")
        .unwrap();
    let mut columns: Vec<String> = statement
        .query_map([], |row| row.get(0))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    columns.sort();
    assert_eq!(
        columns,
        [
            "branch",
            "candidate_id",
            "candidate_root",
            "ended_at",
            "entry_cwd",
            "first_prompt",
            "harness",
            "last_active_at",
            "last_nudge_prompt_count",
            "last_write_prompt_count",
            "origin",
            "project_id",
            "prompt_count",
            "recent_prompts",
            "session_id",
            "started_at",
            "status",
            "summary",
            "summary_at",
            "transcript_path",
        ]
    );
}

#[test]
fn a_published_summary_round_trips_and_later_writes_keep_it() {
    let fixture = initialized();
    fixture.register(session(|_| {}));
    let store = &fixture.store;
    store
        .publish_summary(&key(), "Fixed the login page.", &at(0), &at(5))
        .unwrap();
    let stored = fixture.get();
    assert_eq!(
        (
            stored.summary.as_deref(),
            stored.summary_at,
            stored.last_active_at
        ),
        (Some("Fixed the login page."), Some(at(5)), at(0))
    );
    store.touch(&key(), &at(9), None, Activity::First).unwrap();
    fixture.add(&key(), &prompt(10, "x"), &session(|_| {}));
    let stored = fixture.get();
    assert_eq!(
        (stored.summary.as_deref(), stored.summary_at),
        (Some("Fixed the login page."), Some(at(5)))
    );
    // and a search matches it
    assert_eq!(fixture.ids(Some(PROJECT), "LOGIN page", 10), ["session-1"]);
}

fn with_global(fixture: &Fixture) {
    fixture.sql("INSERT INTO projects (id, name, is_global) VALUES ('gggggggggg', 'g', 1)");
}

#[test]
fn publishing_for_a_moved_unbound_pending_global_or_unknown_session_writes_nothing() {
    let fixture = initialized();
    with_global(&fixture);
    fixture.register(session(|_| {}));
    fixture.register(session_of(codex("unbound"), |session| {
        session.project_id = None
    }));
    fixture.register(pending_of(codex("pending"), |_| {}));
    fixture.register(session_of(codex("on-global"), |session| {
        session.project_id = own(GLOBAL)
    }));
    // a project that is gone, as only a raw writer can leave it
    fixture.register(session_of(codex("gone"), |_| {}));
    fixture.sql("UPDATE sessions SET project_id = 'zzzzzzzzzz' WHERE session_id = 'gone'");
    fixture
        .store
        .touch(&key(), &at(3), None, Activity::First)
        .unwrap(); // activity after it was read
    for key in [
        key(),
        codex("unbound"),
        codex("pending"),
        codex("on-global"),
        codex("gone"),
        codex("never-seen"),
    ] {
        let published = fixture
            .store
            .publish_summary(&key, "a summary", &at(0), &at(5));
        assert_eq!(
            published.unwrap_err(),
            CoreError::SessionMoved.into(),
            "{key:?}"
        );
    }
    assert_eq!(
        fixture.count("SELECT count(*) FROM sessions WHERE summary IS NOT NULL"),
        0
    );
}

#[test]
fn bound_lists_sessions_with_a_writable_project_newest_first() {
    let fixture = initialized();
    with_global(&fixture);
    fixture.register(session(|session| session.last_active_at = at(1)));
    fixture.register(session_of(codex("s-2"), |session| {
        session.project_id = own(OTHER);
        session.last_active_at = at(2);
    }));
    fixture.register(session_of(codex("s-3"), |session| {
        session.project_id = None
    }));
    fixture.register(pending_of(codex("s-4"), |_| {}));
    fixture.register(session_of(codex("s-5"), |session| {
        session.project_id = own(GLOBAL)
    }));
    let bound = fixture.store.bound().unwrap();
    let ids: Vec<&str> = bound
        .iter()
        .map(|session| session.key.session_id())
        .collect();
    assert_eq!(ids, ["s-2", "session-1"]);
}

#[test]
fn a_summary_memriver_could_not_have_written_is_damage() {
    let fixture = initialized();
    fixture.register(session(|_| {}));
    fixture
        .raw()
        .execute(
            "UPDATE sessions SET summary = '  ', summary_at = ?1",
            [at(5)],
        )
        .unwrap();
    assert_eq!(fixture.store.get(&key()), Err(CoreError::StorageFailure));
    assert!(fixture.ids(None, "", 10).is_empty()); // skipped, not a failure
    for (summary, summary_at) in [("'done'", "'soon'"), ("'done'", "NULL"), ("NULL", "'soon'")] {
        fixture.sql(&format!(
            "UPDATE sessions SET summary = {summary}, summary_at = {summary_at}"
        ));
        assert_eq!(fixture.store.get(&key()), Err(CoreError::StorageFailure));
    }
}

// --- adopt_transcript ---

#[test]
fn adopt_transcript_only_when_null() {
    let fixture = initialized();
    fixture.register(session(|_| {}));
    let adopt = |path| {
        fixture
            .store
            .adopt_transcript(&key(), path)
            .unwrap()
            .unwrap()
    };
    assert_eq!(
        adopt("/t/a.jsonl").transcript_path.as_deref(),
        Some("/t/a.jsonl")
    );
    assert_eq!(
        adopt("/t/b.jsonl").transcript_path.as_deref(),
        Some("/t/a.jsonl")
    );
    assert_eq!(fixture.get().transcript_path.as_deref(), Some("/t/a.jsonl"));
    assert_eq!(
        fixture
            .store
            .adopt_transcript(&other_key(), "/t/c")
            .unwrap(),
        None
    );
}

// --- move ---

fn started_at_a(fixture: &Fixture, row: Session) -> Session {
    let mut row = row;
    row.transcript_path = own("/t/a");
    fixture.register(row)
}

fn moved(fixture: &Fixture, to: &MoveTo<'_>) -> (Session, Option<SessionMove>) {
    fixture.store.move_(&key(), to).unwrap().unwrap()
}

#[test]
fn move_registered_to_other_project() {
    let fixture = initialized();
    started_at_a(&fixture, session(|_| {}));
    let (updated, record) = moved(&fixture, &to_other());
    assert_eq!(
        (
            updated.project_id.as_deref(),
            updated.entry_cwd.as_str(),
            updated.branch.as_deref(),
            updated.transcript_path.as_deref(),
            updated.last_active_at.as_str()
        ),
        (
            Some(OTHER),
            "/work/other/x",
            Some("dev"),
            Some("/t/b"),
            at(50).as_str()
        )
    );
    let expected = SessionMove::new(
        key(),
        1,
        &at(50),
        "/work/app/src",
        own(PROJECT),
        "/work/other/x",
        own(OTHER),
    );
    assert_eq!(record, Some(expected.unwrap()));
    assert_eq!(fixture.get(), updated);
}

#[test]
fn move_registered_to_none() {
    let fixture = initialized();
    started_at_a(&fixture, session(|_| {}));
    let nowhere = MoveTo {
        project_id: None,
        project_root: None,
        branch: None,
        ..to_other()
    };
    let (updated, record) = moved(&fixture, &nowhere);
    assert_eq!((updated.project_id, updated.branch), (None, None));
    assert_eq!(record.unwrap().to_project_id, None);
    assert_eq!(updated.status, SessionStatus::Registered);
}

#[test]
fn move_clears_leftover_candidate() {
    let fixture = initialized();
    started_at_a(&fixture, pending());
    fixture.confirm(&key()).unwrap();
    assert_eq!(fixture.get().candidate_id.as_deref(), Some(PROJECT));
    let (updated, _) = moved(&fixture, &to_other());
    assert_eq!((updated.candidate_id, updated.candidate_root), (None, None));
}

#[test]
fn move_pending_sets_new_candidate() {
    let fixture = initialized();
    started_at_a(&fixture, pending());
    let (updated, record) = moved(&fixture, &to_other());
    assert_eq!(
        (
            updated.status,
            updated.project_id,
            updated.candidate_id.as_deref(),
            updated.candidate_root.as_deref()
        ),
        (
            SessionStatus::Pending,
            None,
            Some(OTHER),
            Some("/work/other")
        )
    );
    let record = record.unwrap();
    assert_eq!((record.from_project_id, record.to_project_id), (None, None));
}

#[test]
fn move_is_compare_and_set_on_the_stored_transcript() {
    let fixture = initialized();
    let before = fixture.register(session(|session| session.transcript_path = own("/t/zzz")));
    assert_eq!(moved(&fixture, &to_other()), (before.clone(), None));
    let expecting_none = MoveTo {
        expected_transcript_path: None,
        ..to_other()
    };
    assert_eq!(moved(&fixture, &expecting_none), (before.clone(), None));
    assert_eq!(fixture.get(), before);
    assert_eq!(fixture.count("SELECT count(*) FROM session_moves"), 0);
    // an unknown key
    assert_eq!(
        fixture.store.move_(&other_key(), &to_other()).unwrap(),
        None
    );
}

#[test]
fn moves_are_numbered_and_listed_in_order() {
    let fixture = initialized();
    started_at_a(&fixture, session(|_| {}));
    assert_eq!(moved(&fixture, &to_other()).1.unwrap().seq, 1);
    let stamp = at(60);
    let again = MoveTo {
        expected_transcript_path: Some("/t/b"),
        transcript_path: "/t/c",
        at: &stamp,
        ..to_other()
    };
    assert_eq!(moved(&fixture, &again).1.unwrap().seq, 2);
    let seqs = || -> Vec<i64> {
        let moves = fixture.store.moves(&key()).unwrap();
        moves.iter().map(|record| record.seq).collect()
    };
    assert_eq!(seqs(), [1, 2]);
    assert_eq!(fixture.store.moves(&other_key()).unwrap(), []);
    // a bad row is skipped
    fixture.sql("UPDATE session_moves SET moved_at = 'bad' WHERE seq = 1");
    assert_eq!(seqs(), [2]);
    fixture.sql("UPDATE session_moves SET to_project_id = 'nope' WHERE seq = 2");
    assert_eq!(seqs(), [0i64; 0]);
}

#[test]
fn move_last_active_strictly_after() {
    for second in [10, 5] {
        let fixture = initialized();
        started_at_a(&fixture, session(|session| session.last_active_at = at(10)));
        let stamp = at(second);
        let (updated, record) = moved(
            &fixture,
            &MoveTo {
                at: &stamp,
                ..to_other()
            },
        );
        assert!(updated.last_active_at > at(10));
        assert_eq!(record.unwrap().moved_at, updated.last_active_at);
        // so a summary made from what was read before the move is refused
        let published = fixture.store.publish_summary(&key(), "s", &at(10), &at(70));
        assert_eq!(published.unwrap_err(), CoreError::SessionMoved.into());
    }
}
