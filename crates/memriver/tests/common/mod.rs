//! A store in a temporary directory with one bound project, global and one memory, and
//! the command line run over it in memory.
#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};

use memriver::cli::{Io, run};
use memriver_core::models::context::ProjectContext;
use memriver_core::models::memory::Memory;
use memriver_core::models::project::Project;
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::settings::{Env, Settings};
use memriver_core::{Services, build_services};
use memriver_test_support::Sandbox;
use rusqlite::Connection;

/// What one invocation left: the exit code and the two streams.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Done {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub struct World {
    _sandbox: Sandbox,
    /// The temporary directory, with its links read.
    pub tmp: PathBuf,
    pub home: PathBuf,
    pub store: PathBuf,
    /// The directory the project `demo` is bound to.
    pub work: PathBuf,
    pub env: Env,
    pub services: Services,
    pub project: Project,
    pub global_id: String,
    /// "line one\nline two", described as "the cue", in `demo`.
    pub memory: Memory,
    /// The context of `work`.
    pub context: ProjectContext,
}

/// Directories only: no store is made.
pub fn empty() -> (Sandbox, PathBuf) {
    let sandbox = Sandbox::new();
    let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
    fs::create_dir_all(tmp.join("home")).unwrap();
    fs::create_dir_all(tmp.join("work")).unwrap();
    (sandbox, tmp)
}

/// The services over `store`, with the default settings.
pub fn services(store: &Path, home: &Path, env: &Env) -> Services {
    build_services(&Settings::light(store.to_path_buf()), home, env, None)
}

pub fn world() -> World {
    let (sandbox, tmp) = empty();
    let (home, store, work) = (tmp.join("home"), tmp.join("mem"), tmp.join("work"));
    let mut env = sandbox.env();
    env.insert("HOME".to_string(), home.to_string_lossy().into_owned());
    env.insert(
        "MEMRIVER_ROOT".to_string(),
        store.to_string_lossy().into_owned(),
    );
    let services = services(&store, &home, &env);
    let global_id = services.project.ensure_global().unwrap();
    let plan = services.project.plan_root(&work, None).unwrap();
    let project = services.project.init_project("demo", &plan).unwrap();
    let context = services.project.open_project_context(&work).unwrap();
    let memory = services
        .memory
        .record(
            "line one\nline two",
            "project",
            true,
            "t",
            "the cue",
            &context,
        )
        .unwrap();
    World {
        _sandbox: sandbox,
        tmp,
        home,
        store,
        work,
        env,
        services,
        project,
        global_id,
        memory,
        context,
    }
}

/// Runs `memriver <args>` through the library entry. `answer` is what stands on stdin;
/// `tty` says whether stdin is a terminal.
pub fn run_in(env: &Env, home: &Path, cwd: &Path, args: &[&str], answer: &str, tty: bool) -> Done {
    let argv: Vec<String> = std::iter::once("memriver")
        .chain(args.iter().copied())
        .map(str::to_string)
        .collect();
    let (mut stdin, mut stdout, mut stderr) = (answer.as_bytes(), Vec::new(), Vec::new());
    let mut io = Io {
        stdin: &mut stdin,
        stdin_is_tty: tty,
        stdout: &mut stdout,
        stderr: &mut stderr,
    };
    let code = run(argv, env, home, cwd, &mut io);
    Done {
        code,
        stdout: String::from_utf8(stdout).unwrap(),
        stderr: String::from_utf8(stderr).unwrap(),
    }
}

impl World {
    /// `memriver <args>` in the project's directory, with no terminal.
    pub fn run(&self, args: &[&str]) -> Done {
        run_in(&self.env, &self.home, &self.work, args, "", false)
    }

    /// The same from `cwd`.
    pub fn run_from(&self, cwd: &Path, args: &[&str]) -> Done {
        run_in(&self.env, &self.home, cwd, args, "", false)
    }

    /// The same at a terminal where `answer` is typed. The prompt `Proceed? [y/N] `
    /// is part of stdout, once for every time it was asked.
    pub fn ask(&self, args: &[&str], answer: &str) -> Done {
        run_in(&self.env, &self.home, &self.work, args, answer, true)
    }

    /// A connection for planting what no command writes.
    pub fn sql(&self) -> Connection {
        Connection::open(self.store.join(DATABASE_FILENAME)).unwrap()
    }

    /// Every file of the store with its bytes.
    pub fn files(&self) -> Vec<(String, Vec<u8>)> {
        let mut files: Vec<(String, Vec<u8>)> = fs::read_dir(&self.store)
            .unwrap()
            .map(|entry| entry.unwrap())
            .map(|entry| {
                let name = entry.file_name().to_string_lossy().into_owned();
                (name, fs::read(entry.path()).unwrap())
            })
            .collect();
        files.sort();
        files
    }
}

/// `text` without the prompts that were asked.
pub fn unprompted(text: &str) -> String {
    text.replace("Proceed? [y/N] ", "")
}
