//! The session service over a real store, real directories and the real git, with the
//! memory writes that ask it whether a session is pending and mark it saved. Every
//! service comes from `build_services`.

use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};

use memriver_core::application::contexts::{
    NONE_HEADER, PENDING_HEADER, PENDING_NO_CANDIDATE_HEADER, SESSION_NONE_HEADER,
    SESSION_PROJECT_GONE_HEADER, STORE_UNREADABLE_HEADER, UNIDENTIFIED_HEADER, no_project_context,
    registered_context,
};
use memriver_core::application::sessions::{PromptOutcome, StartOutcome};
use memriver_core::models::context::{ProjectContext, ProjectContextState, ReadWriteSet};
use memriver_core::models::errors::{CoreError, WriteError};
use memriver_core::models::helpers::{cut, single_line};
use memriver_core::models::memory::Memory;
use memriver_core::models::project::Project;
use memriver_core::models::session::{
    Harness, MoveOutcome, PromptBody, PromptEntry, PromptOmission, Session, SessionKey,
    SessionOrigin, SessionStatus,
};
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::repository::sqlite::session_store::SessionStore;
use memriver_core::settings::{Env, Settings};
use memriver_core::{Services, build_services};
use memriver_test_support::{Sandbox, isolated_command};
use rusqlite::Connection;
use serde_json::{Value, json};

const T_A: &str = "/t/a/s.jsonl"; // the transcript under the start directory's projects dir
const T_B: &str = "/t/b/s.jsonl"; // the same file after a /cd

fn key() -> SessionKey {
    SessionKey::new(Harness::ClaudeCode, "session-1").unwrap()
}

fn other_key() -> SessionKey {
    SessionKey::new(Harness::Codex, "session-2").unwrap()
}

fn claude(session_id: &str) -> SessionKey {
    SessionKey::new(Harness::ClaudeCode, session_id).unwrap()
}

fn codex(session_id: &str) -> SessionKey {
    SessionKey::new(Harness::Codex, session_id).unwrap()
}

fn secret_prompt() -> String {
    format!("token ghp_{}", "a".repeat(36))
}

fn text(path: &Path) -> &str {
    path.to_str().unwrap()
}

fn unavailable(reason: &str) -> CoreError {
    CoreError::ProjectUnavailable {
        reason: reason.to_string(),
    }
}

struct World {
    sandbox: Sandbox,
    base: PathBuf,
    store: PathBuf,
    work: PathBuf,
    env: Env,
    services: Services,
    global_id: Option<String>,
    project: Option<Project>,
}

/// A place for a store, and a directory `work`; nothing is created in the store.
fn storeless() -> World {
    let sandbox = Sandbox::new();
    let base = fs::canonicalize(sandbox.dir.path()).unwrap();
    let (store, work) = (base.join("store"), base.join("work"));
    fs::create_dir(&work).unwrap();
    let env = sandbox.env();
    let services = build_services(
        &Settings::light(store.clone()),
        &base.join("home"),
        &env,
        None,
    );
    World {
        sandbox,
        base,
        store,
        work,
        env,
        services,
        global_id: None,
        project: None,
    }
}

/// A store with global and the project `demo` bound to `work`.
fn world() -> World {
    let mut world = storeless();
    world.global_id = Some(world.services.project.ensure_global().unwrap());
    world.project = Some(world.init(&world.work, "demo"));
    world
}

/// One call of a hook, with what the Python tests' defaults were.
struct Call<'a> {
    key: SessionKey,
    entry: Option<&'a Path>,
    transcript: Option<&'a str>,
    cwd: Option<&'a str>,
}

fn call<'a>() -> Call<'a> {
    Call {
        key: key(),
        entry: None,
        transcript: None,
        cwd: None,
    }
}

fn by<'a>(key: SessionKey) -> Call<'a> {
    Call { key, ..call() }
}

fn at<'a>(transcript: &'a str, cwd: &'a Path) -> Call<'a> {
    Call {
        transcript: Some(transcript),
        cwd: Some(text(cwd)),
        ..call()
    }
}

impl World {
    fn project(&self) -> &Project {
        self.project.as_ref().unwrap()
    }

    fn sessions(&self) -> SessionStore {
        SessionStore::new(&self.store)
    }

    fn row_of(&self, key: &SessionKey) -> Option<Session> {
        self.sessions().get(key).unwrap()
    }

    fn row(&self) -> Session {
        self.row_of(&key()).unwrap()
    }

    fn raw(&self) -> Connection {
        let conn = Connection::open(self.store.join(DATABASE_FILENAME)).unwrap();
        conn.execute_batch("PRAGMA foreign_keys = OFF").unwrap();
        conn
    }

    fn sql(&self, sql: &str, params: &[&str]) {
        self.raw()
            .execute(sql, rusqlite::params_from_iter(params))
            .unwrap();
    }

    fn init(&self, directory: &Path, name: &str) -> Project {
        fs::create_dir_all(directory).unwrap();
        let plan = self.services.project.plan_root(directory, None).unwrap();
        self.services.project.init_project(name, &plan).unwrap()
    }

    fn dir(&self, name: &str) -> PathBuf {
        let directory = self.base.join(name);
        fs::create_dir_all(&directory).unwrap();
        directory
    }

    fn start_as(&self, source: &str, call: Call<'_>) -> StartOutcome {
        let entry = call.entry.unwrap_or(&self.work);
        let started = self.services.session.start_session(
            &call.key,
            source,
            text(entry),
            call.transcript,
            call.cwd,
        );
        started.unwrap()
    }

    /// A startup with a transcript, in `work` unless the call says otherwise.
    fn start(&self, call: Call<'_>) -> ProjectContext {
        let transcript = call.transcript.or(Some("/t/1.jsonl"));
        self.start_as("startup", Call { transcript, ..call })
            .context
    }

    fn prompt_value(&self, prompt: Option<&Value>, call: Call<'_>) -> PromptOutcome {
        let entry = call.entry.unwrap_or(&self.work);
        let observed = self.services.session.observe_prompt(
            &call.key,
            prompt,
            text(entry),
            call.transcript,
            call.cwd,
        );
        observed.unwrap()
    }

    fn prompt(&self, prompt: &str, call: Call<'_>) -> PromptOutcome {
        self.prompt_value(Some(&json!(prompt)), call)
    }

    fn hello(&self) -> PromptOutcome {
        self.prompt("hello", call())
    }

    fn write(&self, context: &ProjectContext, content: &str) -> Result<Memory, WriteError> {
        let memory = &self.services.memory;
        memory.record(content, "project", false, "claude-code", "cue", context)
    }

    fn registered_to(&self, project: &Project, key: SessionKey) -> ProjectContext {
        registered_context(project.clone(), self.global_id.clone(), 120, Some(key))
    }

    fn registered(&self) -> ProjectContext {
        self.registered_to(self.project(), key())
    }

    fn no_project(
        &self,
        state: ProjectContextState,
        header: &str,
        key: SessionKey,
    ) -> ProjectContext {
        no_project_context(state, header, self.global_id.clone(), Some(key))
    }

    fn pending(&self) -> ProjectContext {
        self.no_project(ProjectContextState::Pending, PENDING_HEADER, key())
    }

    fn none(&self) -> ProjectContext {
        self.no_project(ProjectContextState::None, SESSION_NONE_HEADER, key())
    }

    fn context(&self, key: &SessionKey) -> ProjectContext {
        self.services.session.session_context(Some(key)).unwrap()
    }

    /// Where the session is: status, project, candidate, entry, branch, transcript.
    fn whereabouts(
        &self,
        key: &SessionKey,
    ) -> (
        SessionStatus,
        Option<String>,
        Option<String>,
        String,
        Option<String>,
        Option<String>,
    ) {
        let row = self.row_of(key).unwrap();
        (
            row.status,
            row.project_id,
            row.candidate_id,
            row.entry_cwd,
            row.branch,
            row.transcript_path,
        )
    }

    /// A session started in a directory no project covers yet.
    fn projectless_start(&self, below: &str) -> PathBuf {
        let entry = self.dir(&format!("later/{below}"));
        let entry = fs::canonicalize(entry).unwrap();
        let started = self.start(Call {
            entry: Some(&entry),
            ..call()
        });
        assert_eq!(started.state, ProjectContextState::None);
        entry
    }

    /// Real git, isolated from the user's and the system's configuration.
    fn git(&self, cwd: &Path, args: &[&str]) {
        let git = ["/usr/bin/git", "/bin/git", "/usr/local/bin/git"]
            .into_iter()
            .find(|git| Path::new(git).exists())
            .expect("git is installed");
        let output = isolated_command(&self.sandbox, Path::new(git))
            .current_dir(cwd)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .args(["-c", "user.name=t", "-c", "user.email=t@t"])
            .args(args)
            .output()
            .unwrap();
        assert!(output.status.success(), "git {args:?} failed");
    }

    /// A repository `main` with one commit, and its linked worktree `wt` on `feature`.
    fn repository(&self) -> (PathBuf, PathBuf) {
        let (main, worktree) = (self.base.join("main"), self.base.join("wt"));
        self.git(&self.base, &["init", "-q", "-b", "main", text(&main)]);
        self.git(&main, &["commit", "-q", "--allow-empty", "-m", "init"]);
        self.git(
            &main,
            &["worktree", "add", "-q", "-b", "feature", text(&worktree)],
        );
        (main, worktree)
    }

    /// The same services built again: nothing is kept between two hook processes.
    fn again(&self) -> Services {
        let settings = Settings::light(self.store.clone());
        build_services(&settings, &self.base.join("home"), &self.env, None)
    }
}

fn changed(outcome: &Option<MoveOutcome>) -> bool {
    outcome.as_ref().is_some_and(MoveOutcome::changed)
}

fn prompt_text(entry: &PromptEntry) -> &str {
    match entry.body() {
        PromptBody::Text(text) => text,
        PromptBody::Omitted(omitted) => panic!("omitted: {omitted}"),
    }
}

// --- the two callbacks of a memory write ---

