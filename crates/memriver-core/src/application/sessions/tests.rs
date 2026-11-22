//! What the service does when another process writes between its read and its write,
//! and when the content policy fails: each needs a hand inside the store or the policy.

use std::fs;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::{Arc, LazyLock};

use memriver_test_support::Sandbox;
use rusqlite::Connection;
use serde_json::json;

use super::*;
use crate::models::session::PromptBody;
use crate::repository::directories::{DATABASE_FILENAME, ignore_case, transcript_looks};
use crate::repository::sqlite::session_store::meanwhile;
use crate::settings::Settings;
use crate::{Services, build_services};

const T_A: &str = "/t/a/s.jsonl";
const T_B: &str = "/t/b/s.jsonl";

fn key() -> SessionKey {
    SessionKey::new(Harness::ClaudeCode, "session-1").unwrap()
}

struct World {
    _sandbox: Sandbox,
    base: PathBuf,
    store: PathBuf,
    work: String,
    services: Services,
    global_id: Option<String>,
    project: Project,
}

fn world() -> Rc<World> {
    let sandbox = Sandbox::new();
    let base = fs::canonicalize(sandbox.dir.path()).unwrap();
    let (store, work) = (base.join("store"), base.join("work"));
    let settings = Settings::light(store.clone());
    let services = build_services(&settings, &base.join("home"), &sandbox.env(), None);
    let global_id = Some(services.project.ensure_global().unwrap());
    let mut world = World {
        _sandbox: sandbox,
        work: work.to_str().unwrap().to_string(),
        base,
        store,
        services,
        global_id,
        project: Project::new("unused", 120).unwrap(),
    };
    world.project = world.init("work");
    Rc::new(world)
}

impl World {
    fn init(&self, name: &str) -> Project {
        let directory = self.base.join(name);
        fs::create_dir_all(&directory).unwrap();
        let plan = self.services.project.plan_root(&directory, None).unwrap();
        self.services.project.init_project(name, &plan).unwrap()
    }

    fn session(&self) -> &SessionService {
        &self.services.session
    }

    fn row(&self) -> Session {
        self.session().session_store.get(&key()).unwrap().unwrap()
    }

    fn sql(&self, sql: &str, value: &str) {
        let conn = Connection::open(self.store.join(DATABASE_FILENAME)).unwrap();
        conn.execute(sql, [value]).unwrap();
    }

    fn start(&self, source: &str, transcript: Option<&str>, cwd: Option<&str>) -> StartOutcome {
        self.session()
            .start_session(&key(), source, &self.work, transcript, cwd)
            .unwrap()
    }

    fn prompt(&self, transcript: Option<&str>, cwd: Option<&str>) -> PromptOutcome {
        self.session()
            .observe_prompt(&key(), Some(&json!("first")), &self.work, transcript, cwd)
            .unwrap()
    }

    fn registered_to(&self, project: &Project) -> ProjectContext {
        registered_context(project.clone(), self.global_id.clone(), 120, Some(key()))
    }

    fn none(&self) -> ProjectContext {
        no_project_context(
            ProjectContextState::None,
            SESSION_NONE_HEADER,
            self.global_id.clone(),
            Some(key()),
        )
    }

    /// A session started in a directory no project covers yet.
    fn projectless_start(&self) {
        let later = self.base.join("later");
        fs::create_dir(&later).unwrap();
        let started =
            self.session()
                .start_session(&key(), "startup", later.to_str().unwrap(), None, None);
        assert_eq!(started.unwrap().context, self.none());
    }
}

