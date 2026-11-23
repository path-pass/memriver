//! A store in a temporary directory with one bound project and global, and the
//! services over it, for the tests of the memory paths.
#![allow(dead_code)]

use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use memriver_core::content_policy::{ContentClassifier, Verdict};
use memriver_core::models::changes::{Change, MetadataCaps, Op, SourceRef};
use memriver_core::models::context::{ProjectContext, ProjectContextState, ReadWriteSet};
use memriver_core::models::errors::{BatchConflictReason, Cap, CoreError, WriteError};
use memriver_core::models::memory::Memory;
use memriver_core::models::project::Project;
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::repository::sqlite::memory_store::MemoryStore;
use memriver_core::repository::sqlite::project_store::ProjectStore;
use memriver_core::settings::Settings;
use memriver_core::{Services, build_services};
use memriver_test_support::Sandbox;
use rusqlite::Connection;

pub const CAPS: MetadataCaps = MetadataCaps {
    standing_max_count: 10,
    standing_max_chars: 200,
    tags_max: 12,
    tags_per_memory_max: 3,
};

pub struct Env {
    _sandbox: Sandbox,
    pub tmp: PathBuf,
    pub home: PathBuf,
    pub store: PathBuf,
    pub project: Project,
    pub other: Project,
    pub global_id: String,
    pub settings: Settings,
    /// What git is run with.
    pub env: memriver_core::settings::Env,
}

pub fn setup() -> Env {
    let sandbox = Sandbox::new();
    let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
    let (home, store) = (tmp.join("home"), tmp.join("store"));
    fs::create_dir_all(&home).unwrap();
    let projects = ProjectStore::with_busy_timeout(&store, &home, 2000);
    let bound = |name: &str| {
        let directory = tmp.join(name);
        fs::create_dir(&directory).unwrap();
        let project = Project::new(name, 120).unwrap();
        let plan = projects.plan_root(&directory, None).unwrap();
        projects.create(&project, &plan).unwrap();
        project
    };
    let (project, other) = (bound("work"), bound("elsewhere"));
    let global_id = projects.ensure_global().unwrap();
    let settings = Settings::light(store.clone());
    let env = sandbox.env();
    Env {
        env,
        _sandbox: sandbox,
        tmp,
        home,
        store,
        project,
        other,
        global_id,
        settings,
    }
}

/// A context that reads `project_id` and global and writes `project_id`.
pub fn context_of(project_id: Option<&str>, global_id: Option<&str>) -> ProjectContext {
    ProjectContext {
        state: ProjectContextState::Registered,
        header: String::new(),
        read_write_set: ReadWriteSet {
            project_id: project_id.map(str::to_string),
            global_project_id: global_id.map(str::to_string),
        },
        project: None,
        diagnostic: None,
        session_key: None,
    }
}

impl Env {
    pub fn services(&self) -> Services {
        build_services(&self.settings, &self.home, &self.env, None)
    }

    pub fn classified(&self, classifier: Box<dyn ContentClassifier>) -> Services {
        build_services(&self.settings, &self.home, &self.env, Some(classifier))
    }

    /// The services over the same store with some settings changed.
    pub fn limited(&self, change: impl FnOnce(&mut Settings)) -> Services {
        let mut settings = self.settings.clone();
        change(&mut settings);
        build_services(&settings, &self.home, &self.env, None)
    }

    /// The services with a classifier that notes what it is asked and refuses a text
    /// with "ignore" in it; and its notes: each text with who asked.
    pub fn noted(&self) -> (Services, Asked) {
        let asked = Asked::default();
        let noting = Noting {
            asked: Arc::clone(&asked),
            database: self.store.join(DATABASE_FILENAME),
        };
        (self.classified(Box::new(noting)), asked)
    }

    /// One memory a human made on the management path, through the services: its id.
    pub fn made(&self, op: Op) -> String {
        let change = self.services().memory.apply(&[op], "human", None);
        change.unwrap().steps[0].memory_id.clone()
    }

