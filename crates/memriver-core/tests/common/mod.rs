//! A store in a temporary directory with one bound project and global, and the
//! services over it, for the tests of the memory paths.
#![allow(dead_code)]

use std::fs;
use std::path::PathBuf;

use memriver_core::content_policy::ContentClassifier;
use memriver_core::models::changes::{Change, MetadataCaps, Op};
use memriver_core::models::context::{ProjectContext, ProjectContextState, ReadWriteSet};
use memriver_core::models::errors::{CoreError, WriteError};
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
    Env {
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
        build_services(&self.settings, &self.home, None)
    }

    pub fn classified(&self, classifier: Box<dyn ContentClassifier>) -> Services {
        build_services(&self.settings, &self.home, Some(classifier))
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