/// Where another process moves the session meanwhile.
fn elsewhere(project: Option<&'static Project>) -> MoveTo<'static> {
    MoveTo {
        expected_transcript_path: None,
        entry_cwd: "/elsewhere",
        branch: None,
        transcript_path: T_B,
        project_id: project.map(|project| project.id.as_str()),
        project_root: project.and_then(|project| project.root.as_deref()),
        at: "2026-11-22T02:16:43.300060Z",
    }
}

#[test]
fn a_store_removed_after_the_existence_check_is_still_a_no_op() {
    let world = world();
    world.start("startup", None, None);
    let store = world.store.clone();
    meanwhile(move || fs::remove_dir_all(store).unwrap());
    let touched = world.session().session_store.touch(&key(), &now(), None);
    assert_eq!(touched.unwrap(), None);
    assert!(!world.store.exists());
    // and through the service: the answer of a store that is not there
    let world = self::world();
    let store = world.store.clone();
    meanwhile(move || fs::remove_dir_all(store).unwrap());
    let started = world.start("startup", None, None);
    assert_eq!(
        (started.context.state, started.context.header.as_str()),
        (ProjectContextState::None, NONE_HEADER)
    );
    assert!(!world.store.exists());
}

#[test]
fn a_first_start_and_a_first_prompt_racing_share_one_row() {
    // each has read no row and so builds its own seed; one INSERT wins, both answer
    // from it, and the prompt is counted once
    for register_first in [true, false] {
        let world = world();
        let peer = Rc::clone(&world);
        let (started, prompted) = if register_first {
            // the start's row lands while the prompt is between its read and its write
            let started = Rc::new(std::cell::RefCell::new(None));
            let slot = Rc::clone(&started);
            meanwhile(move || *slot.borrow_mut() = Some(peer.start("startup", None, None)));
            let prompted = world.prompt(None, None);
            (started.take().unwrap(), prompted)
        } else {
            let prompted = Rc::new(std::cell::RefCell::new(None));
            let slot = Rc::clone(&prompted);
            meanwhile(move || *slot.borrow_mut() = Some(peer.prompt(None, None)));
            let started = world.start("startup", None, None);
            (started, prompted.take().unwrap())
        };
        let rows = world.session().list_sessions(None, "", None).unwrap();
        assert_eq!(rows.len(), 1);
        let expected = if register_first {
            SessionStatus::Registered
        } else {
            SessionStatus::Pending
        };
        assert_eq!((rows[0].status, rows[0].prompt_count), (expected, 1));
        let first = rows[0].first_prompt.as_ref().unwrap();
        assert_eq!(first.body(), &PromptBody::Text("first".to_string()));
        assert_eq!(prompted.created_pending, !register_first);
        let context = world.session().session_context(Some(&key())).unwrap();
        assert_eq!(prompted.context, context);
        // the start that lost answers from the stored row, never from its own seed
        if !register_first {
            assert_eq!(started.context, context);
        }
        assert_eq!(
            context.state == ProjectContextState::Pending,
            !register_first
        );
    }
}

#[test]
fn register_keeps_a_project_the_row_got_in_the_meantime() {
    let world = world();
    world.projectless_start();
    let project = world.init("later");
    let other = world.init("other");
    let (peer, id) = (Rc::clone(&world), other.id.clone());
    meanwhile(move || peer.sql("UPDATE sessions SET project_id = ?1", &id));
    let registered = world.session().register_session(&key(), None).unwrap();
    assert_eq!(registered, world.registered_to(&other));
    assert_eq!(world.row().project_id, Some(other.id));
    assert_ne!(world.row().project_id, Some(project.id));
}

#[test]
fn move_between_resolve_and_assign_wins() {
    let world = world();
    world.projectless_start();
    world.init("later");
    let peer = Rc::clone(&world);
    meanwhile(move || {
        let moved = peer.session().session_store.move_(&key(), &elsewhere(None));
        let moved = moved.unwrap();
        assert!(moved.unwrap().1.is_some());
    });
    let registered = world.session().register_session(&key(), None).unwrap();
    assert_eq!(registered, world.none());
    let row = world.row();
    assert_eq!(
        (row.project_id, row.entry_cwd.as_str()),
        (None, "/elsewhere")
    );
}

#[test]
fn confirm_rejects_candidate_moved_by_move() {
    let world = world();
    world.prompt(None, None);
    let b = world.init("b");
    let (peer, to): (_, &'static Project) = (Rc::clone(&world), Box::leak(Box::new(b.clone())));
    meanwhile(move || {
        let moved = peer
            .session()
            .session_store
            .move_(&key(), &elsewhere(Some(to)));
        assert!(moved.unwrap().unwrap().1.is_some());
    });
    let confirmed = world.session().confirm_session(&key());
    assert_eq!(confirmed, Err(unavailable("candidate-moved")));
    let row = world.row();
    assert_eq!(
        (row.status, row.candidate_id),
        (SessionStatus::Pending, Some(b.id))
    );
}

#[test]
fn compact_move_loser_keeps_stored_path() {
    let world = world();
    world.start("startup", Some(T_A), None);
    let b = world.init("b");
    let peer = Rc::clone(&world);
    meanwhile(move || peer.sql("UPDATE sessions SET transcript_path = ?1", "/t/c/s.jsonl"));
    let compacted = world.start("compact", Some(T_B), b.root.as_deref());
    assert_eq!(
        (compacted.context, compacted.moved),
        (world.registered_to(&world.project), None)
    );
    let row = world.row();
    assert_eq!(
        (
            row.project_id,
            row.entry_cwd,
            row.transcript_path.as_deref()
        ),
        (
            Some(world.project.id.clone()),
            world.work.clone(),
            Some("/t/c/s.jsonl")
        )
    );
    assert_eq!(world.session().moves(&key()).unwrap(), []);
    // and the same loss on a prompt: the row answers, the prompt is still counted
    let peer = Rc::clone(&world);
    meanwhile(move || peer.sql("UPDATE sessions SET transcript_path = ?1", "/t/d/s.jsonl"));
    let observed = world.prompt(Some(T_B), b.root.as_deref());
    assert_eq!(
        (observed.context, observed.moved),
        (world.registered_to(&world.project), None)
    );
    assert_eq!(
        (world.row().prompt_count, world.row().entry_cwd.clone()),
        (1, world.work.clone())
    );
}

#[test]
fn a_transcript_path_that_arrives_meanwhile_is_not_replaced() {
    let world = world();
    world.start("startup", None, None);
    let peer = Rc::clone(&world);
    meanwhile(move || peer.sql("UPDATE sessions SET transcript_path = ?1", "/t/c/s.jsonl"));
    world.start("compact", Some(T_A), Some(&world.work));
    assert_eq!(world.row().transcript_path.as_deref(), Some("/t/c/s.jsonl"));
}

#[test]
fn a_scanner_failure_is_recorded_as_a_scan_error() {
    let world = world();
    let broken: PolicySource = Arc::new(LazyLock::new(|| panic!("the rules did not load")));
    let home = world.base.join("home");
    let session = SessionService::new(
        SessionStore::new(&world.store),
        ProjectStore::new(&world.store, &home),
        broken,
        Env::new(),
    );
    for _ in 0..2 {
        let observed =
            session.observe_prompt(&key(), Some(&json!("hello")), &world.work, None, None);
        assert_eq!(
            observed.unwrap().context.state,
            ProjectContextState::Pending
        );
    }
    let row = world.row();
    let omitted = PromptBody::Omitted(PromptOmission::ScanError);
    assert_eq!(row.first_prompt.unwrap().body(), &omitted);
    assert_eq!((row.prompt_count, row.recent_prompts.len()), (2, 2));
    // a prompt that is not looked at never reaches the policy
    session
        .observe_prompt(&key(), Some(&json!(42)), &world.work, None, None)
        .unwrap();
    let invalid = PromptBody::Omitted(PromptOmission::Invalid);
    assert_eq!(world.row().recent_prompts[2].body(), &invalid);
}

#[test]
fn without_a_store_no_directory_question_and_no_scan_is_asked() {
    // an entry that cannot be resolved is not a degraded answer, and a policy that
    // would fail is never loaded
    let sandbox = Sandbox::new();
    let broken: PolicySource = Arc::new(LazyLock::new(|| panic!("never loaded")));
    let session = SessionService::new(
        SessionStore::new(&sandbox.root),
        ProjectStore::new(&sandbox.root, &sandbox.home),
        Arc::clone(&broken),
        Env::new(),
    );
    assert!(!session.store_exists());
    let started = session.start_session(&key(), "startup", "/never/created", None, None);
    assert_eq!(started.unwrap().context.header, NONE_HEADER);
    let observed = session.observe_prompt(&key(), Some(&json!("hi")), "/never/created", None, None);
    let observed = observed.unwrap();
    assert_eq!(
        (observed.context.header.as_str(), observed.created_pending),
        (NONE_HEADER, false)
    );
    assert_eq!(
        session.register_session(&key(), None).unwrap().header,
        NONE_HEADER
    );
    assert_eq!(Arc::strong_count(&broken), 2);
    assert!(!sandbox.root.exists());
}

#[test]
fn building_the_services_and_the_calls_that_check_no_text_never_load_the_policy() {
    let world = world();
    let session = world.session();
    world.start("startup", None, None);
    session
        .observe_prompt(&key(), Some(&json!(42)), &world.work, None, None)
        .unwrap();
    session.stop_decision(&key()).unwrap();
    session.record_tool_call(&key(), "call-1", None).unwrap();
    session.end_session(&key()).unwrap();
    session.session_context(Some(&key())).unwrap();
    // nothing forced the rules; the first prompt with text does
    assert!(LazyLock::get(&session.policy).is_none());
    world.prompt(None, None);
    assert!(LazyLock::get(&session.policy).is_some());
}

/// The transcript of one session under two spellings of one directory, and a project
/// `b` the session's working directory has gone to meanwhile.
fn aliased(world: &World) -> (String, String, Project) {
    let transcript = |name: &str| {
        let directory = world.base.join(name);
        fs::create_dir_all(&directory).unwrap();
        format!("{}/s.jsonl", directory.to_str().unwrap())
    };
    let spellings = (transcript("Transcripts/x"), transcript("transcripts/x"));
    ignore_case();
    (spellings.0, spellings.1, world.init("b"))
}

#[test]
fn a_transcript_directory_spelled_another_way_does_not_move_a_registered_session() {
    let world = world();
    let (stored, spelled, b) = aliased(&world);
    world.start("startup", Some(&stored), None);
    world.prompt(Some(&stored), Some(&world.work));
    let before = world.row();
    // the working directory is in another project; only the spelling changed
    let observed = world.prompt(Some(&spelled), b.root.as_deref());
    let registered = world.registered_to(&world.project);
    assert_eq!(
        (observed.context, observed.moved),
        (registered.clone(), None)
    );
    let compacted = world.start("compact", Some(&spelled), b.root.as_deref());
    assert_eq!((compacted.context, compacted.moved), (registered, None));
    let after = world.row();
    assert_eq!(
        (after.project_id, after.entry_cwd, after.transcript_path),
        (before.project_id, before.entry_cwd, before.transcript_path)
    );
    assert_eq!(world.session().moves(&key()).unwrap(), []);
    // what the session said stays out of the other project's reach
    let reach = world.registered_to(&b);
    assert_eq!(
        world
            .session()
            .search_sessions("first", &reach, None)
            .unwrap(),
        []
    );
    // a directory that really is another one still moves it
    let elsewhere = world.base.join("Transcripts/y");
    fs::create_dir(&elsewhere).unwrap();
    let moved = format!("{}/s.jsonl", elsewhere.to_str().unwrap());
    let observed = world.prompt(Some(&moved), b.root.as_deref());
    assert!(observed.moved.is_some_and(|moved| moved.changed()));
    assert_eq!(world.row().project_id, Some(b.id));
}

#[test]
fn a_transcript_directory_spelled_another_way_does_not_change_a_pending_candidate() {
    let world = world();
    let (stored, spelled, b) = aliased(&world);
    world.prompt(Some(&stored), Some(&world.work));
    let before = world.row();
    assert_eq!(before.candidate_id, Some(world.project.id.clone()));
    let observed = world.prompt(Some(&spelled), b.root.as_deref());
    assert_eq!(observed.moved, None);
    world.start("compact", Some(&spelled), b.root.as_deref());
    let after = world.row();
    assert_eq!(
        (after.candidate_id, after.candidate_root, after.entry_cwd),
        (before.candidate_id, before.candidate_root, before.entry_cwd)
    );
    assert_eq!(world.session().moves(&key()).unwrap(), []);
    // and it is confirmed to the project it was seen in
    let confirmed = world.session().confirm_session(&key()).unwrap();
    assert_eq!(confirmed, world.registered_to(&world.project));
}

#[test]
fn the_transcript_directory_is_looked_at_only_for_a_claude_code_session_with_a_usable_directory() {
    let world = world();
    let b = world.init("b");
    let session = world.session();
    let looks = |work: &dyn Fn()| {
        let before = transcript_looks();
        work();
        transcript_looks() - before
    };
    // a Codex row is never checked
    let codex = SessionKey::new(Harness::Codex, "session-2").unwrap();
    session
        .start_session(&codex, "startup", &world.work, Some(T_A), None)
        .unwrap();
    let prompt = json!("hello");
    assert_eq!(
        looks(&|| {
            let observed = session.observe_prompt(
                &codex,
                Some(&prompt),
                &world.work,
                Some(T_B),
                b.root.as_deref(),
            );
            assert_eq!(observed.unwrap().moved, None);
            let compacted =
                session.start_session(&codex, "compact", &world.work, Some(T_B), b.root.as_deref());
            assert_eq!(compacted.unwrap().moved, None);
        }),
        0
    );
    // nor is a Claude Code row whose working directory cannot be used, or any other start
    world.start("startup", Some(T_A), None);
    assert_eq!(
        looks(&|| {
            for cwd in [None, Some("rel"), Some("/a\0")] {
                assert_eq!(world.prompt(Some(T_B), cwd).moved, None);
                assert_eq!(world.start("compact", Some(T_B), cwd).moved, None);
            }
            world.start("startup", Some(T_B), b.root.as_deref());
            world.start("resume", Some(T_A), b.root.as_deref());
        }),
        0
    );
    // with one it is, and the move is applied
    assert_eq!(
        looks(&|| assert!(world.prompt(Some(T_B), b.root.as_deref()).moved.is_some())),
        1
    );
}