    /// The context of a session in the bound project.
    pub fn context(&self) -> ProjectContext {
        context_of(Some(&self.project.id), Some(&self.global_id))
    }

    pub fn memory_store(&self) -> MemoryStore {
        MemoryStore::with_busy_timeout(&self.store, 2000)
    }

    /// The management path, with a policy that lets everything through.
    pub fn apply(&self, ops: &[Op]) -> Result<Change, WriteError> {
        self.memory_store()
            .apply(ops, "cli", None, &|_, _| None, &CAPS)
    }

    /// One memory written by the management path: its id.
    pub fn manage(&self, op: Op) -> String {
        self.apply(&[op]).unwrap().steps[0].memory_id.clone()
    }

    /// A global memory with a level and tags.
    pub fn global(&self, body: &str, level: Option<i64>, tags: &[&str]) -> String {
        let Op::Create {
            project_id,
            type_,
            description,
            body,
            sources,
            trust,
            sync,
            ..
        } = Op::create(&self.global_id, "feedback", "", body)
        else {
            unreachable!()
        };
        self.manage(Op::Create {
            project_id,
            type_,
            description,
            body,
            sources,
            trust,
            sync,
            level,
            tags: tags.iter().map(|tag| tag.to_string()).collect(),
        })
    }

    /// A connection of someone who is not memriver.
    pub fn raw(&self) -> Connection {
        Connection::open(self.store.join(DATABASE_FILENAME)).unwrap()
    }

    /// The same, with no key enforced: for planting rows no write of memriver makes.
    pub fn unkeyed(&self) -> Connection {
        let conn = self.raw();
        conn.execute_batch("PRAGMA foreign_keys = OFF").unwrap();
        conn
    }

    pub fn count(&self, table: &str) -> i64 {
        self.raw()
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap()
    }

    pub fn record(&self, body: &str) -> Memory {
        self.services()
            .memory
            .record(body, "project", true, "claude-code", "", &self.context())
            .unwrap()
    }
}

pub fn core<T: std::fmt::Debug>(result: Result<T, WriteError>) -> CoreError {
    match result {
        Err(WriteError::Core(err)) => err,
        other => panic!("not a core error: {other:?}"),
    }
}

pub fn value<T: std::fmt::Debug>(result: Result<T, WriteError>) -> String {
    match result {
        Err(WriteError::Value(err)) => err.to_string(),
        other => panic!("not a value error: {other:?}"),
    }
}

// --- operations, written short ---

pub struct Draft {
    pub project_id: String,
    pub body: String,
    pub sources: Vec<SourceRef>,
    pub trust: String,
    pub sync: bool,
    pub level: Option<i64>,
    pub tags: Vec<String>,
}

pub fn draft(project_id: &str, body: &str) -> Draft {
    Draft {
        project_id: project_id.to_string(),
        body: body.to_string(),
        sources: Vec::new(),
        trust: "agent".to_string(),
        sync: true,
        level: None,
        tags: Vec::new(),
    }
}

impl Draft {
    pub fn citing(mut self, sources: &[(&str, i64)]) -> Draft {
        self.sources = refs(sources);
        self
    }

    pub fn trusted(mut self, trust: &str, sync: bool) -> Draft {
        (self.trust, self.sync) = (trust.to_string(), sync);
        self
    }

    pub fn judged(mut self, level: Option<i64>, tags: &[&str]) -> Draft {
        self.level = level;
        self.tags = tags.iter().map(|tag| tag.to_string()).collect();
        self
    }

    pub fn op(self) -> Op {
        Op::Create {
            project_id: self.project_id,
            type_: "project".to_string(),
            description: String::new(),
            body: self.body,
            sources: self.sources,
            trust: self.trust,
            sync: self.sync,
            level: self.level,
            tags: self.tags,
        }
    }
}

pub fn refs(sources: &[(&str, i64)]) -> Vec<SourceRef> {
    sources
        .iter()
        .map(|(memory_id, version)| SourceRef {
            memory_id: memory_id.to_string(),
            version: *version,
        })
        .collect()
}