#[test]
fn a_pending_session_writes_nothing_through_the_built_services_and_a_save_moves_its_mark() {
    let world = world();
    let registered = world.start(by(other_key()));
    let memory = world.write(&registered, "a fact").unwrap();
    let service = &world.services.memory;
    let memories = || {
        world
            .raw()
            .query_row("SELECT count(*), max(version) FROM memories", [], |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?))
            })
    };
    for (entry, reason, header) in [
        (world.work.clone(), "pending", PENDING_HEADER),
        (
            world.base.clone(),
            "pending-no-candidate",
            PENDING_NO_CANDIDATE_HEADER,
        ),
    ] {
        let key = claude(reason);
        let outcome = world.prompt(
            "hello",
            Call {
                entry: Some(&entry),
                ..by(key.clone())
            },
        );
        assert!(outcome.created_pending);
        let pending = world.context(&key);
        assert_eq!(
            (pending.state, pending.header.as_str()),
            (ProjectContextState::Pending, header)
        );
        let refused = WriteError::Core(unavailable(reason));
        assert_eq!(world.write(&pending, "kept out").unwrap_err(), refused);
        let updated = service.update(&memory.id, memory.version, "x", None, &pending, None);
        assert_eq!(updated.unwrap_err(), refused);
        let deleted = service.delete(&memory.id, memory.version, &pending, "mcp", None);
        assert_eq!(deleted.unwrap_err(), refused);
        // nothing was written, and the pending session's mark did not move
        assert_eq!(memories().unwrap(), (1, memory.version));
        assert_eq!(world.row_of(&key).unwrap().last_write_prompt_count, 0);
    }
    // confirmed: one successful record moves that session's mark, a delete does not
    let key = claude("pending");
    let confirmed = world.services.session.confirm_session(&key).unwrap();
    assert_eq!(confirmed, world.registered_to(world.project(), key.clone()));
    world.prompt("again", by(key.clone()));
    let written = world.write(&confirmed, "now it is saved").unwrap();
    let row = world.row_of(&key).unwrap();
    assert_eq!((row.prompt_count, row.last_write_prompt_count), (2, 2));
    world.prompt("and again", by(key.clone()));
    service
        .delete(&written.id, written.version, &confirmed, "mcp", None)
        .unwrap();
    let row = world.row_of(&key).unwrap();
    assert_eq!((row.prompt_count, row.last_write_prompt_count), (3, 2));
    // an update moves it too
    let updated = service.update(
        &memory.id,
        memory.version,
        "a newer fact",
        None,
        &confirmed,
        None,
    );
    assert_eq!(updated.unwrap().version, memory.version + 1);
    assert_eq!(world.row_of(&key).unwrap().last_write_prompt_count, 3);
}

#[test]
fn the_services_can_be_handed_to_another_thread() {
    fn handed<T: Send + Sync>() {}
    handed::<Services>();
    handed::<memriver_core::application::sessions::SessionService>();
}

#[test]
fn a_failed_save_leaves_the_watermark_alone() {
    let world = world();
    let context = world.start(call());
    world.hello();
    let rejected = world.write(&context, &secret_prompt()).unwrap_err();
    assert!(matches!(
        rejected,
        WriteError::Core(CoreError::ContentRejected { .. })
    ));
    assert_eq!(world.row().last_write_prompt_count, 0);
    let memory = world.write(&context, "uv manages python").unwrap();
    world.hello();
    let stale = world.services.memory.update(
        &memory.id,
        memory.version + 1,
        "changed",
        None,
        &context,
        None,
    );
    assert_eq!(
        stale.unwrap_err(),
        CoreError::VersionConflict(memory.id).into()
    );
    assert_eq!(world.row().last_write_prompt_count, 1);
}

#[test]
fn a_failed_watermark_never_fails_the_save() {
    let world = world();
    let context = world.start(call());
    world.hello();
    world.sql(
        "CREATE TRIGGER no_mark BEFORE UPDATE OF last_write_prompt_count ON sessions \
         BEGIN SELECT RAISE(ABORT, 'no'); END",
        &[],
    );
    assert_eq!(
        world.sessions().mark_saved(&key()),
        Err(CoreError::StorageFailure)
    );
    let memory = world.write(&context, "kept").unwrap();
    let updated =
        world
            .services
            .memory
            .update(&memory.id, memory.version, "kept too", None, &context, None);
    assert_eq!(updated.unwrap().version, memory.version + 1);
    assert_eq!(world.row().last_write_prompt_count, 0);
}

// --- start_session ---

#[test]
fn start_without_a_store_answers_none_and_creates_nothing() {
    let world = storeless();
    let context = world.start(call());
    assert_eq!(
        context,
        no_project_context(ProjectContextState::None, NONE_HEADER, None, Some(key()))
    );
    let observed = world.hello();
    assert_eq!(
        (observed.context, observed.created_pending, observed.moved),
        (context.clone(), false, None)
    );
    assert_eq!(
        world
            .services
            .session
            .register_session(&key(), None)
            .unwrap(),
        context
    );
    assert!(!world.store.exists());
}

#[test]
fn startup_clear_fork_register_at_once() {
    for source in ["startup", "clear", "fork"] {
        let world = world();
        let started = world.start_as(
            source,
            Call {
                transcript: Some("/t/1.jsonl"),
                ..call()
            },
        );
        assert_eq!((started.context, started.moved), (world.registered(), None));
        let row = world.row();
        assert_eq!(
            (row.status, row.origin, row.project_id.as_deref()),
            (
                SessionStatus::Registered,
                SessionOrigin::Start,
                Some(world.project().id.as_str())
            )
        );
        assert_eq!((row.candidate_id, row.candidate_root), (None, None));
        assert_eq!(
            (
                row.entry_cwd.as_str(),
                row.branch,
                row.transcript_path.as_deref()
            ),
            (text(&world.work), None, Some("/t/1.jsonl"))
        );
        assert_eq!((row.started_at, row.prompt_count), (row.last_active_at, 0));
    }
}

#[test]
fn resume_compact_and_unknown_sources_create_pending() {
    for source in ["resume", "compact", "", "Startup", "something new"] {
        let world = world();
        assert_eq!(world.start_as(source, call()).context, world.pending());
        let row = world.row();
        assert_eq!(
            (row.status, row.origin, row.project_id),
            (SessionStatus::Pending, SessionOrigin::FirstSeen, None)
        );
        assert_eq!(
            (row.candidate_id.as_ref(), row.candidate_root.as_ref()),
            (Some(&world.project().id), world.project().root.as_ref())
        );
        assert_eq!(row.entry_cwd, text(&world.work));
    }
}

#[test]
fn a_start_in_an_unbound_directory_registers_no_project() {
    let world = world();
    let elsewhere = world.dir("elsewhere");
    let started = world.start(Call {
        entry: Some(&elsewhere),
        ..call()
    });
    assert_eq!(started, world.none());
    assert_eq!(
        (world.row().status, world.row().project_id),
        (SessionStatus::Registered, None)
    );
    world.start_as(
        "resume",
        Call {
            entry: Some(&elsewhere),
            ..by(other_key())
        },
    );
    let row = world.row_of(&other_key()).unwrap();
    assert_eq!(
        (row.status, row.candidate_id, row.candidate_root),
        (SessionStatus::Pending, None, None)
    );
}

#[test]
fn resume_adopts_the_transcript_and_never_moves() {
    let world = world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let before = world.row();
    let b = world.init(&world.base.join("b"), "b");
    let root = b.root.clone().unwrap();
    let resumed = world.start_as(
        "resume",
        Call {
            entry: Some(Path::new(&root)),
            ..at(T_B, Path::new(&root))
        },
    );
    assert_eq!((resumed.context, resumed.moved), (world.registered(), None));
    let after = world.row();
    assert_eq!(
        world.whereabouts(&key()),
        (
            SessionStatus::Registered,
            Some(world.project().id.clone()),
            None,
            text(&world.work).to_string(),
            None,
            Some(T_B.to_string())
        )
    );
    assert!(after.last_active_at > before.last_active_at);
    // the path was adopted, so the next prompt sees no signal
    assert_eq!(world.prompt("hi", at(T_B, Path::new(&root))).moved, None);
    assert_eq!(world.services.session.moves(&key()).unwrap(), []);
    // a start without a transcript keeps the stored one
    world.start_as("resume", call());
    world.start_as("startup", call());
    assert_eq!(world.row().transcript_path.as_deref(), Some(T_B));
}

#[test]
fn a_directory_that_cannot_be_resolved_registers_nothing() {
    let world = world();
    for entry in [
        world.base.join("does-not-exist"),
        PathBuf::from("/tmp/x\0y"),
        PathBuf::from("relative"),
    ] {
        let started = world.start(Call {
            entry: Some(&entry),
            ..call()
        });
        assert_eq!(started.state, ProjectContextState::Degraded);
        assert_eq!(
            started.diagnostic.as_deref(),
            Some("working directory could not be resolved")
        );
        assert_eq!(
            (
                started.read_write_set.project_id.clone(),
                started.session_key.clone()
            ),
            (None, Some(key()))
        );
        let observed = world.prompt(
            "hi",
            Call {
                entry: Some(&entry),
                ..call()
            },
        );
        assert_eq!(
            (observed.context, observed.created_pending),
            (started, false)
        );
        assert_eq!(world.row_of(&key()), None);
    }
    assert_eq!(
        world
            .services
            .session
            .list_sessions(None, "", None)
            .unwrap(),
        []
    );
}

#[test]
fn a_symlinked_alias_registers_and_proposes_its_targets_project() {
    let world = world();
    let target = world.init(&world.base.join("a"), "a");
    let sub = world.dir("a/sub");
    world.init(&world.base.join("b"), "b");
    let alias = world.base.join("b/alias");
    symlink(&sub, &alias).unwrap();
    let started = world.start(Call {
        entry: Some(&alias),
        ..call()
    });
    assert_eq!(started.project.as_ref(), Some(&target));
    assert_eq!(world.row().entry_cwd, text(&sub));
    let pending = world
        .prompt(
            "hi",
            Call {
                entry: Some(&alias),
                ..by(other_key())
            },
        )
        .context;
    assert_eq!(pending.state, ProjectContextState::Pending);
    assert_eq!(
        world.services.session.pending_candidate(&pending).unwrap(),
        Some(target)
    );
}

// --- git: the place in the main tree, and the branch ---

#[test]
fn linked_worktree_registers_under_the_main_tree() {
    let world = world();
    let (main, worktree) = world.repository();
    let sub = worktree.join("sub");
    fs::create_dir(&sub).unwrap();
    let link = world.base.join("link");
    symlink(&sub, &link).unwrap();
    let project = world.init(&main, "main");
    // through an alias into the worktree: its target's place, and the worktree's branch
    let started = world.start(Call {
        entry: Some(&link),
        ..call()
    });
    assert_eq!(started, world.registered_to(&project, key()));
    let row = world.row();
    assert_eq!(
        (row.entry_cwd.as_str(), row.branch.as_deref()),
        (text(&sub), Some("feature"))
    );
    // in the main tree itself
    world.start(Call {
        entry: Some(&main),
        ..by(other_key())
    });
    assert_eq!(
        world.row_of(&other_key()).unwrap().branch.as_deref(),
        Some("main")
    );
    // a pending session's candidate is found the same way
    let third = claude("session-3");
    world.prompt(
        "hi",
        Call {
            entry: Some(&sub),
            ..by(third.clone())
        },
    );
    let row = world.row_of(&third).unwrap();
    assert_eq!(
        (row.candidate_id, row.branch.as_deref()),
        (Some(project.id), Some("feature"))
    );
}