pub fn update(memory_id: &str, expected_version: i64) -> Op {
    Op::update(memory_id, expected_version)
}

pub fn body(memory_id: &str, expected_version: i64, text: &str) -> Op {
    let Op::Update {
        memory_id,
        expected_version,
        description,
        sources,
        level,
        tags,
        ..
    } = update(memory_id, expected_version)
    else {
        unreachable!()
    };
    Op::Update {
        memory_id,
        expected_version,
        description,
        body: Some(text.to_string()),
        sources,
        level,
        tags,
    }
}

pub fn citing(memory_id: &str, expected_version: i64, sources: &[(&str, i64)]) -> Op {
    Op::Update {
        memory_id: memory_id.to_string(),
        expected_version,
        description: None,
        body: None,
        sources: Some(refs(sources)),
        level: None,
        tags: None,
    }
}

pub fn judged(
    memory_id: &str,
    expected_version: i64,
    level: Option<i64>,
    tags: Option<&[&str]>,
) -> Op {
    Op::Update {
        memory_id: memory_id.to_string(),
        expected_version,
        description: None,
        body: None,
        sources: None,
        level,
        tags: tags.map(|tags| tags.iter().map(|tag| tag.to_string()).collect()),
    }
}

pub fn conflict<T: std::fmt::Debug>(
    result: Result<T, WriteError>,
) -> (usize, Option<String>, BatchConflictReason) {
    match core(result) {
        CoreError::BatchConflict {
            index,
            memory_id,
            reason,
        } => (index, memory_id, reason),
        other => panic!("not a conflict: {other:?}"),
    }
}

pub fn restore(memory_id: &str, expected_version: i64, to_version: i64) -> Op {
    Op::Restore {
        memory_id: memory_id.to_string(),
        expected_version,
        to_version,
    }
}

pub fn described(memory_id: &str, expected_version: i64, description: &str) -> Op {
    let mut op = update(memory_id, expected_version);
    if let Op::Update {
        description: new, ..
    } = &mut op
    {
        *new = Some(description.to_string());
    }
    op
}

pub fn unread(memory_id: &str, expected_version: i64, since: &str) -> Op {
    Op::SoftDelete {
        memory_id: memory_id.to_string(),
        expected_version,
        unread_since: Some(since.to_string()),
    }
}

/// A text the embedded policy refuses, and the rule that refuses it.
pub const SECRET: &str = "aws key AKIAIOSFODNN7EXAMPLE ok";
pub const SECRET_RULE: &str = "aws-access-token";

pub fn rejected<T: std::fmt::Debug>(
    result: Result<T, WriteError>,
) -> (String, String, Option<String>) {
    match core(result) {
        CoreError::ContentRejected {
            message,
            rule_id,
            memory_id,
            ..
        } => (message, rule_id, memory_id),
        other => panic!("not a content refusal: {other:?}"),
    }
}

pub fn exceeded<T: std::fmt::Debug>(result: Result<T, WriteError>) -> (Cap, Option<String>) {
    match core(result) {
        CoreError::CapExceeded { cap, memory_id } => (cap, memory_id),
        other => panic!("not a limit: {other:?}"),
    }
}

pub type Asked = Arc<Mutex<Vec<(String, String)>>>;

struct Noting {
    asked: Asked,
    database: PathBuf,
}

impl ContentClassifier for Noting {
    fn classify(&self, text: &str, changed_by: &str) -> Verdict {
        self.asked
            .lock()
            .unwrap()
            .push((text.to_string(), changed_by.to_string()));
        // a call can take seconds: nobody holds the store's write lock meanwhile
        let conn = Connection::open(&self.database).unwrap();
        conn.busy_timeout(std::time::Duration::ZERO).unwrap();
        conn.execute_batch("BEGIN IMMEDIATE; ROLLBACK")
            .expect("the write lock is free while the classifier is asked");
        if text.contains("ignore") {
            Verdict::Blocked {
                category: "instruction".to_string(),
            }
        } else {
            Verdict::Allowed
        }
    }
}