#[test]
fn register_maps_a_linked_worktree_entry_to_the_main_trees_project() {
    let world = world();
    let (main, worktree) = world.repository();
    let sub = worktree.join("sub");
    fs::create_dir(&sub).unwrap();
    let started = world.start(Call {
        entry: Some(&sub),
        ..call()
    });
    assert_eq!(started.state, ProjectContextState::None);
    let project = world.init(&main, "main");
    let registered = world
        .services
        .session
        .register_session(&key(), None)
        .unwrap();
    assert_eq!(registered, world.registered_to(&project, key()));
    assert_eq!(world.row().entry_cwd, text(&sub));
}

/// A format version git refuses: nothing about the repository is known.
fn refused_by_git(main: &Path) {
    let config = main.join(".git/config");
    let written = fs::read_to_string(&config).unwrap();
    let damaged = written.replace(
        "repositoryformatversion = 0",
        "repositoryformatversion = 99",
    );
    assert_ne!(damaged, written);
    fs::write(&config, damaged).unwrap();
}

#[test]
fn a_session_in_a_repository_git_refuses_is_registered_with_no_project_and_the_degraded_diagnostic()
{
    let world = world();
    let (main, worktree) = world.repository();
    world.init(&main, "main");
    refused_by_git(&main);
    for (entry, key) in [(&main, key()), (&worktree, claude("in-the-worktree"))] {
        let started = world.start(Call {
            entry: Some(entry),
            ..by(key.clone())
        });
        assert_eq!(started.state, ProjectContextState::Degraded);
        assert_eq!(
            started.header,
            "project: unavailable \u{2014} this directory could not be matched to one project \
             (the git worktree could not be mapped to its main working tree); ask the user to \
             run memriver project explain"
        );
        // the project the files alone would have named is not used, and nothing is stored
        assert_eq!(
            started.read_write_set,
            ReadWriteSet {
                project_id: None,
                global_project_id: world.global_id.clone()
            }
        );
        assert_eq!(started.session_key, Some(key.clone()));
        let observed = world.prompt(
            "hi",
            Call {
                entry: Some(entry),
                ..by(key.clone())
            },
        );
        assert_eq!(
            (observed.context, observed.created_pending),
            (started, false)
        );
        assert_eq!(world.row_of(&key), None);
    }
    assert_eq!(
        world
            .raw()
            .query_row("SELECT count(*) FROM sessions", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn a_session_in_a_repository_with_a_damaged_branch_ref_gets_its_project_and_a_null_branch() {
    let world = world();
    let (main, worktree) = world.repository();
    let project = world.init(&main, "main");
    fs::write(main.join(".git/refs/heads/feature"), "not-a-sha\n").unwrap();
    let started = world.start(Call {
        entry: Some(&worktree),
        ..call()
    });
    assert_eq!(started, world.registered_to(&project, key()));
    let row = world.row();
    assert_eq!((row.project_id, row.branch), (Some(project.id), None));
}

#[test]
fn a_degraded_registration_writes_nothing() {
    // the entry is gone
    let world = world();
    let entry = world.projectless_start("");
    world.init(&entry, "later");
    world.sql(
        "UPDATE sessions SET entry_cwd = ?1",
        &[text(&world.base.join("gone"))],
    );
    let before = world.row();
    // nor is the directory the call came from tried after it
    for current_dir in [None, world.project().root.as_deref()] {
        let context = world
            .services
            .session
            .register_session(&key(), current_dir)
            .unwrap();
        assert_eq!(context.state, ProjectContextState::Degraded);
        assert_eq!(
            (context.read_write_set.project_id, context.session_key),
            (None, Some(key()))
        );
        assert_eq!(world.row(), before);
    }
    // the repository cannot be mapped
    let world = self::world();
    let (main, _) = world.repository();
    let started = world.start(Call {
        entry: Some(&main),
        ..call()
    });
    assert_eq!(started.state, ProjectContextState::None);
    world.init(&main, "main");
    refused_by_git(&main);
    let before = world.row();
    let context = world
        .services
        .session
        .register_session(&key(), None)
        .unwrap();
    assert_eq!(context.state, ProjectContextState::Degraded);
    assert_eq!(world.row(), before);
}

// --- observe_prompt ---

#[test]
fn first_prompt_from_an_unseen_session_creates_pending() {
    let world = world();
    let first = world.prompt("first", call());
    assert_eq!(
        (first.context, first.created_pending, first.moved),
        (world.pending(), true, None)
    );
    let second = world.prompt("second", call());
    assert_eq!(
        (second.context, second.created_pending),
        (world.pending(), false)
    );
    let row = world.row();
    assert_eq!(
        (
            row.status,
            row.origin,
            row.candidate_id.as_ref(),
            row.prompt_count
        ),
        (
            SessionStatus::Pending,
            SessionOrigin::FirstSeen,
            Some(&world.project().id),
            2
        )
    );
    assert_eq!(prompt_text(row.first_prompt.as_ref().unwrap()), "first");
    let recent: Vec<&str> = row.recent_prompts.iter().map(prompt_text).collect();
    assert_eq!(recent, ["first", "second"]);
}

#[test]
fn a_prompt_on_a_registered_row_is_counted_and_keeps_its_registration() {
    let world = world();
    world.start(call());
    let observed = world.hello();
    assert_eq!(
        (observed.context, observed.created_pending),
        (world.registered(), false)
    );
    assert_eq!(
        (world.row().status, world.row().prompt_count),
        (SessionStatus::Registered, 1)
    );
}

#[test]
fn prompt_recording() {
    let world = world();
    let kept = |world: &World, prompt: Option<&Value>, key: &str| {
        let key = claude(key);
        world.prompt_value(prompt, by(key.clone()));
        let row = world.row_of(&key).unwrap();
        assert_eq!(row.prompt_count, 1);
        assert_eq!(row.recent_prompts, [row.first_prompt.clone().unwrap()]);
        row.first_prompt.unwrap().body().clone()
    };
    let omitted = [
        (json!(secret_prompt()), PromptOmission::Secret),
        (json!("x".repeat(65537)), PromptOmission::TooLarge),
        // 65538 bytes in 32769 characters
        (json!("\u{e9}".repeat(32769)), PromptOmission::TooLarge),
        (json!(42), PromptOmission::Invalid),
        (json!(null), PromptOmission::Invalid),
        (json!(["a prompt"]), PromptOmission::Invalid),
        (json!({"text": "a prompt"}), PromptOmission::Invalid),
        (json!(""), PromptOmission::Invalid),
        // nothing left on one line
        (json!(" \n\t\u{2028}"), PromptOmission::Invalid),
    ];
    for (number, (prompt, why)) in omitted.iter().enumerate() {
        let body = kept(&world, Some(prompt), &format!("omitted-{number}"));
        assert_eq!(body, PromptBody::Omitted(*why), "{number}");
    }
    assert_eq!(
        kept(&world, None, "absent"),
        PromptBody::Omitted(PromptOmission::Invalid)
    );
    // at the byte limit it is kept: stored cut at 512 characters, on one line
    let text = |text: String| PromptBody::Text(text);
    assert_eq!(
        kept(&world, Some(&json!("x".repeat(65536))), "limit"),
        text("x".repeat(512))
    );
    let long = format!("a\nb\t{}", "x".repeat(600));
    let stored = kept(&world, Some(&json!(long)), "long");
    assert_eq!(stored, text(cut(&single_line(&long), 512).to_string()));
    assert_eq!(stored, text(format!("a b {}", "x".repeat(508))));
    // 512 characters of four bytes each, not 512 bytes
    let wide = "\u{1f600}".repeat(513);
    assert_eq!(
        kept(&world, Some(&json!(wide)), "wide"),
        text("\u{1f600}".repeat(512))
    );
    assert_eq!(
        kept(&world, Some(&json!("\u{1f}hi\u{1f}")), "edges"),
        text("hi".to_string())
    );
    // the secret is nowhere in the store
    let dump: String = world
        .raw()
        .query_row(
            "SELECT group_concat(coalesce(first_prompt, '') || recent_prompts) FROM sessions",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert!(!dump.contains("ghp_"));
}

/// Text from the hex digits of its bytes, so that no sample here has the shape of a
/// credential where it is written down.
fn unhex(hex: &str) -> String {
    let bytes = (0..hex.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&hex[at..at + 2], 16).unwrap())
        .collect();
    String::from_utf8(bytes).unwrap()
}

#[test]
fn a_preview_the_policy_refuses_is_never_stored() {
    let world = world();
    // the name of a credential, "=" and a made-up value of fourteen letters and
    // digits, with something between them that is no white space until the prompt is
    // folded onto one line
    let (name, value) = (
        unhex("70617373776f7264"),
        unhex("5a7137784b326d5076394c745234"),
    );
    let policy = memriver_core::content_policy::Policy::embedded();
    let mut checked = 0;
    for between in ["\u{1}", "\u{0}\u{8}", "\u{2}\u{3}", "\u{7f}"] {
        for (number, prompt) in [
            format!("{name}{between}={between}{value}"),
            format!("please keep this:\n{name}{between}={between}{value}\nthanks"),
            // the folded line is cut inside the value, and what is left is still one
            format!(
                "{}{name}{between}={between}{value}",
                "x".repeat(495 - between.chars().count())
            ),
        ]
        .iter()
        .enumerate()
        {
            // the policy lets the prompt itself through, and refuses its one line
            assert!(
                policy.check(prompt, prompt.chars().count()).is_none(),
                "{between:?}"
            );
            let line = single_line(prompt);
            assert!(
                policy.check(cut(&line, 512), 512).is_some(),
                "{between:?} {number}"
            );
            let key = claude(&format!("folded-{checked}"));
            world.prompt(prompt, by(key.clone()));
            world.prompt(prompt, by(key.clone()));
            let row = world.row_of(&key).unwrap();
            let secret = PromptBody::Omitted(PromptOmission::Secret);
            assert_eq!(
                row.first_prompt.unwrap().body(),
                &secret,
                "{between:?} {number}"
            );
            assert_eq!(row.recent_prompts.len(), 2);
            assert!(
                row.recent_prompts
                    .iter()
                    .all(|entry| entry.body() == &secret)
            );
            checked += 1;
        }
    }
    assert_eq!(checked, 12);
    // neither column of any row holds the value, whole or cut
    let stored: String = world
        .raw()
        .query_row(
            "SELECT group_concat(coalesce(first_prompt, '') || recent_prompts) FROM sessions",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert!(!stored.contains(&value[..6]) && !stored.contains(&name));
    // and a search for it finds no session
    let found = world
        .services
        .session
        .list_sessions(None, &value, None)
        .unwrap();
    assert_eq!(found, []);
    // a credential beyond what would be kept still leaves nothing of the prompt
    let beyond = format!("{} {}", "x".repeat(600), secret_prompt());
    world.prompt(&beyond, by(claude("beyond")));
    let row = world.row_of(&claude("beyond")).unwrap();
    let secret = PromptBody::Omitted(PromptOmission::Secret);
    assert_eq!(row.first_prompt.unwrap().body(), &secret);
    // a prompt that only names the word is kept as before
    let harmless = format!("how do I reset my {name}?");
    world.prompt(&harmless, by(claude("harmless")));
    let row = world.row_of(&claude("harmless")).unwrap();
    assert_eq!(
        row.first_prompt.unwrap().body(),
        &PromptBody::Text(harmless)
    );
}

#[test]
fn only_the_recent_prompts_are_kept() {
    let world = world();
    for number in 0..7 {
        world.prompt(&format!("p{number}"), call());
    }
    let row = world.row();
    assert_eq!(prompt_text(row.first_prompt.as_ref().unwrap()), "p0");
    let recent: Vec<&str> = row.recent_prompts.iter().map(prompt_text).collect();
    assert_eq!(recent, ["p2", "p3", "p4", "p5", "p6"]);
    assert_eq!(row.prompt_count, 7);
}

// --- end_session / stop_decision ---

#[test]
fn end_marks_the_row_and_an_unknown_session_is_left_alone() {
    let world = world();
    world.start(call());
    let before = world.row();
    world.services.session.end_session(&key()).unwrap();
    let after = world.row();
    assert!(after.ended_at.is_some() && after.last_active_at >= before.last_active_at);
    world.services.session.end_session(&other_key()).unwrap();
    assert_eq!(world.row_of(&other_key()), None);
}

#[test]
fn stop_nudges_after_n_unsaved_prompts_then_every_m() {
    let world = world();
    let context = world.start(call());
    let stop = || world.services.session.stop_decision(&key()).unwrap();
    let mut decisions = Vec::new();
    for _ in 0..10 {
        world.hello();
        decisions.push(stop());
    }
    assert_eq!(
        decisions,
        [
            false, false, false, false, true, false, false, false, false, true
        ]
    );
    world.write(&context, "uv manages python").unwrap();
    for _ in 0..4 {
        world.hello();
        assert!(!stop());
    }
    world.hello();
    assert!(stop());
}

#[test]
fn stop_is_silent_for_pending_projectless_unknown_and_broken_sessions() {
    let world = world();
    let stop = |key: &SessionKey| world.services.session.stop_decision(key).unwrap();
    for _ in 0..6 {
        world.hello(); // pending
    }
    let before = world.row();
    assert!(!stop(&key()));
    assert_eq!(world.row(), before);
    world.start(Call {
        entry: Some(&world.base),
        ..by(other_key())
    }); // registered, no project
    for _ in 0..6 {
        world.prompt("hello", by(other_key()));
    }
    assert!(!stop(&other_key()));
    assert!(!stop(&codex("unknown")));
    // a row that cannot be read
    world.start(by(claude("damaged")));
    world.sql(
        "UPDATE sessions SET recent_prompts = 'not json' WHERE session_id = 'damaged'",
        &[],
    );
    assert!(!stop(&claude("damaged")));
    // and no store at all
    assert!(!storeless().services.session.stop_decision(&key()).unwrap());
}

// --- session_context ---

#[test]
fn session_context_follows_the_stored_row() {
    let world = world();
    let session = &world.services.session;
    let unidentified = |key: Option<SessionKey>| {
        no_project_context(
            ProjectContextState::Unidentified,
            UNIDENTIFIED_HEADER,
            world.global_id.clone(),
            key,
        )
    };
    assert_eq!(session.session_context(None).unwrap(), unidentified(None));
    assert_eq!(world.context(&key()), unidentified(Some(key())));
    world.hello();
    assert_eq!(world.context(&key()), world.pending());
    world.start(Call {
        entry: Some(&world.base),
        ..by(other_key())
    });
    assert_eq!(
        world.context(&other_key()),
        world.no_project(ProjectContextState::None, SESSION_NONE_HEADER, other_key())
    );
    let third = claude("session-3");
    world.start(by(third.clone()));
    assert_eq!(
        world.context(&third),
        world.registered_to(world.project(), third)
    );
}

#[test]
fn a_session_whose_project_is_gone_or_global_is_degraded() {
    let world = world();
    let global_id = world.global_id.clone().unwrap();
    for project_id in ["zzzzzzzzzz", &global_id] {
        world.start(call());
        world.sql("UPDATE sessions SET project_id = ?1", &[project_id]);
        let expected = ProjectContext {
            state: ProjectContextState::Degraded,
            header: SESSION_PROJECT_GONE_HEADER.to_string(),
            read_write_set: ReadWriteSet {
                project_id: None,
                global_project_id: world.global_id.clone(),
            },
            project: None,
            diagnostic: Some("session project missing".to_string()),
            session_key: Some(key()),
        };
        assert_eq!(world.context(&key()), expected);
        // a start answers the same; where the project is gone the row cannot even be
        // touched, which is a store that cannot be used
        let started = world.start(call());
        if project_id == global_id {
            assert_eq!(started, expected);
        } else {
            assert_eq!(started.state, ProjectContextState::Unavailable);
        }
    }
}

/// A store that is there and cannot be used: its database is a link.
fn unusable(world: &World) {
    let database = world.store.join(DATABASE_FILENAME);
    let moved = world.base.join("moved.db");
    fs::rename(&database, &moved).unwrap();
    symlink(&moved, &database).unwrap();
}

#[test]
fn a_store_failure_is_an_unavailable_context() {
    let world = world();
    world.start(call());
    unusable(&world);
    let session = &world.services.session;
    // only a store known to be absent is absent
    assert!(session.store_exists());
    let expected = ProjectContext {
        state: ProjectContextState::Unavailable,
        header: STORE_UNREADABLE_HEADER.to_string(),
        read_write_set: ReadWriteSet {
            project_id: None,
            global_project_id: None,
        },
        project: None,
        diagnostic: None,
        session_key: Some(key()),
    };
    assert_eq!(world.context(&key()), expected);
    assert_eq!(
        session.session_context(None).unwrap().state,
        ProjectContextState::Unavailable
    );
    for source in ["startup", "resume", "compact"] {
        let started = world.start_as(source, at(T_B, &world.work));
        assert_eq!((started.context, started.moved), (expected.clone(), None));
    }
    let observed = world.prompt("hi", at(T_B, &world.work));
    assert_eq!(
        (observed.context, observed.created_pending, observed.moved),
        (expected, false, None)
    );
    assert!(!session.stop_decision(&key()).unwrap());
    // the tool-call mapping never fails its caller
    session.record_tool_call(&key(), "call-1", None).unwrap();
    assert_eq!(session.call_record("claude-code", "call-1").unwrap(), None);
    // what the user asked for by name says so
    assert_eq!(session.end_session(&key()), Err(CoreError::StorageFailure));
    assert_eq!(
        session.confirm_session(&key()),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(
        session.register_session(&key(), None),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(session.moves(&key()), Err(CoreError::StorageFailure));
    assert_eq!(
        session.list_sessions(None, "", None),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(session.bound_sessions(), Err(CoreError::StorageFailure));
}

#[test]
fn a_store_of_an_older_schema_is_named_by_every_call() {
    let world = world();
    world.start(call());
    world
        .raw()
        .execute_batch("PRAGMA user_version = 5")
        .unwrap();
    let session = &world.services.session;
    let older = CoreError::StoreNeedsUpgrade(5);
    let entry = text(&world.work);
    assert_eq!(
        session
            .start_session(&key(), "startup", entry, None, None)
            .unwrap_err(),
        older
    );
    assert_eq!(
        session
            .observe_prompt(&key(), Some(&json!("hi")), entry, None, None)
            .unwrap_err(),
        older
    );
    assert_eq!(session.stop_decision(&key()), Err(older.clone()));
    assert_eq!(session.end_session(&key()), Err(older.clone()));
    assert_eq!(session.session_context(Some(&key())), Err(older.clone()));
    assert_eq!(
        session.record_tool_call(&key(), "call-1", None),
        Err(older.clone().into())
    );
    assert_eq!(
        session.call_record("claude-code", "call-1"),
        Err(older.clone())
    );
    assert_eq!(session.confirm_session(&key()), Err(older.clone()));
    assert_eq!(session.register_session(&key(), None), Err(older));
}

// --- confirm_session ---

#[test]
fn confirming_a_candidate_registers_its_project() {
    let world = world();
    world.hello();
    let session = &world.services.session;
    assert_eq!(session.confirm_session(&key()).unwrap(), world.registered());
    assert_eq!(
        (world.row().status, world.row().project_id.as_ref()),
        (SessionStatus::Registered, Some(&world.project().id))
    );
    // idempotent: a registered row answers unchanged
    assert_eq!(session.confirm_session(&key()).unwrap(), world.registered());
    // an unknown session
    assert_eq!(
        session.confirm_session(&other_key()),
        Err(unavailable("unidentified"))
    );
}

#[test]
fn a_pending_session_without_a_candidate_confirms_to_no_project() {
    let world = world();
    let observed = world.prompt(
        "hello",
        Call {
            entry: Some(&world.base),
            ..call()
        },
    );
    let expected = world.no_project(
        ProjectContextState::Pending,
        PENDING_NO_CANDIDATE_HEADER,
        key(),
    );
    assert_eq!(
        (observed.context, observed.created_pending),
        (expected.clone(), true)
    );
    assert_eq!(world.context(&key()), expected);
    assert_eq!(
        world.services.session.confirm_session(&key()).unwrap(),
        world.none()
    );
    assert_eq!(
        (world.row().status, world.row().project_id),
        (SessionStatus::Registered, None)
    );
}

#[test]
fn confirm_refuses_candidate_changed_and_candidate_moved() {
    let confirm = |world: &World| world.services.session.confirm_session(&key());
    // the candidate's root was pointed elsewhere
    let world = world();
    world.hello();
    let before = world.row();
    let moved = world.base.join("moved");
    fs::rename(&world.work, &moved).unwrap();
    symlink(&moved, &world.work).unwrap();
    assert_eq!(confirm(&world), Err(unavailable("candidate-changed")));
    assert_eq!(world.row(), before);
    // a candidate without its root never confirms to a project without one
    let world = self::world();
    world.hello();
    world.sql(
        "UPDATE projects SET root = NULL WHERE id = ?1",
        &[&world.project().id],
    );
    world.sql("UPDATE sessions SET candidate_root = NULL", &[]);
    assert_eq!(confirm(&world), Err(unavailable("candidate-changed")));
    assert_eq!(world.row().status, SessionStatus::Pending);
    // the project was bound elsewhere since, or is gone
    for change in [
        "UPDATE projects SET root = '/elsewhere' WHERE id = ?1",
        "DELETE FROM projects WHERE id = ?1",
    ] {
        let world = self::world();
        world.hello();
        world.sql(change, &[&world.project().id]);
        assert_eq!(
            confirm(&world),
            Err(unavailable("candidate-changed")),
            "{change}"
        );
        assert_eq!(world.row().status, SessionStatus::Pending);
    }
}

#[test]
fn a_candidate_whose_root_went_missing_in_the_main_tree_still_confirms() {
    let world = world();
    let (main, worktree) = world.repository();
    let (sub, twin) = (main.join("sub"), worktree.join("sub"));
    fs::create_dir(&twin).unwrap();
    let project = world.init(&sub, "sub");
    world.prompt(
        "hi",
        Call {
            entry: Some(&twin),
            ..call()
        },
    );
    let pending = world.context(&key());
    assert_eq!(
        world.services.session.pending_candidate(&pending).unwrap(),
        Some(project.clone())
    );
    fs::remove_dir(&sub).unwrap(); // the main tree's checkout no longer has it
    let context = world.services.session.confirm_session(&key()).unwrap();
    assert_eq!(context, world.registered_to(&project, key()));
}

// --- register_session ---

#[test]
fn register_binds_a_projectless_session_to_a_project_inited_at_its_entry() {
    let world = world();
    let entry = world.projectless_start("");
    let before = world.row();
    let project = world.init(&entry, "later");
    let session = &world.services.session;
    assert_eq!(
        session.register_session(&key(), None).unwrap(),
        world.registered_to(&project, key())
    );
    assert_eq!(world.context(&key()), world.registered_to(&project, key()));
    // only the project changes: where and how the session started stays
    let mut expected = before;
    expected.project_id = Some(project.id);
    assert_eq!(world.row(), expected);
    assert_eq!(session.moves(&key()).unwrap(), []);
}

#[test]
fn register_binds_the_project_inited_at_an_ancestor_of_the_entry() {
    let world = world();
    world.projectless_start("deep/sub");
    let project = world.init(&world.base.join("later"), "later");
    let registered = world
        .services
        .session
        .register_session(&key(), None)
        .unwrap();
    assert_eq!(registered, world.registered_to(&project, key()));
}

#[test]
fn register_with_nothing_covering_changes_nothing() {
    let world = world();
    world.projectless_start("");
    world.init(&world.base.join("x"), "x");
    let uncovered = world.dir("c");
    let before = world.row();
    for current_dir in [None, Some("rel"), Some("/a\0"), Some(text(&uncovered))] {
        let context = world
            .services
            .session
            .register_session(&key(), current_dir)
            .unwrap();
        assert_eq!(context, world.none(), "{current_dir:?}");
        assert_eq!(world.row(), before);
    }
    // a directory the call came from that cannot be resolved is a degraded answer
    let missing = world.base.join("missing");
    let context = world
        .services
        .session
        .register_session(&key(), Some(text(&missing)))
        .unwrap();
    assert_eq!(
        (context.state, context.session_key),
        (ProjectContextState::Degraded, Some(key()))
    );
    assert_eq!(world.row(), before);
}

#[test]
fn register_uses_the_entry_first_and_then_the_directory_of_the_call() {
    let world = world();
    let entry = world.projectless_start("");
    let before = world.row();
    let here = world.init(&world.base.join("x"), "x");
    let below = world.dir("x/sub");
    let session = &world.services.session;
    // nothing covers the entry: the directory the call came from decides
    let registered = session
        .register_session(&key(), Some(text(&below)))
        .unwrap();
    assert_eq!(registered, world.registered_to(&here, key()));
    // a registration, not a move: where the session started stays recorded
    let after = world.row();
    assert_eq!(
        (
            after.entry_cwd.as_str(),
            after.transcript_path,
            after.project_id
        ),
        (text(&entry), before.transcript_path, Some(here.id.clone()))
    );
    assert_eq!(session.moves(&key()).unwrap(), []);
    // the entry wins when a project covers it
    let world = self::world();
    let entry = world.projectless_start("");
    let at_entry = world.init(&entry, "later");
    let elsewhere = world.init(&world.base.join("x"), "x");
    let registered = world
        .services
        .session
        .register_session(&key(), elsewhere.root.as_deref());
    assert_eq!(registered.unwrap(), world.registered_to(&at_entry, key()));
}

#[test]
fn register_makes_a_pending_session_without_a_candidate_registered() {
    let world = world();
    let entry = world.dir("later");
    world.prompt(
        "hello",
        Call {
            entry: Some(&entry),
            ..call()
        },
    );
    let session = &world.services.session;
    let pending = world.no_project(
        ProjectContextState::Pending,
        PENDING_NO_CANDIDATE_HEADER,
        key(),
    );
    assert_eq!(session.register_session(&key(), None).unwrap(), pending);
    assert_eq!(world.row().status, SessionStatus::Pending);
    let project = world.init(&entry, "later");
    assert_eq!(
        session.register_session(&key(), None).unwrap(),
        world.registered_to(&project, key())
    );
    let row = world.row();
    assert_eq!(
        (row.status, row.origin, row.project_id, row.candidate_id),
        (
            SessionStatus::Registered,
            SessionOrigin::FirstSeen,
            Some(project.id),
            None
        )
    );
}

#[test]
fn register_refuses_a_pending_session_with_a_candidate() {
    let world = world();
    world.hello();
    let before = world.row();
    let other = world.init(&world.base.join("other"), "other");
    for current_dir in [None, other.root.as_deref()] {
        let registered = world.services.session.register_session(&key(), current_dir);
        assert_eq!(registered, Err(unavailable("pending")));
    }
    assert_eq!(world.row(), before);
    // and a session that was never seen
    let registered = world.services.session.register_session(&other_key(), None);
    assert_eq!(registered, Err(unavailable("unidentified")));
}

#[test]
fn register_never_changes_a_session_that_has_a_project() {
    let world = world();
    let sub = world.work.join("sub");
    fs::create_dir(&sub).unwrap();
    assert_eq!(
        world.start(Call {
            entry: Some(&sub),
            ..call()
        }),
        world.registered()
    );
    let before = world.row();
    let nearer = world.init(&sub, "sub"); // the entry now has a nearer project
    let registered = world
        .services
        .session
        .register_session(&key(), nearer.root.as_deref());
    assert_eq!(registered.unwrap(), world.registered());
    assert_eq!(world.row(), before);
    // nor is its directory looked at again: one that is gone since changes nothing
    world.sql(
        "UPDATE sessions SET entry_cwd = ?1",
        &[text(&world.base.join("gone"))],
    );
    let registered = world.services.session.register_session(&key(), None);
    assert_eq!(registered.unwrap(), world.registered());
}

#[test]
fn register_ignores_a_candidate_left_on_a_registered_row() {
    let world = world();
    let entry = world.projectless_start("");
    let demo = world.project();
    world.sql(
        "UPDATE sessions SET candidate_id = ?1, candidate_root = ?2",
        &[&demo.id, demo.root.as_deref().unwrap()],
    );
    let project = world.init(&entry, "later");
    let registered = world
        .services
        .session
        .register_session(&key(), None)
        .unwrap();
    assert_eq!(registered, world.registered_to(&project, key()));
    assert_eq!(world.row().project_id, Some(project.id));
}

#[test]
fn a_store_failure_while_registering_propagates() {
    let world = world();
    let entry = world.projectless_start("");
    world.init(&entry, "later");
    world.sql(
        "CREATE TRIGGER no_project BEFORE UPDATE OF project_id ON sessions \
         BEGIN SELECT RAISE(ABORT, 'no'); END",
        &[],
    );
    let registered = world.services.session.register_session(&key(), None);
    assert_eq!(registered, Err(CoreError::StorageFailure));
    assert_eq!(world.row().project_id, None);
}

// --- search_sessions / list_sessions / pending_candidate / entry_of ---

#[test]
fn session_search_sees_only_the_writable_projects_registered_rows() {
    let world = world();
    for number in 0..12 {
        world.start(by(codex(&format!("s{number}"))));
    }
    world.prompt("hello", by(codex("pending")));
    world.start(Call {
        entry: Some(&world.base),
        ..by(codex("unbound"))
    });
    let (session, registered) = (&world.services.session, world.registered());
    let found = |limit| {
        session
            .search_sessions("", &registered, limit)
            .unwrap()
            .len()
    };
    assert_eq!(found(None), 10);
    assert_eq!(found(Some(100)), 12);
    assert_eq!(found(Some(50)), 12);
    assert_eq!(found(Some(0)), 1);
    assert_eq!(found(Some(-3)), 1);
    assert_eq!(found(Some(i64::MAX)), 12);
    let mut ids: Vec<String> = session
        .search_sessions("", &registered, Some(50))
        .unwrap()
        .iter()
        .map(|found| found.key.session_id().to_string())
        .collect();
    ids.sort();
    let mut expected: Vec<String> = (0..12).map(|number| format!("s{number}")).collect();
    expected.sort();
    assert_eq!(ids, expected);
    for context in [
        world.context(&codex("pending")),
        world.context(&codex("unbound")),
        session.session_context(None).unwrap(),
    ] {
        assert_eq!(session.search_sessions("", &context, None).unwrap(), []);
    }
}

#[test]
fn list_sessions_reads_every_row_with_optional_filters() {
    let world = world();
    world.start(by(codex("a")));
    world.prompt("needle here", by(codex("b")));
    let ids = |project_id: Option<&str>, query: &str, limit| -> Vec<String> {
        let listed = world
            .services
            .session
            .list_sessions(project_id, query, limit)
            .unwrap();
        listed
            .iter()
            .map(|found| found.key.session_id().to_string())
            .collect()
    };
    assert_eq!(ids(None, "", None), ["b", "a"]);
    assert_eq!(ids(None, "NEEDLE", None), ["b"]);
    assert_eq!(ids(Some(&world.project().id), "", None), ["a"]);
    assert_eq!(ids(None, "", Some(1)), ["b"]);
    assert_eq!(ids(None, "", Some(0)), [""; 0]);
    // an unknown project is said to be one, like the other human reads
    for unknown in ["zzzzzzzzzz", "not an id"] {
        let listed = world
            .services
            .session
            .list_sessions(Some(unknown), "", None);
        assert_eq!(listed, Err(CoreError::ProjectNotFound(unknown.to_string())));
    }
}

#[test]
fn pending_candidate_and_entry_of_answer_from_the_stored_row() {
    let world = world();
    world.hello();
    let session = &world.services.session;
    let pending = world.context(&key());
    assert_eq!(
        session.pending_candidate(&pending).unwrap().as_ref(),
        Some(world.project())
    );
    assert_eq!(
        session.entry_of(&pending).unwrap().as_deref(),
        Some(text(&world.work))
    );
    world.prompt(
        "hello",
        Call {
            entry: Some(&world.base),
            ..by(other_key())
        },
    );
    assert_eq!(
        session
            .pending_candidate(&world.context(&other_key()))
            .unwrap(),
        None
    );
    // a context that came from a directory names no session
    let directory = world
        .services
        .project
        .open_project_context(&world.work)
        .unwrap();
    assert_eq!(session.pending_candidate(&directory).unwrap(), None);
    assert_eq!(session.entry_of(&directory).unwrap(), None);
    // confirmed: nothing is pending any more, the entry is still known
    session.confirm_session(&key()).unwrap();
    assert_eq!(
        session.pending_candidate(&world.context(&key())).unwrap(),
        None
    );
    assert_eq!(
        session.entry_of(&world.context(&key())).unwrap().as_deref(),
        Some(text(&world.work))
    );
}

// --- tool calls ---

#[test]
fn a_recorded_tool_call_names_its_session() {
    let world = world();
    world.start(call());
    let session = &world.services.session;
    let third = claude("session-3");
    session
        .record_tool_call(&third, "call-1", Some("/w"))
        .unwrap();
    let record = session
        .call_record("claude-code", "call-1")
        .unwrap()
        .unwrap();
    assert_eq!((record.key, record.cwd.as_deref()), (third, Some("/w")));
    assert_eq!(session.call_record("claude-code", "call-2").unwrap(), None);
    // an id no call can have is the caller's mistake
    let invalid = session.record_tool_call(&key(), "a b", None);
    assert!(matches!(invalid, Err(WriteError::Value(_))));
    // the mapping never fails its caller
    world.sql("DROP TABLE tool_calls", &[]);
    session.record_tool_call(&key(), "call-3", None).unwrap();
    assert_eq!(session.call_record("claude-code", "call-1").unwrap(), None);
    // and without a store nothing is recorded or made
    let nowhere = storeless();
    nowhere
        .services
        .session
        .record_tool_call(&key(), "call-1", None)
        .unwrap();
    assert_eq!(
        nowhere
            .services
            .session
            .call_record("claude-code", "call-1")
            .unwrap(),
        None
    );
    assert!(!nowhere.store.exists());
}

// --- a /cd moves the session (Claude Code) ---

#[test]
fn cd_moves_registered_session_to_other_project() {
    let world = world();
    let context = world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let memory = world.write(&context, "uv manages python").unwrap();
    assert_eq!(
        world.prompt("before the move", at(T_A, &world.work)).moved,
        None
    );
    let (main, _) = world.repository();
    let b = world.init(&main, "b");
    let before = world.row();
    let observed = world.prompt("after the move", at(T_B, &main));
    assert_eq!(
        (observed.context, observed.created_pending),
        (world.registered_to(&b, key()), false)
    );
    let expected = MoveOutcome {
        from_project_id: Some(world.project().id.clone()),
        to_project_id: Some(b.id.clone()),
        from_candidate_id: None,
        to_candidate_id: None,
        to_entry_cwd: text(&main).to_string(),
        pending: false,
        degraded: false,
    };
    assert_eq!(observed.moved, Some(expected));
    assert!(changed(&observed.moved));
    let after = world.row();
    assert_eq!(
        world.whereabouts(&key()),
        (
            SessionStatus::Registered,
            Some(b.id.clone()),
            None,
            text(&main).to_string(),
            Some("main".to_string()),
            Some(T_B.to_string())
        )
    );
    assert!(after.last_active_at > before.last_active_at);
    assert_eq!(after.prompt_count, before.prompt_count + 1);
    let session = &world.services.session;
    let moves = session.moves(&key()).unwrap();
    assert_eq!(moves.len(), 1);
    assert_eq!(
        (
            moves[0].seq,
            moves[0].from_entry_cwd.as_str(),
            moves[0].from_project_id.as_ref(),
            moves[0].to_entry_cwd.as_str(),
            moves[0].to_project_id.as_ref()
        ),
        (
            1,
            text(&world.work),
            Some(&world.project().id),
            text(&main),
            Some(&b.id)
        )
    );
    // what was written before the move stays in A; the row itself now belongs to B
    let shown = world.services.memory.show(&memory.id, false).unwrap();
    assert_eq!(shown.project_id, world.project().id);
    let found = session
        .search_sessions("before", &world.context(&key()), None)
        .unwrap();
    assert_eq!(found.len(), 1);
    assert_eq!(
        session
            .search_sessions("before", &world.registered(), None)
            .unwrap(),
        []
    );
}

#[test]
fn cd_into_unregistered_dir_unbinds() {
    let world = world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let unbound = world.dir("c");
    let observed = world.prompt("hello", at(T_B, &unbound));
    assert_eq!(observed.context, world.none());
    let moved = observed.moved.unwrap();
    assert!(moved.changed() && !moved.degraded);
    assert_eq!(
        (moved.from_project_id.as_ref(), moved.to_project_id),
        (Some(&world.project().id), None)
    );
    assert_eq!(
        world.whereabouts(&key()),
        (
            SessionStatus::Registered,
            None,
            None,
            text(&unbound).to_string(),
            None,
            Some(T_B.to_string())
        )
    );
    // then a project is made there, and the session asks for it
    let project = world.init(&unbound, "c");
    let registered = world
        .services
        .session
        .register_session(&key(), None)
        .unwrap();
    assert_eq!(registered, world.registered_to(&project, key()));
}

#[test]
fn cd_within_same_project_updates_entry_without_change() {
    let world = world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let sub = world.dir("work/sub");
    let observed = world.prompt("hello", at(T_B, &sub));
    assert_eq!(observed.context, world.registered());
    assert!(observed.moved.is_some() && !changed(&observed.moved));
    assert_eq!(
        world.whereabouts(&key()),
        (
            SessionStatus::Registered,
            Some(world.project().id.clone()),
            None,
            text(&sub).to_string(),
            None,
            Some(T_B.to_string())
        )
    );
    assert_eq!(world.services.session.moves(&key()).unwrap().len(), 1);
}

#[test]
fn move_into_an_unresolvable_directory_is_degraded_no_project() {
    // a repository git refuses, with a project bound there: the move goes to "no
    // project" where /cd went, and the session is never left on the old one
    let world = world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let (main, _) = world.repository();
    let b = world.init(&main, "b");
    refused_by_git(&main);
    let spelled = format!("{}/.", text(&main));
    let observed = world.prompt(
        "hello",
        Call {
            transcript: Some(T_B),
            cwd: Some(&spelled),
            ..call()
        },
    );
    assert_eq!(observed.context, world.none());
    let expected = MoveOutcome {
        from_project_id: Some(world.project().id.clone()),
        to_project_id: None,
        from_candidate_id: None,
        to_candidate_id: None,
        to_entry_cwd: spelled.clone(),
        pending: false,
        degraded: true,
    };
    assert_eq!(observed.moved, Some(expected));
    assert!(changed(&observed.moved));
    // the directory as it was given, no branch
    assert_eq!(
        world.whereabouts(&key()),
        (
            SessionStatus::Registered,
            None,
            None,
            spelled.clone(),
            None,
            Some(T_B.to_string())
        )
    );
    let moves = world.services.session.moves(&key()).unwrap();
    assert_eq!(
        (
            moves[0].to_entry_cwd.as_str(),
            moves[0].to_project_id.as_ref()
        ),
        (spelled.as_str(), None)
    );
    // a later shell cd into a project does not retarget: the transcript stays where
    // /cd put it
    let before = world.row();
    let later = world.prompt("hello", at(T_B, &world.work));
    assert_eq!(
        (later.context.read_write_set.project_id, later.moved),
        (None, None)
    );
    assert_eq!(world.whereabouts(&key()).3, spelled);
    assert_eq!(world.row().entry_cwd, before.entry_cwd);
    assert_eq!(world.services.session.moves(&key()).unwrap().len(), 1);
    assert_ne!(b.id, world.project().id);
    // a directory that is not there at all is degraded the same way
    let world = self::world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let gone = world.base.join("gone");
    let observed = world.prompt("hello", at(T_B, &gone));
    assert!(observed.moved.is_some_and(|moved| moved.degraded));
    assert_eq!(world.whereabouts(&key()).3, text(&gone));
    assert_eq!(world.row().project_id, None);
}

#[test]
fn cd_move_needs_claude_code_a_changed_transcript_directory_and_an_absolute_cwd() {
    let world = world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let b = world.init(&world.base.join("b"), "b");
    let root = Path::new(b.root.as_deref().unwrap());
    let before = world.whereabouts(&key());
    let unmoved = |observed: PromptOutcome| {
        assert_eq!(
            (observed.context, observed.created_pending, observed.moved),
            (world.registered(), false, None)
        );
        assert_eq!(world.whereabouts(&key()), before);
        assert_eq!(world.services.session.moves(&key()).unwrap(), []);
    };
    // a shell cd: the transcript stays where it is
    unmoved(world.prompt("hello", at(T_A, root)));
    // another file in the same directory, and no transcript at all
    unmoved(world.prompt("hello", at("/t/a/other.jsonl", root)));
    unmoved(world.prompt(
        "hello",
        Call {
            cwd: b.root.as_deref(),
            ..call()
        },
    ));
    unmoved(world.prompt("hello", at("rel/s.jsonl", root)));
    // the transcript's directory changed, and the working directory cannot be used: the
    // stored path stays the old one, so the next event with a usable one applies it
    for cwd in [None, Some("rel"), Some("/a\0"), Some("")] {
        unmoved(world.prompt(
            "hello",
            Call {
                transcript: Some(T_B),
                cwd,
                ..call()
            },
        ));
        let compacted = world.start_as(
            "compact",
            Call {
                transcript: Some(T_B),
                cwd,
                ..call()
            },
        );
        assert_eq!(
            (compacted.context, compacted.moved),
            (world.registered(), None)
        );
        assert_eq!(world.whereabouts(&key()), before);
    }
    let observed = world.prompt("hello", at(T_B, root));
    assert_eq!(observed.context, world.registered_to(&b, key()));
    assert!(changed(&observed.moved));
    assert_eq!(world.row().project_id, Some(b.id.clone()));
    // a Codex row never moves
    world.start(Call {
        transcript: Some(T_A),
        ..by(other_key())
    });
    let before = world.whereabouts(&other_key());
    let observed = world.prompt(
        "hello",
        Call {
            key: other_key(),
            ..at(T_B, root)
        },
    );
    assert_eq!(observed.moved, None);
    let compacted = world.start_as(
        "compact",
        Call {
            key: other_key(),
            ..at(T_B, root)
        },
    );
    assert_eq!(compacted.moved, None);
    assert_eq!(world.whereabouts(&other_key()), before);
    assert_eq!(world.services.session.moves(&other_key()).unwrap(), []);
}

fn prompt_at(row: &Session) -> &str {
    row.recent_prompts.last().unwrap().at()
}

#[test]
fn one_event_moves_the_activity_time_once() {
    // while the clock runs forward the times of one event stand to each other as the
    // Python implementation leaves them
    let world = world();
    // a first prompt: the row keeps the time it was made with
    world.prompt("first", at(T_A, &world.work));
    let row = world.row();
    assert_eq!(row.started_at, row.last_active_at);
    assert!(prompt_at(&row) <= row.started_at.as_str());
    // a prompt of its own moves it to the prompt's time
    world.prompt("second", at(T_A, &world.work));
    let row = world.row();
    assert_eq!(row.last_active_at, prompt_at(&row));
    assert!(row.last_active_at > row.started_at);
    // a prompt that moves the session: the move's time, once
    let b = world.init(&world.base.join("b"), "b");
    let root = Path::new(b.root.as_deref().unwrap());
    assert!(changed(&world.prompt("third", at(T_B, root)).moved));
    let (row, moves) = (world.row(), world.services.session.moves(&key()).unwrap());
    assert_eq!(moves[0].moved_at, row.last_active_at);
    assert!(prompt_at(&row) <= row.last_active_at.as_str());
    // a compact that moves it: the start's own time, which is not before the move's
    let compacted = world.start_as("compact", at(T_A, &world.work));
    assert!(changed(&compacted.moved));
    let (row, moves) = (world.row(), world.services.session.moves(&key()).unwrap());
    assert!(moves[1].moved_at <= row.last_active_at);
}

#[test]
fn an_event_while_the_clock_is_behind_moves_the_activity_time_by_one_step() {
    // the session was last active at a time the clock has not reached
    let ahead = "2999-01-01T00:00:00.000000Z";
    let step = |steps: u32| format!("2999-01-01T00:00:00.{steps:06}Z");
    let world = world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    world.sql("UPDATE sessions SET last_active_at = ?1", &[ahead]);
    let session = &world.services.session;
    // a prompt without a move: a summary made from the old reading is refused
    world.prompt("hello", at(T_A, &world.work));
    assert_eq!(world.row().last_active_at, step(1));
    let published = session.publish_summary(&key(), "made from the old reading", ahead);
    assert_eq!(published.unwrap_err(), CoreError::SessionMoved.into());
    assert_eq!(world.row().summary, None);
    // a prompt that moves the session: one step for the move and the prompt together
    let b = world.init(&world.base.join("b"), "b");
    let root = Path::new(b.root.as_deref().unwrap());
    assert!(changed(&world.prompt("hello", at(T_B, root)).moved));
    let moves = session.moves(&key()).unwrap();
    assert_eq!(
        (world.row().last_active_at, moves[0].moved_at.clone()),
        (step(2), step(2))
    );
    // a compact that moves it back: one step again
    assert!(changed(
        &world.start_as("compact", at(T_A, &world.work)).moved
    ));
    assert_eq!(world.row().last_active_at, step(3));
    // every other event is one step each
    world.start_as("resume", at(T_A, &world.work));
    assert_eq!(world.row().last_active_at, step(4));
    session.stop_decision(&key()).unwrap();
    session.end_session(&key()).unwrap();
    assert_eq!(world.row().last_active_at, step(6));
    // a first prompt of a session that is new has no reading to end: its own time
    let observed = world.prompt("hello", by(claude("new")));
    assert!(observed.created_pending);
    let row = world.row_of(&claude("new")).unwrap();
    assert_eq!(row.started_at, row.last_active_at);
    assert!(row.last_active_at.as_str() < ahead);
}

#[test]
fn only_a_compact_start_applies_a_move() {
    let world = world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let b = world.init(&world.base.join("b"), "b");
    let root = Path::new(b.root.as_deref().unwrap());
    let before = world.whereabouts(&key());
    // the transcript's directory changed and the working directory can be used: any
    // other start on the row leaves the session where it is
    for source in ["startup", "clear", "fork", "", "Compact", "something new"] {
        let started = world.start_as(source, at(T_B, root));
        assert_eq!(
            (started.context, started.moved),
            (world.registered(), None),
            "{source}"
        );
        assert_eq!(world.whereabouts(&key()), before, "{source}");
        assert_eq!(world.services.session.moves(&key()).unwrap(), []);
    }
    let count = |table: &str| -> i64 {
        let sql = format!("SELECT count(*) FROM {table}");
        world.raw().query_row(&sql, [], |row| row.get(0)).unwrap()
    };
    assert_eq!(count("session_moves"), 0);
    // and the signal is still there for the compact that follows
    assert!(changed(&world.start_as("compact", at(T_B, root)).moved));
    assert_eq!(count("session_moves"), 1);
}

#[test]
fn a_session_search_never_gives_more_than_fifty() {
    let world = world();
    for number in 0..51 {
        world.start(by(codex(&format!("s{number}"))));
    }
    let (session, registered) = (&world.services.session, world.registered());
    assert_eq!(session.list_sessions(None, "", None).unwrap().len(), 51);
    for limit in [50, 51, i64::MAX] {
        let found = session.search_sessions("", &registered, Some(limit));
        assert_eq!(found.unwrap().len(), 50, "{limit}");
    }
    assert_eq!(
        session
            .search_sessions("", &registered, Some(49))
            .unwrap()
            .len(),
        49
    );
}

#[test]
fn transcript_same_dir_other_spelling_is_not_a_move() {
    let world = world();
    let real = world.dir("projects/x");
    symlink(world.base.join("projects"), world.base.join("alias")).unwrap();
    let stored = format!("{}/s.jsonl", text(&real));
    world.start(Call {
        transcript: Some(&stored),
        ..call()
    });
    let b = world.init(&world.base.join("b"), "b");
    let base = text(&world.base);
    for spelling in [
        format!("{base}/alias/x/s.jsonl"),
        format!("{base}/projects/./x/s.jsonl"),
        format!("{base}/projects/x/../x/s.jsonl"),
    ] {
        let observed = world.prompt(
            "hello",
            Call {
                transcript: Some(&spelling),
                cwd: b.root.as_deref(),
                ..call()
            },
        );
        assert_eq!(
            (observed.context, observed.moved),
            (world.registered(), None)
        );
    }
    assert_eq!(world.row().transcript_path, Some(stored));
    assert_eq!(world.services.session.moves(&key()).unwrap(), []);
}

#[test]
fn a_transcript_that_was_never_stored_is_adopted_and_then_a_cd_moves() {
    let world = world();
    world.start_as("startup", call());
    assert_eq!(world.row().transcript_path, None);
    let b = world.init(&world.base.join("b"), "b");
    let root = Path::new(b.root.as_deref().unwrap());
    // only an absolute path is adopted
    world.prompt(
        "hello",
        Call {
            transcript: Some("rel.jsonl"),
            cwd: Some(text(&world.work)),
            ..call()
        },
    );
    assert_eq!(world.row().transcript_path, None);
    assert_eq!(world.prompt("hello", at(T_A, &world.work)).moved, None);
    assert_eq!(world.row().transcript_path.as_deref(), Some(T_A));
    assert_eq!(world.services.session.moves(&key()).unwrap(), []);
    let observed = world.prompt("hello", at(T_B, root));
    assert_eq!(observed.context, world.registered_to(&b, key()));
    assert_eq!(
        (
            world.row().project_id,
            world.row().transcript_path.as_deref()
        ),
        (Some(b.id), Some(T_B))
    );
}

#[test]
fn compact_backfills_null_transcript_only_when_null() {
    let world = world();
    world.start_as("startup", call());
    let compact = |transcript| {
        world.start_as(
            "compact",
            Call {
                transcript: Some(transcript),
                cwd: Some(text(&world.work)),
                ..call()
            },
        )
    };
    assert_eq!(compact("rel.jsonl").context, world.registered());
    assert_eq!(world.row().transcript_path, None); // only an absolute path is adopted
    assert_eq!(compact(T_A).moved, None);
    assert_eq!(world.row().transcript_path.as_deref(), Some(T_A));
    // a path that is already stored is never overwritten by a compact
    compact("/t/a/other.jsonl");
    assert_eq!(world.row().transcript_path.as_deref(), Some(T_A));
    // nor by an ordinary start
    world.start(Call {
        transcript: Some("/t/a/third.jsonl"),
        ..call()
    });
    assert_eq!(world.row().transcript_path.as_deref(), Some(T_A));
}

#[test]
fn compact_detects_move_before_touch() {
    let world = world();
    world.start(Call {
        transcript: Some(T_A),
        ..call()
    });
    let b = world.init(&world.base.join("b"), "b");
    let root = b.root.clone().unwrap();
    let compacted = world.start_as("compact", at(T_B, Path::new(&root)));
    assert_eq!(compacted.context, world.registered_to(&b, key()));
    assert!(changed(&compacted.moved));
    assert_eq!(compacted.moved.unwrap().to_project_id, Some(b.id.clone()));
    assert_eq!(
        world.whereabouts(&key()),
        (
            SessionStatus::Registered,
            Some(b.id),
            None,
            root,
            None,
            Some(T_B.to_string())
        )
    );
    assert_eq!(world.services.session.moves(&key()).unwrap().len(), 1);
}

#[test]
fn pending_session_moves_candidate() {
    let world = world();
    world.prompt("hello", at(T_A, &world.work));
    let b = world.init(&world.base.join("b"), "b");
    let root = b.root.clone().unwrap();
    let observed = world.prompt("hello", at(T_B, Path::new(&root)));
    assert_eq!(
        (observed.context, observed.created_pending),
        (world.pending(), false)
    );
    let expected = MoveOutcome {
        from_project_id: None,
        to_project_id: None,
        from_candidate_id: Some(world.project().id.clone()),
        to_candidate_id: Some(b.id.clone()),
        to_entry_cwd: root.clone(),
        pending: true,
        degraded: false,
    };
    assert_eq!(observed.moved, Some(expected));
    assert!(changed(&observed.moved));
    let row = world.row();
    assert_eq!(
        (
            row.status,
            row.candidate_id.as_ref(),
            row.candidate_root.as_ref(),
            row.entry_cwd.as_str()
        ),
        (
            SessionStatus::Pending,
            Some(&b.id),
            Some(&root),
            root.as_str()
        )
    );
    let moves = world.services.session.moves(&key()).unwrap();
    assert_eq!(
        (
            moves[0].from_project_id.as_ref(),
            moves[0].to_project_id.as_ref()
        ),
        (None, None)
    );
    let confirmed = world.services.session.confirm_session(&key()).unwrap();
    assert_eq!(confirmed, world.registered_to(&b, key()));
}

#[test]
fn confirmed_row_moved_unbound_then_registered() {
    let world = world();
    world.prompt("hello", at(T_A, &world.work));
    world.services.session.confirm_session(&key()).unwrap();
    let id = Some(world.project().id.clone());
    assert_eq!(
        (world.row().project_id, world.row().candidate_id),
        (id.clone(), id)
    );
    let unbound = world.dir("c");
    assert_eq!(
        world.prompt("hello", at(T_B, &unbound)).context,
        world.none()
    );
    // the candidate it was confirmed with is cleared by the move
    assert_eq!(
        world.whereabouts(&key()),
        (
            SessionStatus::Registered,
            None,
            None,
            text(&unbound).to_string(),
            None,
            Some(T_B.to_string())
        )
    );
    let project = world.init(&unbound, "c");
    let registered = world
        .services
        .session
        .register_session(&key(), None)
        .unwrap();
    assert_eq!(registered, world.registered_to(&project, key()));
}

#[test]
fn early_returns_carry_no_outcome() {
    let nowhere = storeless();
    let work = nowhere.work.clone();
    assert_eq!(nowhere.start_as("compact", at(T_B, &work)).moved, None);
    let observed = nowhere.prompt("hi", at(T_B, &work));
    assert_eq!((observed.created_pending, observed.moved), (false, None));
    // a new row
    let world = world();
    let observed = world.prompt("hi", at(T_A, &world.work));
    assert_eq!((observed.created_pending, observed.moved), (true, None));
    let started = world.start_as(
        "compact",
        Call {
            key: claude("session-3"),
            ..at(T_A, &world.work)
        },
    );
    assert_eq!(started.moved, None);
}

// --- the published summary ---

#[test]
fn publish_summary_checks_policy_length_and_last_active() {
    let world = world();
    world.start(call());
    let session = &world.services.session;
    let read = world.row().last_active_at;
    let publish = |text: &str, read: &str| session.publish_summary(&key(), text, read);
    // the content policy and the length limit, before the session is looked at
    for (text, rule_id) in [
        (secret_prompt(), "github-pat"),
        ("x".repeat(1201), "too-large"),
        (" \n".to_string(), "empty"),
    ] {
        match publish(&text, &read).unwrap_err() {
            WriteError::Core(CoreError::ContentRejected {
                rule_id: refused, ..
            }) => assert_eq!(refused, rule_id),
            other => panic!("{other:?}"),
        }
        match session
            .publish_summary(&codex("never-seen"), &text, &read)
            .unwrap_err()
        {
            WriteError::Core(CoreError::ContentRejected { .. }) => {}
            other => panic!("{other:?}"),
        }
    }
    assert_eq!(world.row().summary, None);
    // exactly the limit is published, with its time, and a search finds it
    let longest = format!("Rotated the staging certificates. {}", "x".repeat(1166));
    assert_eq!(longest.chars().count(), 1200);
    publish(&longest, &read).unwrap();
    let row = world.row();
    assert_eq!(row.summary, Some(longest));
    assert!(row.summary_at.is_some_and(|published| published >= read));
    assert_eq!(row.last_active_at, read);
    let found = session
        .search_sessions("CERTIFICATES", &world.registered(), None)
        .unwrap();
    assert_eq!(found.len(), 1);
    // a session that was active since it was read: refused, the summary stays
    world.hello();
    assert_eq!(
        publish("a newer summary", &read).unwrap_err(),
        CoreError::SessionMoved.into()
    );
    assert!(
        world
            .row()
            .summary
            .is_some_and(|summary| summary.starts_with("Rotated"))
    );
    publish("a newer summary", &world.row().last_active_at).unwrap();
    assert_eq!(world.row().summary.as_deref(), Some("a newer summary"));
    // unbound, pending and unknown sessions have none
    world.start(Call {
        entry: Some(&world.base),
        ..by(other_key())
    });
    world.prompt("hello", by(claude("pending")));
    for key in [other_key(), claude("pending"), codex("never-seen")] {
        let read = world
            .row_of(&key)
            .map(|row| row.last_active_at)
            .unwrap_or(read.clone());
        let published = session.publish_summary(&key, "a summary", &read);
        assert_eq!(published.unwrap_err(), CoreError::SessionMoved.into());
    }
    // and without a store there is no such session
    let nowhere = storeless();
    let published = nowhere
        .services
        .session
        .publish_summary(&key(), "a summary", &read);
    assert_eq!(published.unwrap_err(), CoreError::SessionMoved.into());
    assert!(!nowhere.store.exists());
}

#[test]
fn bound_sessions_lists_only_sessions_bound_to_a_project() {
    let world = world();
    world.start(by(codex("bound")));
    world.start(Call {
        entry: Some(&world.base),
        ..by(codex("unbound"))
    });
    world.prompt("hello", by(codex("pending")));
    let bound = world.services.session.bound_sessions().unwrap();
    let ids: Vec<&str> = bound.iter().map(|found| found.key.session_id()).collect();
    assert_eq!(ids, ["bound"]);
}

// --- damage ---

#[test]
fn a_damaged_prompt_column_is_a_storage_failure_or_a_skipped_row_on_every_read_path() {
    let world = world();
    world.start(call());
    world.hello();
    world.start(by(codex("good")));
    let registered = world.registered();
    let fixture = include_str!("fixtures/damaged_columns.jsonl");
    let mut checked = 0;
    for line in fixture.lines() {
        let sample: Value = serde_json::from_str(line).unwrap();
        let (column, raw) = (
            sample["column"].as_str().unwrap(),
            sample["raw"].as_str().unwrap(),
        );
        if column == "tags" {
            continue;
        }
        let sql = format!("UPDATE sessions SET {column} = ?1 WHERE session_id = 'session-1'");
        assert_eq!(world.raw().execute(&sql, [raw]).unwrap(), 1);
        checked += 1;
        // each service is built anew, as each hook and each tool call is
        let services = world.again();
        let session = &services.session;
        let entry = text(&world.work);
        let unreadable = |context: &ProjectContext| {
            (context.state, context.header.as_str())
                == (ProjectContextState::Unavailable, STORE_UNREADABLE_HEADER)
        };
        let mut said = Vec::new();
        let context = session.session_context(Some(&key())).unwrap();
        assert!(unreadable(&context), "{raw:.60}");
        let started = session
            .start_session(&key(), "startup", entry, None, None)
            .unwrap();
        assert!(unreadable(&started.context) && started.moved.is_none());
        let observed = session
            .observe_prompt(&key(), Some(&json!("hi")), entry, None, None)
            .unwrap();
        assert!(unreadable(&observed.context) && !observed.created_pending);
        assert!(!session.stop_decision(&key()).unwrap());
        for failed in [
            session.end_session(&key()).unwrap_err(),
            session.confirm_session(&key()).unwrap_err(),
            session.register_session(&key(), None).unwrap_err(),
            session.pending_candidate(&context).unwrap_err(),
            session.entry_of(&context).map(drop).unwrap_err(),
        ] {
            assert_eq!(failed, CoreError::StorageFailure);
            said.push(failed.to_string());
        }
        let published = session.publish_summary(&key(), "a summary", "2026-11-22T02:16:43.300060Z");
        assert_eq!(
            published.clone().unwrap_err(),
            CoreError::StorageFailure.into()
        );
        said.push(published.unwrap_err().to_string());
        said.push(format!("{context:?}{started:?}{observed:?}"));
        // a pending context of that session: the write is refused as a failure
        let pending = ProjectContext {
            state: ProjectContextState::Pending,
            ..context
        };
        let written =
            services
                .memory
                .record("a fact", "project", false, "claude-code", "", &pending);
        assert_eq!(written.unwrap_err(), CoreError::StorageFailure.into());
        // the lists pass over the row
        let ids = |found: Vec<Session>| -> Vec<String> {
            found
                .iter()
                .map(|found| found.key.session_id().to_string())
                .collect()
        };
        assert_eq!(
            ids(session.list_sessions(None, "", None).unwrap()),
            ["good"]
        );
        assert_eq!(
            ids(session.search_sessions("", &registered, None).unwrap()),
            ["good"]
        );
        assert_eq!(ids(session.bound_sessions().unwrap()), ["good"]);
        // none of the decoder's words is said to anyone
        for words in [
            "JSON",
            "written form",
            "prompt entry",
            "recent_prompts",
            "timestamp",
            "omission",
        ] {
            assert!(
                !said.iter().any(|text| text.contains(words)),
                "{words}: {raw:.60}"
            );
        }
    }
    assert!(checked > 40, "{checked}");
    // the row is as it was planted: nothing wrote over it
    let count: i64 = world
        .raw()
        .query_row(
            "SELECT prompt_count FROM sessions WHERE session_id = 'session-1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}
