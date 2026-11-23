//! `memriver project init | adopt | unbind | explain` through the command line entry:
//! every plan, refusal and result sentence as it is printed, the exit codes, and that no
//! command ever changes anything inside a target directory.

mod common;

use std::fs;
use std::io::{self, BufRead, Read};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};

use common::{Done, run_in};
use memriver::cli::{Io, run};
use memriver_core::Services;
use memriver_core::models::errors::CoreError;
use memriver_core::models::helpers::{is_id, new_id};
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::settings::Env;
use memriver_test_support::Sandbox;
use rusqlite::Connection;

const PROMPT: &str = "Proceed? [y/N] ";
const RESTART: &str = "Restart the affected harness sessions and their memriver MCP servers for \
                       the change to take effect.\n";
const SUBTREE: &str = "Every directory under this root -- including repositories added later -- \
                       will share this project's memories.";
const NOT_A_TERMINAL: &str =
    "refused: stdin is not a terminal; pass --yes to confirm non-interactively\n";
const DECLINED: &str = "aborted; nothing was written\n";
const COVERS_HOME: &str =
    "refused: the filesystem root, the home directory and its ancestors cannot be a project\n";
const OUT_OF_DATE: &str =
    "refused: the plan is out of date (a path changed while waiting); run the command again\n";
const STORE_FAILURE: &str =
    "refused: could not complete the store write; inspect with memriver project explain\n";
const CANNOT_VERIFY: &str = "refused: could not verify the directory's relation to the home \
                             directory, the memory store or the bound directories\n";
const GLOBAL_REFUSAL: &str = "refused: the global project cannot be bound to a directory\n";
const BINDING_CHANGED: &str =
    "refused: the binding changed while waiting; nothing was changed; run the command again\n";
const NEW: &str = "(new; id assigned when written)";

/// A home with a work directory holding two repositories, and the place of a store that
/// does not exist yet.
struct Place {
    sandbox: Sandbox,
    tmp: PathBuf,
    home: PathBuf,
    work: PathBuf,
    store: PathBuf,
}

fn place() -> Place {
    let (sandbox, tmp) = common::empty();
    let home = tmp.join("home");
    let work = home.join("99_git/work");
    for repository in ["frontend", "backend"] {
        fs::create_dir_all(work.join(repository).join(".git")).unwrap();
    }
    let store = home.join("agent-memory");
    Place {
        sandbox,
        tmp,
        home,
        work,
        store,
    }
}

fn s(path: &Path) -> String {
    path.to_str().unwrap().to_string()
}

/// The sandbox's environment with another home and another store.
fn environment(sandbox: &Sandbox, home: &Path, store: &Path) -> Env {
    let mut env = sandbox.env();
    for (key, value) in env.iter_mut() {
        match key.as_str() {
            "PATH" => {}
            key if key.starts_with("MEMRIVER") => *value = s(store),
            _ => *value = s(home),
        }
    }
    env
}

/// A terminal where `y` is typed only after `act` has run: the read of the answer is the
/// time the prompt stands open.
struct Typing<F: FnMut()>(F);

impl<F: FnMut()> Read for Typing<F> {
    fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
        Ok(0)
    }
}

impl<F: FnMut()> BufRead for Typing<F> {
    fn fill_buf(&mut self) -> io::Result<&[u8]> {
        Ok(&[])
    }

    fn consume(&mut self, _: usize) {}

    fn read_line(&mut self, line: &mut String) -> io::Result<usize> {
        (self.0)();
        line.push_str("y\n");
        Ok(2)
    }
}

impl Place {
    fn env(&self) -> Env {
        environment(&self.sandbox, &self.home, &self.store)
    }

    /// `memriver <args>` in the work directory, with no terminal.
    fn run(&self, args: &[&str]) -> Done {
        self.run_from(&self.work, args)
    }

    fn run_from(&self, cwd: &Path, args: &[&str]) -> Done {
        run_in(&self.env(), &self.home, cwd, args, "", false)
    }

    /// The same at a terminal where `answer` is typed.
    fn ask(&self, args: &[&str], answer: &str) -> Done {
        run_in(&self.env(), &self.home, &self.work, args, answer, true)
    }

    /// The same from `cwd`, where `act` happens while the prompt is open and `y` is
    /// typed after it.
    fn ask_while(&self, cwd: &Path, args: &[&str], act: impl FnMut()) -> Done {
        let argv: Vec<String> = std::iter::once("memriver")
            .chain(args.iter().copied())
            .map(str::to_string)
            .collect();
        let (mut stdin, mut stdout, mut stderr) = (Typing(act), Vec::new(), Vec::new());
        let mut io = Io {
            stdin: &mut stdin,
            stdin_is_tty: true,
            stdout: &mut stdout,
            stderr: &mut stderr,
        };
        let code = run(argv, &self.env(), &self.home, cwd, &mut io);
        Done {
            code,
            stdout: String::from_utf8(stdout).unwrap(),
            stderr: String::from_utf8(stderr).unwrap(),
        }
    }

    fn services(&self) -> Services {
        common::services(&self.store, &self.home, &self.env())
    }

    /// A project bound to `directory`, made by the core.
    fn init_bound(&self, directory: &Path, name: &str) -> String {
        let projects = self.services().project;
        let plan = projects.plan_root(directory, None).unwrap();
        projects.init_project(name, &plan).unwrap().id
    }

    fn core_unbind(&self, project_id: &str, directory: &Path) {
        let projects = self.services().project;
        let root = s(&fs::canonicalize(directory).unwrap());
        let (plan, _) = projects.plan_unbind(project_id, &root, &self.work).unwrap();
        projects.unbind(&plan).unwrap();
    }

    fn core_adopt(&self, project_id: &str, directory: &Path) {
        let projects = self.services().project;
        let plan = projects.plan_root(directory, Some(project_id)).unwrap();
        projects.adopt(project_id, &plan).unwrap();
    }

    /// A project with no directory: bound to a scratch directory, then unbound.
    fn unbound_project(&self, name: &str) -> String {
        let scratch = self.home.join("scratch").join(new_id());
        fs::create_dir_all(&scratch).unwrap();
        let id = self.init_bound(&scratch, name);
        self.core_unbind(&id, &scratch);
        id
    }

    fn database(&self) -> PathBuf {
        self.store.join(DATABASE_FILENAME)
    }

    fn nothing_created(&self) -> bool {
        !self.database().exists() || self.services().project.list_projects().unwrap().is_empty()
    }

    fn resolved_id(&self, directory: &Path) -> Option<String> {
        let context = self
            .services()
            .project
            .open_project_context(directory)
            .unwrap();
        context.read_write_set.project_id
    }

    fn root_of(&self, project_id: &str) -> Option<String> {
        self.services()
            .project
            .read_project(project_id)
            .unwrap()
            .root
    }

    fn id_named(&self, name: &str) -> String {
        let projects = self.services().project.list_projects().unwrap();
        let mut named = projects.into_iter().filter(|project| project.name == name);
        let (Some(project), None) = (named.next(), named.next()) else {
            panic!("not exactly one project named {name}");
        };
        project.id
    }

    /// A connection for planting what no command writes.
    fn sql(&self) -> Connection {
        Connection::open(self.database()).unwrap()
    }

    fn insert(&self, id: &str, name: &str, root: &str) {
        self.sql()
            .execute(
                "INSERT INTO projects (id, name, root, is_global) VALUES (?1, ?2, ?3, 0)",
                (id, name, root),
            )
            .unwrap();
    }

    /// A bound directory that was moved away and replaced by a link to where it went.
    fn repoint_a_root(&self) -> PathBuf {
        let (elsewhere, moved) = (self.home.join("elsewhere"), self.home.join("moved"));
        fs::create_dir(&elsewhere).unwrap();
        self.init_bound(&elsewhere, "elsewhere");
        fs::rename(&elsewhere, &moved).unwrap();
        symlink(&moved, &elsewhere).unwrap();
        elsewhere
    }
}

/// The plan `init` and `adopt` show.
fn plan(command: &str, store: &Path, project: &str, root: &str, markers: &str) -> String {
    format!(
        "memriver project {command}\n  store:   {}\n  project: {project}\n  root:    {root}\n  \
         {SUBTREE}\n  Direct child directories containing a .git marker: {markers}\n",
        s(store)
    )
}

/// The lines a plan ends with when bound directories lie under its root.
fn independent(nested: &[(&str, &str)]) -> String {
    let lines: String = nested
        .iter()
        .map(|(id, root)| format!("    {id}: {root}\n"))
        .collect();
    format!("  These registered sub-projects stay independent:\n{lines}")
}

/// The plan `unbind` shows.
fn unbind_plan(store: &Path, project_id: &str, before: &str, afterwards: &str) -> String {
    format!(
        "memriver project unbind\n  store:   {}\n  project: {project_id}\n  directory before: \
         {before}\n  directory after:  (none)\n  the current directory resolves afterwards: \
         {afterwards}\n",
        s(store)
    )
}

fn check(done: &Done, code: i32, stdout: &str) {
    assert_eq!(
        (done.code, done.stdout.as_str(), done.stderr.as_str()),
        (code, stdout, "")
    );
}

/// Every entry below `directory`, with the bytes of each file.
fn tree(directory: &Path) -> Vec<(PathBuf, Option<Vec<u8>>)> {
    let mut found = Vec::new();
    let mut pending = vec![directory.to_path_buf()];
    while let Some(at) = pending.pop() {
        for entry in fs::read_dir(&at).unwrap() {
            let path = entry.unwrap().path();
            let kind = fs::symlink_metadata(&path).unwrap().file_type();
            let bytes = kind.is_file().then(|| fs::read(&path).unwrap());
            if kind.is_dir() {
                pending.push(path.clone());
            }
            found.push((path, bytes));
        }
    }
    found.sort();
    found
}

fn set_mode(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}

// --- init ---

#[test]
fn init_creates_a_named_project_and_binds_the_directory() {
    let place = place();
    let work = s(&place.work);
    let before = tree(&place.work);
    let done = place.ask(&["project", "init"], "y\n");
    let projects = place.services().project.list_projects().unwrap();
    let [registered] = projects.as_slice() else {
        panic!("not one project: {projects:?}");
    };
    assert!(is_id(&registered.id));
    assert_eq!(registered.root.as_deref(), Some(work.as_str()));
    assert_eq!(registered.name, "work");
    let expected = format!(
        "{}{PROMPT}created project work [{}] and bound {work}\n{RESTART}",
        plan("init", &place.store, &format!("work  {NEW}"), &work, "2"),
        registered.id
    );
    check(&done, 0, &expected);
    assert_eq!(tree(&place.work), before);
}

#[test]
fn init_takes_a_name_option_and_refuses_a_bad_name_before_the_prompt() {
    let place = place();
    let work = s(&place.work);
    let done = place.run(&["project", "init", "--name", "  My Work  ", "--yes"]);
    let id = place.id_named("My Work");
    let expected = format!(
        "{}created project My Work [{id}] and bound {work}\n{RESTART}",
        plan("init", &place.store, &format!("My Work  {NEW}"), &work, "2")
    );
    check(&done, 0, &expected);
    assert_eq!(place.services().project.list_projects().unwrap().len(), 1);
    // nothing is asked
    let frontend = s(&place.work.join("frontend"));
    let long = "x".repeat(121);
    let done = place.ask(&["project", "init", &frontend, "--name", &long], "y\n");
    check(
        &done,
        2,
        "refused: project name is longer than 120 characters\n",
    );
    assert_eq!(place.services().project.list_projects().unwrap().len(), 1);
}

#[test]
fn init_aborts_on_no_and_refuses_non_tty_without_yes() {
    let place = place();
    let before = tree(&place.work);
    let shown = plan(
        "init",
        &place.store,
        &format!("work  {NEW}"),
        &s(&place.work),
        "2",
    );
    let done = place.ask(&["project", "init"], "n\n");
    check(&done, 1, &format!("{shown}{PROMPT}{DECLINED}"));
    assert!(place.nothing_created());
    let done = place.run(&["project", "init"]);
    check(&done, 2, &format!("{shown}{NOT_A_TERMINAL}"));
    assert!(place.nothing_created());
    assert_eq!(tree(&place.work), before);
    let done = place.run(&["project", "init", "--yes"]);
    assert_eq!((done.code, done.stderr.as_str()), (0, ""));
    assert!(!place.nothing_created());
}

#[test]
fn init_prompt_eof_aborts_without_a_traceback() {
    let place = place();
    let done = place.ask(&["project", "init"], "");
    assert_eq!((done.code, done.stderr.as_str()), (1, ""));
    assert!(
        done.stdout.ends_with(&format!("{PROMPT}{DECLINED}")),
        "{}",
        done.stdout
    );
    assert!(place.nothing_created());
}

#[test]
fn init_refuses_root_home_and_home_ancestors() {
    for target in ["/", "{home}", "{home_parent}"] {
        let place = place();
        let target = target
            .replace("{home}", &s(&place.home))
            .replace("{home_parent}", &s(&place.tmp));
        let done = place.run(&["project", "init", &target, "--yes"]);
        check(&done, 2, COVERS_HOME);
        assert!(place.nothing_created());
    }
}

#[test]
fn init_refuses_case_alias_of_home_and_of_the_store_parent() {
    let place = place();
    let alias = place.tmp.join("home".to_uppercase());
    if !alias.exists() {
        return; // this file system tells the two spellings apart
    }
    let alias = s(&alias);
    let done = place.run(&["project", "init", &alias, "--yes"]);
    check(&done, 2, COVERS_HOME);
    // with a home elsewhere the alias still holds the store
    let unrelated = place.tmp.join("unrelated-home");
    let env = environment(&place.sandbox, &unrelated, &place.store);
    let args = ["project", "init", &alias, "--yes"];
    let done = run_in(&env, &unrelated, &place.work, &args, "", false);
    let expected = format!(
        "refused: the memory store {} lies inside this directory; the store would end up \
         inside the project\n",
        s(&place.store)
    );
    check(&done, 2, &expected);
    assert!(place.nothing_created());
}

#[test]
fn init_refuses_when_containment_cannot_be_verified() {
    let place = place();
    let locked = place.home.join("locked");
    let hidden = locked.join("bound");
    fs::create_dir_all(&hidden).unwrap();
    place.init_bound(&hidden, "demo");
    let unbound = place.unbound_project("unbound");
    let work = s(&place.work);
    set_mode(&locked, 0o000);
    let stopped = fs::metadata(&hidden).is_err();
    let init = place.run(&["project", "init", "--yes"]);
    let adopt = place.run(&["project", "adopt", &unbound, &work, "--yes"]);
    set_mode(&locked, 0o700);
    if !stopped {
        return; // nothing stops this user
    }
    // whether the work directory is the bound one could not be checked
    check(&init, 2, CANNOT_VERIFY);
    check(&adopt, 2, CANNOT_VERIFY);
    assert_eq!(place.services().project.list_projects().unwrap().len(), 2);
    assert_eq!(place.root_of(&unbound), None);
}

#[test]
fn init_refuses_store_inside_target() {
    let place = place();
    let store = s(&place.work.join("frontend/mem"));
    let target = s(&place.home.join("99_git"));
    let done = place.run(&["project", "init", &target, "--root", &store, "--yes"]);
    let expected = format!(
        "refused: the memory store {store} lies inside this directory; the store would end up \
         inside the project\n"
    );
    check(&done, 2, &expected);
    assert!(!place.work.join("frontend/mem").exists());
}

#[test]
fn init_refuses_a_directory_inside_the_memory_store() {
    let place = place();
    let inside = place.store.join("somewhere");
    fs::create_dir_all(&inside).unwrap();
    let before = tree(&place.store);
    let done = place.run(&["project", "init", &s(&inside), "--yes"]);
    let expected = format!(
        "refused: {} lies inside the memory store {}\n",
        s(&inside),
        s(&place.store)
    );
    check(&done, 2, &expected);
    assert_eq!(tree(&place.store), before);
}

#[test]
fn init_refuses_missing_directory_and_already_bound_dir() {
    let place = place();
    let (work, missing) = (s(&place.work), s(&place.work.join("nope")));
    let done = place.run(&["project", "init", &missing, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {missing} is not an existing directory\n"),
    );
    let id = place.init_bound(&place.work, "work");
    // a file is no directory either
    let file = s(&place.home.join("file"));
    fs::write(&file, "x").unwrap();
    let done = place.run(&["project", "init", &file, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {file} is not an existing directory\n"),
    );
    let before = tree(&place.work);
    let done = place.run(&["project", "init", &work, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {work} is already bound to project {id}\n"),
    );
    assert_eq!(place.services().project.list_projects().unwrap().len(), 1);
    assert_eq!(tree(&place.work), before);
}

#[test]
fn init_refuses_an_unaddressable_path_without_a_traceback() {
    let place = place();
    let done = place.run(&["project", "init", "/x\0y", "--yes"]);
    check(&done, 2, "refused: /x y is not an existing directory\n");
    assert!(place.nothing_created());
}

#[test]
fn init_inside_a_registered_parent_is_allowed() {
    let place = place();
    let parent = place.init_bound(&place.work, "work");
    let frontend = place.work.join("frontend");
    let done = place.run(&["project", "init", &s(&frontend), "--yes"]);
    assert_eq!((done.code, done.stderr.as_str()), (0, ""));
    assert_eq!(place.services().project.list_projects().unwrap().len(), 2);
    let child = place.resolved_id(&frontend);
    assert!(child.is_some() && child != Some(parent.clone()));
    assert_eq!(place.resolved_id(&place.work.join("backend")), Some(parent));
}

#[test]
fn parent_init_lists_existing_child_roots_as_independent() {
    let place = place();
    let (work, frontend) = (s(&place.work), place.work.join("frontend"));
    let child = place.init_bound(&frontend, "frontend");
    let done = place.run(&["project", "init", "--yes"]);
    let id = place.id_named("work");
    let expected = format!(
        "{}{}created project work [{id}] and bound {work}\n{RESTART}",
        plan("init", &place.store, &format!("work  {NEW}"), &work, "2"),
        independent(&[(&child, &s(&frontend))])
    );
    check(&done, 0, &expected);
    assert_eq!(place.resolved_id(&frontend), Some(child));
}

#[test]
fn parent_plan_lists_a_case_alias_child_root_as_independent() {
    let place = place();
    let alias = place.work.join("FRONTEND");
    if !alias.exists() {
        return; // this file system tells the two spellings apart
    }
    let child = place.init_bound(&alias, "frontend");
    let done = place.run(&["project", "init"]);
    let expected = format!(
        "{}{}{NOT_A_TERMINAL}",
        plan(
            "init",
            &place.store,
            &format!("work  {NEW}"),
            &s(&place.work),
            "2"
        ),
        independent(&[(&child, &s(&alias))])
    );
    check(&done, 2, &expected);
}

#[test]
fn init_independent_list_never_forges_a_fake_project_line() {
    let place = place();
    let work = s(&place.work);
    let (row, fake) = (new_id(), new_id());
    place.services().project.ensure_global().unwrap();
    place.insert(
        &row,
        "forger",
        &format!("{work}/child\n    {fake}: /forged"),
    );
    let done = place.run(&["project", "init", "--yes"]);
    let id = place.id_named("work");
    let expected = format!(
        "{}{}created project work [{id}] and bound {work}\n{RESTART}",
        plan("init", &place.store, &format!("work  {NEW}"), &work, "2"),
        independent(&[(&row, &format!("{work}/child     {fake}: /forged"))])
    );
    check(&done, 0, &expected);
    let forged = format!("    {fake}: /forged");
    assert!(!done.stdout.lines().any(|line| line.starts_with(&forged)));
}

#[test]
fn init_of_a_directory_named_with_a_newline_never_forges_a_line() {
    let place = place();
    let target = place.work.join("bad\nFORGED-LINE");
    fs::create_dir(&target).unwrap();
    let done = place.run(&["project", "init", &s(&target), "--yes"]);
    let id = place.id_named("bad FORGED-LINE");
    let shown = format!("{}/bad FORGED-LINE", s(&place.work));
    let expected = format!(
        "{}created project bad FORGED-LINE [{id}] and bound {shown}\n{RESTART}",
        plan(
            "init",
            &place.store,
            &format!("bad FORGED-LINE  {NEW}"),
            &shown,
            "0"
        )
    );
    check(&done, 0, &expected);
    assert!(
        !done
            .stdout
            .lines()
            .any(|line| line.starts_with("FORGED-LINE"))
    );
    assert_eq!(place.root_of(&id), Some(s(&target)));
}

#[test]
fn target_deleted_while_the_prompt_is_open_is_refused() {
    let place = place();
    let target = place.work.join("ephemeral");
    fs::create_dir(&target).unwrap();
    let done = place.ask_while(&place.work, &["project", "init", &s(&target)], || {
        fs::remove_dir(&target).unwrap()
    });
    let expected = format!(
        "{}{PROMPT}{OUT_OF_DATE}",
        plan(
            "init",
            &place.store,
            &format!("ephemeral  {NEW}"),
            &s(&target),
            "0"
        )
    );
    check(&done, 2, &expected);
    assert!(place.nothing_created());
}

#[test]
fn store_repointed_into_target_while_the_prompt_is_open_is_refused() {
    let place = place();
    let before = tree(&place.work);
    let done = place.ask_while(&place.work, &["project", "init"], || {
        symlink(&place.work, &place.store).unwrap()
    });
    // the confirmed plan's store now lies inside the target: refused before anything is
    // opened, so nothing lands in the target
    let expected = format!(
        "{}{PROMPT}refused: the memory store {} lies inside this directory; the store would \
         end up inside the project\n",
        plan(
            "init",
            &place.store,
            &format!("work  {NEW}"),
            &s(&place.work),
            "2"
        ),
        s(&place.store)
    );
    check(&done, 2, &expected);
    assert_eq!(tree(&place.work), before);
}

#[test]
fn init_is_one_transaction_and_a_changed_target_creates_nothing() {
    let place = place();
    let parent = place.home.join("99_git");
    let done = place.ask_while(&parent, &["project", "init", &s(&place.work)], || {
        fs::rename(&place.work, parent.join("moved")).unwrap()
    });
    let expected = format!(
        "{}{PROMPT}{OUT_OF_DATE}",
        plan(
            "init",
            &place.store,
            &format!("work  {NEW}"),
            &s(&place.work),
            "2"
        )
    );
    check(&done, 2, &expected);
    assert_eq!(place.services().project.list_projects().unwrap(), []);
}

#[test]
fn a_store_link_redirected_during_the_prompt_is_refused() {
    for command in ["init", "adopt", "unbind"] {
        let place = place();
        let (real, other) = (place.home.join("store-a"), place.home.join("store-b"));
        fs::create_dir(&real).unwrap();
        fs::create_dir(&other).unwrap();
        let link = place.home.join("store-link");
        symlink(&real, &link).unwrap();
        let projects = common::services(&link, &place.home, &place.env()).project;
        projects.ensure_global().unwrap();
        let work = s(&place.work);
        let mut id = String::new();
        if command != "init" {
            let planned = projects.plan_root(&place.work, None).unwrap();
            id = projects.init_project("work", &planned).unwrap().id;
        }
        if command == "adopt" {
            let (planned, _) = projects.plan_unbind(&id, &work, &place.work).unwrap();
            projects.unbind(&planned).unwrap();
        }
        let link_text = s(&link);
        let (args, shown): (Vec<&str>, String) = match command {
            "init" => (
                vec!["project", "init", &work, "--root", &link_text],
                plan("init", &real, &format!("work  {NEW}"), &work, "2"),
            ),
            "adopt" => (
                vec!["project", "adopt", &id, &work, "--root", &link_text],
                plan(
                    "adopt",
                    &real,
                    &format!("work [{id}]  (existing)"),
                    &work,
                    "2",
                ),
            ),
            _ => (
                vec!["project", "unbind", &id, &work, "--root", &link_text],
                unbind_plan(&real, &id, &work, "none"),
            ),
        };
        let done = place.ask_while(&place.work, &args, || {
            fs::remove_file(&link).unwrap();
            symlink(&other, &link).unwrap();
        });
        check(&done, 2, &format!("{shown}{PROMPT}{OUT_OF_DATE}"));
        assert!(!other.join(DATABASE_FILENAME).exists(), "{command}");
        assert_eq!(fs::read_dir(&other).unwrap().count(), 0, "{command}");
    }
}

#[test]
fn relative_directories_resolve_against_the_injected_cwd() {
    let place = place();
    let frontend = s(&place.work.join("frontend"));
    let done = place.run(&["project", "init", "frontend", "--yes"]);
    let id = place.id_named("frontend");
    let expected = format!(
        "{}created project frontend [{id}] and bound {frontend}\n{RESTART}",
        plan(
            "init",
            &place.store,
            &format!("frontend  {NEW}"),
            &frontend,
            "0"
        )
    );
    check(&done, 0, &expected);
    // `.` and a doubled slash are dropped, `..` is followed through the file system
    let done = place.run(&["project", "init", "frontend/..//backend/."]);
    let backend = s(&place.work.join("backend"));
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        plan(
            "init",
            &place.store,
            &format!("backend  {NEW}"),
            &backend,
            "0"
        )
    );
    check(&done, 2, &expected);
    let done = place.run(&["project", "init", "frontend/../nope", "--yes"]);
    let expected = format!(
        "refused: {}/frontend/../nope is not an existing directory\n",
        s(&place.work)
    );
    check(&done, 2, &expected);
}

// --- adopt ---

#[test]
fn adopt_binds_an_existing_project_and_is_idempotent() {
    let place = place();
    let work = s(&place.work);
    let id = place.init_bound(&place.work, "work");
    let done = place.run(&["project", "adopt", &id, &work, "--yes"]);
    check(
        &done,
        0,
        &format!("{work} is already bound to {id}; nothing to do\n"),
    );
    let done = place.run(&["project", "unbind", &id, &work, "--yes"]);
    let expected = format!(
        "{}unbound {work} from {id}\n{RESTART}",
        unbind_plan(&place.store, &id, &work, "none")
    );
    check(&done, 0, &expected);
    assert_eq!(place.root_of(&id), None);
    let done = place.run(&["project", "adopt", &id, &work, "--yes"]);
    let expected = format!(
        "{}bound {work} to {id}\n{RESTART}",
        plan(
            "adopt",
            &place.store,
            &format!("work [{id}]  (existing)"),
            &work,
            "2"
        )
    );
    check(&done, 0, &expected);
    assert_eq!(place.root_of(&id), Some(work.clone()));
    let other = place.home.join("99_git/other");
    fs::create_dir(&other).unwrap();
    let done = place.run(&["project", "adopt", &id, &s(&other), "--yes"]);
    let expected =
        format!("refused: project {id} already has a directory ({work}); unbind it first\n");
    check(&done, 2, &expected);
    let absent = new_id();
    let done = place.run(&["project", "adopt", &absent, &work, "--yes"]);
    check(&done, 2, &format!("no such project: {absent}\n"));
    let done = place.run(&["project", "adopt", &"x".repeat(300), &work, "--yes"]);
    check(&done, 2, &format!("no such project: {}\n", "x".repeat(255)));
    assert_eq!(place.root_of(&id), Some(work));
}

#[test]
fn adopt_refuses_the_global_project() {
    let place = place();
    let global = place.services().project.ensure_global().unwrap();
    let work = s(&place.work);
    let done = place.run(&["project", "adopt", &global, &work, "--yes"]);
    check(&done, 2, GLOBAL_REFUSAL);
    assert_eq!(place.root_of(&global), None);
    // and it holds no directory to take away
    let done = place.run(&["project", "unbind", &global, &work, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {work} is not bound to {global}\n"),
    );
}

#[test]
fn an_invalid_project_id_argument_cannot_forge_an_output_line() {
    let place = place();
    let work = s(&place.work);
    for command in ["adopt", "unbind"] {
        let done = place.run(&["project", command, "bad\nFORGED-LINE", &work, "--yes"]);
        check(&done, 2, "no such project: bad FORGED-LINE\n");
        assert!(
            !done
                .stdout
                .lines()
                .any(|line| line.starts_with("FORGED-LINE"))
        );
    }
}

#[test]
fn adopt_applies_the_same_target_checks() {
    let place = place();
    let id = place.unbound_project("work");
    let done = place.run(&["project", "adopt", &id, &s(&place.home), "--yes"]);
    check(&done, 2, COVERS_HOME);
    let missing = s(&place.work.join("nope"));
    let done = place.run(&["project", "adopt", &id, &missing, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {missing} is not an existing directory\n"),
    );
    let inside = s(&place.store.join("somewhere"));
    fs::create_dir(&inside).unwrap();
    let done = place.run(&["project", "adopt", &id, &inside, "--yes"]);
    let expected = format!(
        "refused: {inside} lies inside the memory store {}\n",
        s(&place.store)
    );
    check(&done, 2, &expected);
    let owner = place.init_bound(&place.work, "owner");
    let work = s(&place.work);
    let done = place.run(&["project", "adopt", &id, &work, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {work} is already bound to project {owner}\n"),
    );
    assert_eq!(place.root_of(&id), None);
}

#[test]
fn adopt_of_a_project_removed_during_confirmation_exits_2() {
    let place = place();
    let id = place.unbound_project("work");
    let work = s(&place.work);
    let done = place.ask_while(&place.work, &["project", "adopt", &id, &work], || {
        place
            .sql()
            .execute("DELETE FROM projects WHERE id = ?1", [&id])
            .unwrap();
    });
    let expected = format!(
        "{}{PROMPT}no such project: {id}\n",
        plan(
            "adopt",
            &place.store,
            &format!("work [{id}]  (existing)"),
            &work,
            "2"
        )
    );
    check(&done, 2, &expected);
    assert_eq!(place.resolved_id(&place.work), None);
}

// --- unbind ---

#[test]
fn unbind_literal_first_and_shows_next_identity() {
    let place = place();
    let old = place.home.join("99_git/old-work");
    fs::create_dir(&old).unwrap();
    let id = place.init_bound(&old, "work");
    fs::remove_dir_all(&old).unwrap();
    let old = s(&old);
    let done = place.run(&["project", "unbind", &id, &old, "--yes"]);
    let expected = format!(
        "{}unbound {old} from {id}\n{RESTART}",
        unbind_plan(&place.store, &id, &old, "none")
    );
    check(&done, 0, &expected);
    assert_eq!(place.root_of(&id), None);
    let done = place.run(&["project", "unbind", &id, &old, "--yes"]);
    check(&done, 2, &format!("refused: {old} is not bound to {id}\n"));
}

#[test]
fn unbind_of_a_root_that_became_a_symlink_removes_the_stored_spelling() {
    let place = place();
    let (old, new) = (place.home.join("99_git/old"), place.home.join("99_git/new"));
    fs::create_dir(&old).unwrap();
    let id = place.init_bound(&old, "work");
    fs::remove_dir(&old).unwrap();
    fs::create_dir(&new).unwrap();
    symlink(&new, &old).unwrap();
    let old = s(&old);
    let done = place.run(&["project", "unbind", &id, &old, "--yes"]);
    let expected = format!(
        "{}unbound {old} from {id}\n{RESTART}",
        unbind_plan(&place.store, &id, &old, "none")
    );
    check(&done, 0, &expected);
    assert_eq!(place.root_of(&id), None);
}

#[test]
fn unbind_shows_parent_as_next_identity() {
    let place = place();
    let frontend = place.work.join("frontend");
    let parent = place.init_bound(&place.work, "work");
    let child = place.init_bound(&frontend, "frontend");
    let shown = s(&frontend);
    let done = place.run_from(&frontend, &["project", "unbind", &child, &shown, "--yes"]);
    let expected = format!(
        "{}unbound {shown} from {child}\n{RESTART}",
        unbind_plan(
            &place.store,
            &child,
            &shown,
            &format!("registered {parent}")
        )
    );
    check(&done, 0, &expected);
}

#[test]
fn unbind_resolves_a_relative_directory_against_the_injected_cwd() {
    let place = place();
    let frontend = place.work.join("frontend");
    symlink(&frontend, place.work.join("link")).unwrap();
    let id = place.init_bound(&frontend, "work");
    let done = place.run(&["project", "unbind", &id, "link", "--yes"]);
    let shown = s(&frontend);
    let expected = format!(
        "{}unbound {shown} from {id}\n{RESTART}",
        unbind_plan(&place.store, &id, &shown, "none")
    );
    check(&done, 0, &expected);
    assert_eq!(place.root_of(&id), None);
}

#[test]
fn unbind_executes_exactly_the_pair_it_showed() {
    let place = place();
    let id = place.init_bound(&place.work, "work");
    let (work, other) = (s(&place.work), place.home.join("99_git/other"));
    let done = place.ask_while(&place.work, &["project", "unbind", &id, &work], || {
        place.core_unbind(&id, &place.work);
        fs::create_dir(&other).unwrap();
        place.core_adopt(&id, &other);
    });
    let expected = format!(
        "{}{PROMPT}{BINDING_CHANGED}",
        unbind_plan(&place.store, &id, &work, "none")
    );
    check(&done, 2, &expected);
    assert_eq!(place.root_of(&id), Some(s(&other)));
}

// --- explain ---

/// What `explain` prints: the three lines every state has, the lines of this state, and
/// what may be read and written.
fn explained(
    store: &Path,
    cwd: &str,
    state: &str,
    middle: &str,
    reads: &str,
    writes: &str,
) -> String {
    format!(
        "store: {}\ncwd: {cwd}\nstate: {state}\n{middle}reads: {reads}\nwrites: {writes}\n",
        s(store)
    )
}

const UNREADABLE: &str = "diagnostic: the memory store could not be read\n";

#[test]
fn explain_states_and_exit_codes() {
    let place = place();
    let (work, frontend) = (s(&place.work), s(&place.work.join("frontend")));
    let done = place.run(&["project", "explain"]);
    // no store yet: global does not exist, so it is not claimed readable
    check(
        &done,
        0,
        &explained(&place.store, &work, "none", "", "none", "none"),
    );
    assert!(!place.store.exists());
    let id = place.init_bound(&place.work, "work");
    let registered = format!("project: {id}\nname: work\nroot: {work}\n");
    let done = place.run(&["project", "explain", "--project-dir", &frontend]);
    let expected = explained(&place.store, &frontend, "registered", &registered, &id, &id);
    check(&done, 0, &expected);
    place.services().project.ensure_global().unwrap();
    let done = place.run(&["project", "explain", "--project-dir", &frontend]);
    let reads = format!("{id}, global");
    let expected = explained(
        &place.store,
        &frontend,
        "registered",
        &registered,
        &reads,
        &id,
    );
    check(&done, 0, &expected);
    let done = place.run_from(&place.home, &["project", "explain"]);
    let home = s(&place.home);
    check(
        &done,
        0,
        &explained(&place.store, &home, "none", "", "global", "none"),
    );
    // one re-pointed root degrades every directory, never a silent fall-through
    let elsewhere = place.repoint_a_root();
    let done = place.run(&["project", "explain"]);
    let diagnostic = format!(
        "diagnostic: {}: registered root is no longer a canonical path\n",
        s(&elsewhere)
    );
    let expected = explained(
        &place.store,
        &work,
        "degraded",
        &diagnostic,
        "global",
        "none",
    );
    check(&done, 1, &expected);
    fs::write(place.database(), b"not a database").unwrap();
    let done = place.run(&["project", "explain"]);
    let expected = explained(
        &place.store,
        &work,
        "unavailable",
        UNREADABLE,
        "none",
        "none",
    );
    check(&done, 1, &expected);
}

#[test]
fn explain_of_a_store_of_a_later_schema_is_unavailable() {
    let place = place();
    place.init_bound(&place.work, "work");
    place.services().project.ensure_global().unwrap();
    place.sql().pragma_update(None, "user_version", 7).unwrap();
    let before = tree(&place.store);
    let done = place.run(&["project", "explain"]);
    let work = s(&place.work);
    let expected = explained(
        &place.store,
        &work,
        "unavailable",
        UNREADABLE,
        "none",
        "none",
    );
    check(&done, 1, &expected);
    assert_eq!(tree(&place.store), before);
}

#[test]
fn explain_of_a_directory_that_is_not_there_is_degraded() {
    let place = place();
    let done = place.run_from(
        &place.home,
        &["project", "explain", "--project-dir", "nope/x"],
    );
    let cwd = s(&place.home.join("nope/x"));
    let diagnostic = "diagnostic: working directory could not be resolved\n";
    check(
        &done,
        1,
        &explained(&place.store, &cwd, "degraded", diagnostic, "none", "none"),
    );
}

#[test]
fn explain_keeps_consecutive_spaces_in_a_registered_root() {
    let place = place();
    let target = place.work.join("two  spaces");
    fs::create_dir(&target).unwrap();
    let id = place.init_bound(&target, "work");
    let shown = s(&target);
    let done = place.run(&["project", "explain", "--project-dir", &shown]);
    let registered = format!("project: {id}\nname: work\nroot: {shown}\n");
    check(
        &done,
        0,
        &explained(&place.store, &shown, "registered", &registered, &id, &id),
    );
}

#[test]
fn explain_diagnostic_never_forges_a_fake_root_line() {
    // a root whose nearest existing ancestor is a symlink is "re-pointed", and its stored
    // spelling lands in the diagnostic
    let place = place();
    let (real, link) = (place.home.join("real"), place.home.join("link"));
    fs::create_dir(&real).unwrap();
    symlink(&real, &link).unwrap();
    place.services().project.ensure_global().unwrap();
    place.insert(
        &new_id(),
        "forger",
        &format!("{}/x\n  root: /forged", s(&link)),
    );
    let done = place.run_from(&place.home, &["project", "explain"]);
    let diagnostic = format!(
        "diagnostic: {}/x   root: /forged: registered root is no longer a canonical path\n",
        s(&link)
    );
    let home = s(&place.home);
    check(
        &done,
        1,
        &explained(
            &place.store,
            &home,
            "degraded",
            &diagnostic,
            "global",
            "none",
        ),
    );
    assert!(
        !done
            .stdout
            .lines()
            .any(|line| line.starts_with("  root: /forged"))
    );
}

#[test]
fn explain_never_creates_or_writes_the_store() {
    let place = place();
    let done = place.run(&["project", "explain"]);
    assert_eq!(done.code, 0);
    assert!(!place.store.exists());
    place.init_bound(&place.work, "work");
    place.services().project.ensure_global().unwrap();
    let before = tree(&place.store);
    let done = place.run(&["project", "explain"]);
    assert_eq!(done.code, 0);
    assert_eq!(tree(&place.store), before);
}

// --- every command together ---

#[test]
fn no_command_or_refusal_touches_the_target() {
    let scenarios = [
        ("init-ok", 0),
        ("init-no", 1),
        ("init-non-tty", 2),
        ("init-refused-home", 2),
        ("init-already-bound", 2),
        ("adopt-ok", 0),
        ("adopt-missing-id", 2),
        ("adopt-refused-home", 2),
        ("unbind-ok", 0),
        ("unbind-not-bound", 2),
        ("explain", 0),
    ];
    for (scenario, code) in scenarios {
        let place = place();
        let (work, home) = (s(&place.work), s(&place.home));
        let (frontend, backend) = (format!("{work}/frontend"), format!("{work}/backend"));
        let id = match scenario {
            scenario if scenario.starts_with("adopt") => place.unbound_project("work"),
            "unbind-ok" | "unbind-not-bound" | "init-already-bound" => {
                place.init_bound(&place.work, "work")
            }
            _ => String::new(),
        };
        let absent = new_id();
        let before = tree(&place.work);
        let done = match scenario {
            "init-ok" => place.run(&["project", "init", "--yes"]),
            "init-no" => place.ask(&["project", "init"], "n\n"),
            "init-non-tty" => place.run(&["project", "init"]),
            "init-refused-home" => place.run(&["project", "init", &home, "--yes"]),
            "init-already-bound" => place.run(&["project", "init", &work, "--yes"]),
            "adopt-ok" => place.run(&["project", "adopt", &id, &frontend, "--yes"]),
            "adopt-missing-id" => place.run(&["project", "adopt", &absent, &work, "--yes"]),
            "adopt-refused-home" => place.run(&["project", "adopt", &id, &home, "--yes"]),
            "unbind-ok" => place.run(&["project", "unbind", &id, &work, "--yes"]),
            "unbind-not-bound" => place.run(&["project", "unbind", &id, &backend, "--yes"]),
            _ => place.run(&["project", "explain"]),
        };
        assert_eq!((done.code, done.stderr.as_str()), (code, ""), "{scenario}");
        assert_eq!(tree(&place.work), before, "{scenario}");
    }
}

#[test]
fn read_project_is_how_existence_is_decided() {
    let place = place();
    let absent = new_id();
    assert_eq!(
        place.services().project.read_project(&absent),
        Err(CoreError::ProjectNotFound(absent.clone()))
    );
    for command in ["adopt", "unbind"] {
        let done = place.run(&["project", command, &absent, &s(&place.work), "--yes"]);
        check(&done, 2, &format!("no such project: {absent}\n"));
    }
    assert!(place.nothing_created());
}

// --- the sentences no test above reaches ---

#[test]
fn a_root_given_relative_is_read_from_the_working_directory() {
    let place = place();
    let (store, work) = (place.home.join("rel-store"), s(&place.work));
    let root = ["--root", "rel-store"];
    let args = [&["project", "init", &work, "--yes"], &root[..]].concat();
    let done = place.run_from(&place.home, &args);
    assert!(store.join(DATABASE_FILENAME).exists());
    let id = common::services(&store, &place.home, &place.env())
        .project
        .list_projects()
        .unwrap()[0]
        .id
        .clone();
    let expected = format!(
        "{}created project work [{id}] and bound {work}\n{RESTART}",
        plan("init", &store, &format!("work  {NEW}"), &work, "2")
    );
    check(&done, 0, &expected);
    // a directory given relative, and one reached through a link, are shown resolved
    symlink(place.work.join("frontend"), place.home.join("lnk")).unwrap();
    let registered = format!("project: {id}\nname: work\nroot: {work}\n");
    let frontend = format!("{work}/frontend");
    let expected = explained(&store, &frontend, "registered", &registered, &id, &id);
    for directory in ["99_git/work/frontend", "lnk"] {
        let args = [
            &["project", "explain", "--project-dir", directory],
            &root[..],
        ]
        .concat();
        check(&place.run_from(&place.home, &args), 0, &expected);
    }
    let args = [
        &["project", "adopt", &id, "99_git/work", "--yes"],
        &root[..],
    ]
    .concat();
    let done = place.run_from(&place.home, &args);
    check(
        &done,
        0,
        &format!("{work} is already bound to {id}; nothing to do\n"),
    );
    let args = [
        &["project", "unbind", &id, "99_git/work", "--yes"],
        &root[..],
    ]
    .concat();
    let done = place.run_from(&place.home, &args);
    let expected = format!(
        "{}unbound {work} from {id}\n{RESTART}",
        unbind_plan(&store, &id, &work, "none")
    );
    check(&done, 0, &expected);
    // the store the environment names was never looked for
    assert!(!place.store.exists());
    assert!(!place.work.join("rel-store").exists());
}

#[test]
fn a_name_is_one_line_of_at_most_120_characters() {
    let place = place();
    let work = s(&place.work);
    let shown = |name: &str| {
        format!(
            "{}{NOT_A_TERMINAL}",
            plan("init", &place.store, &format!("{name}  {NEW}"), &work, "2")
        )
    };
    // characters are counted, not bytes
    let wide = "\u{1F600}".repeat(120);
    let done = place.run(&["project", "init", "--name", &wide]);
    check(&done, 2, &shown(&wide));
    let done = place.run(&["project", "init", "--name", &"\u{1F600}".repeat(121)]);
    check(
        &done,
        2,
        "refused: project name is longer than 120 characters\n",
    );
    // what surrounds the name is not counted
    let padded = format!("  {}\n", "x".repeat(120));
    let done = place.run(&["project", "init", "--name", &padded]);
    check(&done, 2, &shown(&"x".repeat(120)));
    let done = place.run(&[
        "project",
        "init",
        "--name",
        &format!("  {}\n", "x".repeat(121)),
    ]);
    check(
        &done,
        2,
        "refused: project name is longer than 120 characters\n",
    );
    let done = place.run(&["project", "init", "--name", "one\r\ntwo\u{2028}three  four"]);
    check(&done, 2, &shown("one two three four"));
    for empty in ["", "  \n "] {
        let done = place.run(&["project", "init", "--name", empty, "--yes"]);
        check(&done, 2, "refused: project name is empty\n");
    }
    assert!(place.nothing_created());
    // the name a store holds is the single line
    let done = place.run(&["project", "init", "--name", "a\nb\tc", "--yes"]);
    let id = place.id_named("a b c");
    let expected = format!(
        "{}created project a b c [{id}] and bound {work}\n{RESTART}",
        plan("init", &place.store, &format!("a b c  {NEW}"), &work, "2")
    );
    check(&done, 0, &expected);
}

#[test]
fn the_plan_counts_direct_children_that_hold_a_git_marker_of_any_kind() {
    let place = place();
    let work = &place.work;
    fs::create_dir(work.join("plain")).unwrap();
    fs::create_dir(work.join("filemark")).unwrap();
    fs::write(work.join("filemark/.git"), "gitdir: x").unwrap();
    fs::create_dir_all(work.join("deep/inner/.git")).unwrap();
    fs::create_dir(work.join(".git")).unwrap();
    let outside = place.home.join("outside-repo");
    fs::create_dir_all(outside.join(".git")).unwrap();
    symlink(&outside, work.join("linked")).unwrap();
    fs::create_dir(work.join("dangling")).unwrap();
    symlink(work.join("nowhere"), work.join("dangling/.git")).unwrap();
    let done = place.run(&["project", "init"]);
    // the two repositories, the file and the link that leads nowhere
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        plan("init", &place.store, &format!("work  {NEW}"), &s(work), "4")
    );
    check(&done, 2, &expected);
}

#[test]
fn children_that_cannot_be_listed_are_not_counted() {
    let place = place();
    let work = s(&place.work);
    let id = place.unbound_project("work");
    set_mode(&place.work, 0o100);
    let stopped = fs::read_dir(&place.work).is_err();
    let init = place.run_from(&place.home, &["project", "init", &work]);
    let adopt = place.run_from(&place.home, &["project", "adopt", &id, &work]);
    set_mode(&place.work, 0o700);
    if !stopped {
        return; // nothing stops this user
    }
    let count = "could not be counted";
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        plan("init", &place.store, &format!("work  {NEW}"), &work, count)
    );
    check(&init, 2, &expected);
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        plan(
            "adopt",
            &place.store,
            &format!("work [{id}]  (existing)"),
            &work,
            count
        )
    );
    check(&adopt, 2, &expected);
}

#[test]
fn every_answer_but_y_and_yes_declines() {
    let place = place();
    let frontend = s(&place.work.join("frontend"));
    let shown = plan(
        "init",
        &place.store,
        &format!("frontend  {NEW}"),
        &frontend,
        "0",
    );
    for answer in ["yep\n", "\n", "n\n", "y y\n"] {
        let done = place.ask(&["project", "init", &frontend], answer);
        check(&done, 1, &format!("{shown}{PROMPT}{DECLINED}"));
        assert!(place.nothing_created(), "{answer:?}");
    }
    for answer in ["YES\n", " y \n", "Yes\n", "y"] {
        let done = place.ask(&["project", "init", &frontend], answer);
        let id = place.id_named("frontend");
        let expected = format!(
            "{shown}{PROMPT}created project frontend [{id}] and bound {frontend}\n{RESTART}"
        );
        check(&done, 0, &expected);
        place.core_unbind(&id, &place.work.join("frontend"));
        place.sql().execute("DELETE FROM projects", []).unwrap();
    }
}

#[test]
fn adopt_and_unbind_ask_before_they_write() {
    let place = place();
    let (work, backend) = (s(&place.work), place.work.join("backend"));
    let child = place.init_bound(&backend, "backend");
    let id = place.unbound_project("work");
    let shown = format!(
        "{}{}",
        plan(
            "adopt",
            &place.store,
            &format!("work [{id}]  (existing)"),
            &work,
            "2"
        ),
        independent(&[(&child, &s(&backend))])
    );
    let done = place.run(&["project", "adopt", &id, &work]);
    check(&done, 2, &format!("{shown}{NOT_A_TERMINAL}"));
    let done = place.ask(&["project", "adopt", &id, &work], "n\n");
    check(&done, 1, &format!("{shown}{PROMPT}{DECLINED}"));
    assert_eq!(place.root_of(&id), None);
    let done = place.ask(&["project", "adopt", &id, &work], "yes\n");
    check(
        &done,
        0,
        &format!("{shown}{PROMPT}bound {work} to {id}\n{RESTART}"),
    );
    assert_eq!(place.root_of(&id), Some(work.clone()));

    let shown = unbind_plan(&place.store, &id, &work, "none");
    let done = place.run(&["project", "unbind", &id, &work]);
    check(&done, 2, &format!("{shown}{NOT_A_TERMINAL}"));
    let done = place.ask(&["project", "unbind", &id, &work], "n\n");
    check(&done, 1, &format!("{shown}{PROMPT}{DECLINED}"));
    assert_eq!(place.root_of(&id), Some(work.clone()));
    let done = place.ask(&["project", "unbind", &id, &work], "y\n");
    check(
        &done,
        0,
        &format!("{shown}{PROMPT}unbound {work} from {id}\n{RESTART}"),
    );
    assert_eq!(place.root_of(&id), None);
}

#[test]
fn what_changed_while_adopt_or_init_waited_is_refused_by_its_name() {
    let place = place();
    let (work, other) = (s(&place.work), place.home.join("99_git/other"));
    fs::create_dir(&other).unwrap();
    let id = place.unbound_project("work");
    let shown = plan(
        "adopt",
        &place.store,
        &format!("work [{id}]  (existing)"),
        &work,
        "2",
    );
    // the project took another directory
    let done = place.ask_while(&place.work, &["project", "adopt", &id, &work], || {
        place.core_adopt(&id, &other)
    });
    let expected =
        format!("{shown}{PROMPT}refused: project {id} already has a directory; unbind it first\n");
    check(&done, 2, &expected);
    assert_eq!(place.root_of(&id), Some(s(&other)));
    place.core_unbind(&id, &other);
    // another project took the directory
    let done = place.ask_while(&place.work, &["project", "adopt", &id, &work], || {
        place.init_bound(&place.work, "taker");
    });
    let taker = place.id_named("taker");
    let expected = format!("{shown}{PROMPT}refused: {work} is already bound to project {taker}\n");
    check(&done, 2, &expected);
    assert_eq!(place.root_of(&id), None);
    place.core_unbind(&taker, &place.work);
    let done = place.ask_while(&place.work, &["project", "init", &work], || {
        place.init_bound(&place.work, "second");
    });
    let second = place.id_named("second");
    let expected = format!(
        "{}{PROMPT}refused: {work} is already bound to project {second}\n",
        plan("init", &place.store, &format!("work  {NEW}"), &work, "2")
    );
    check(&done, 2, &expected);
    assert_eq!(place.services().project.list_projects().unwrap().len(), 3);
}

#[test]
fn unbind_says_when_the_directory_resolves_to_a_damaged_store_afterwards() {
    let place = place();
    let work = s(&place.work);
    let id = place.init_bound(&place.work, "work");
    place.repoint_a_root();
    let done = place.run(&["project", "unbind", &id, &work]);
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        unbind_plan(&place.store, &id, &work, "degraded")
    );
    check(&done, 2, &expected);
}

#[test]
fn a_store_that_cannot_be_used_refuses_every_write_with_one_sentence() {
    for damage in ["garbage", "later schema", "invalid row"] {
        let place = place();
        let (work, backend) = (s(&place.work), s(&place.work.join("backend")));
        let id = place.init_bound(&place.work, "work");
        match damage {
            "garbage" => fs::write(place.database(), b"not a database").unwrap(),
            "later schema" => place.sql().pragma_update(None, "user_version", 7).unwrap(),
            _ => {
                let elsewhere = place.home.join("elsewhere");
                fs::create_dir(&elsewhere).unwrap();
                place.insert(&new_id(), "two\nlines", &s(&elsewhere));
            }
        }
        let before = tree(&place.store);
        for args in [
            vec!["project", "init", &backend, "--yes"],
            vec!["project", "adopt", &id, &backend, "--yes"],
            vec!["project", "unbind", &id, &work, "--yes"],
        ] {
            check(&place.run(&args), 2, STORE_FAILURE);
        }
        let done = place.run(&["project", "explain"]);
        let expected = explained(
            &place.store,
            &work,
            "unavailable",
            UNREADABLE,
            "none",
            "none",
        );
        check(&done, 1, &expected);
        assert_eq!(tree(&place.store), before, "{damage}");
    }
}

#[test]
fn no_path_can_forge_a_line_of_any_command() {
    let mut place = place();
    place.store = place.home.join("mem\nS");
    let (target, other) = (place.work.join("d\nD"), place.work.join("o\nO"));
    fs::create_dir(&target).unwrap();
    fs::create_dir(&other).unwrap();
    let shown_store = place.home.join("mem S");
    let work = s(&place.work);
    let (d, o) = (format!("{work}/d D"), format!("{work}/o O"));
    let (target, other) = (s(&target), s(&other));

    let done = place.run(&["project", "init", &target]);
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        plan("init", &shown_store, &format!("d D  {NEW}"), &d, "0")
    );
    check(&done, 2, &expected);
    let done = place.run(&["project", "init", &target, "--name", "n", "--yes"]);
    let id = place.id_named("n");
    let expected = format!(
        "{}created project n [{id}] and bound {d}\n{RESTART}",
        plan("init", &shown_store, &format!("n  {NEW}"), &d, "0")
    );
    check(&done, 0, &expected);

    let registered = format!("project: {id}\nname: n\nroot: {d}\n");
    let done = place.run(&["project", "explain", "--project-dir", &target]);
    check(
        &done,
        0,
        &explained(&shown_store, &d, "registered", &registered, &id, &id),
    );
    let done = place.run(&["project", "explain", "--project-dir", &other]);
    check(
        &done,
        0,
        &explained(&shown_store, &o, "none", "", "none", "none"),
    );
    let gone = format!("{work}/gone\nG");
    let done = place.run(&["project", "explain", "--project-dir", &gone]);
    let diagnostic = "diagnostic: working directory could not be resolved\n";
    let expected = explained(
        &shown_store,
        &format!("{work}/gone G"),
        "degraded",
        diagnostic,
        "none",
        "none",
    );
    check(&done, 1, &expected);

    let done = place.run(&["project", "adopt", &id, &target, "--yes"]);
    check(
        &done,
        0,
        &format!("{d} is already bound to {id}; nothing to do\n"),
    );
    let done = place.run(&["project", "adopt", &id, &other, "--yes"]);
    let expected =
        format!("refused: project {id} already has a directory ({d}); unbind it first\n");
    check(&done, 2, &expected);
    let done = place.run(&["project", "init", &target, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {d} is already bound to project {id}\n"),
    );
    let done = place.run(&["project", "unbind", &id, &other, "--yes"]);
    check(&done, 2, &format!("refused: {o} is not bound to {id}\n"));
    let done = place.run(&["project", "init", &gone, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {work}/gone G is not an existing directory\n"),
    );

    let shown = unbind_plan(&shown_store, &id, &d, "none");
    let done = place.run(&["project", "unbind", &id, &target]);
    check(&done, 2, &format!("{shown}{NOT_A_TERMINAL}"));
    let done = place.run(&["project", "unbind", &id, &target, "--yes"]);
    check(
        &done,
        0,
        &format!("{shown}unbound {d} from {id}\n{RESTART}"),
    );
    let shown = plan(
        "adopt",
        &shown_store,
        &format!("n [{id}]  (existing)"),
        &d,
        "0",
    );
    let done = place.run(&["project", "adopt", &id, &target]);
    check(&done, 2, &format!("{shown}{NOT_A_TERMINAL}"));
    let done = place.run(&["project", "adopt", &id, &target, "--yes"]);
    check(&done, 0, &format!("{shown}bound {d} to {id}\n{RESTART}"));

    let inside = place.store.join("in\nI");
    fs::create_dir(&inside).unwrap();
    let done = place.run(&["project", "init", &s(&inside), "--yes"]);
    let store = s(&shown_store);
    check(
        &done,
        2,
        &format!("refused: {store}/in I lies inside the memory store {store}\n"),
    );
    let covered = format!("{work}/frontend/mem\nS");
    let parent = s(&place.home.join("99_git"));
    let done = place.run(&["project", "init", &parent, "--root", &covered, "--yes"]);
    let expected = format!(
        "refused: the memory store {work}/frontend/mem S lies inside this directory; the store \
         would end up inside the project\n"
    );
    check(&done, 2, &expected);
}

#[test]
fn a_store_reached_through_a_link_is_shown_where_it_is() {
    let mut place = place();
    let (real, link) = (place.home.join("store-a"), place.home.join("store-link"));
    fs::create_dir_all(real.join("in")).unwrap();
    symlink(&real, &link).unwrap();
    place.store = link.clone();
    let done = place.run(&["project", "explain"]);
    let work = s(&place.work);
    check(
        &done,
        0,
        &explained(&real, &work, "none", "", "none", "none"),
    );
    // the directory as it was typed, the store as it is
    let id = place.unbound_project("q");
    let (typed, shown) = (s(&link.join("in")), s(&real));
    let expected = format!("refused: {typed} lies inside the memory store {shown}\n");
    let done = place.run(&["project", "init", &typed, "--yes"]);
    check(&done, 2, &expected);
    let done = place.run(&["project", "adopt", &id, &typed, "--yes"]);
    check(&done, 2, &expected);
    let done = place.run(&["project", "unbind", &id, &typed, "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {typed} is not bound to {id}\n"),
    );
    let elsewhere = place.tmp.join("elsewhere-home");
    let env = environment(&place.sandbox, &elsewhere, &link);
    let args = ["project", "init", &s(&place.home), "--yes"];
    let done = run_in(&env, &elsewhere, &place.work, &args, "", false);
    let expected = format!(
        "refused: the memory store {shown} lies inside this directory; the store would end up \
         inside the project\n"
    );
    check(&done, 2, &expected);
}

#[test]
fn a_refusal_shows_the_directory_without_its_dots_and_doubled_slashes() {
    let place = place();
    let work = s(&place.work);
    let done = place.run(&["project", "init", "nope/.", "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {work}/nope is not an existing directory\n"),
    );
    let done = place.run(&["project", "init", "nope//x/", "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {work}/nope/x is not an existing directory\n"),
    );
    let id = place.unbound_project("q");
    let done = place.run(&["project", "unbind", &id, "backend/.", "--yes"]);
    check(
        &done,
        2,
        &format!("refused: {work}/backend is not bound to {id}\n"),
    );
}

#[test]
fn a_directory_reached_through_a_link_is_planned_as_the_directory_it_is() {
    let place = place();
    let frontend = s(&place.work.join("frontend"));
    symlink(&frontend, place.work.join("lnk")).unwrap();
    // the default name is the directory's, not the link's
    let done = place.run(&["project", "init", "lnk"]);
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        plan(
            "init",
            &place.store,
            &format!("frontend  {NEW}"),
            &frontend,
            "0"
        )
    );
    check(&done, 2, &expected);
    let id = place.unbound_project("q");
    let done = place.run(&["project", "adopt", &id, "lnk", "--yes"]);
    let expected = format!(
        "{}bound {frontend} to {id}\n{RESTART}",
        plan(
            "adopt",
            &place.store,
            &format!("q [{id}]  (existing)"),
            &frontend,
            "0"
        )
    );
    check(&done, 0, &expected);
    let done = place.run(&["project", "adopt", &id, "lnk", "--yes"]);
    check(
        &done,
        0,
        &format!("{frontend} is already bound to {id}; nothing to do\n"),
    );
}

#[test]
fn a_store_that_breaks_while_the_prompt_is_open_is_a_store_failure() {
    for command in ["init", "adopt", "unbind"] {
        let place = place();
        let (frontend, backend) = (s(&place.work.join("frontend")), place.work.join("backend"));
        let id = place.init_bound(&backend, "p");
        if command == "adopt" {
            place.core_unbind(&id, &backend);
        }
        let backend = s(&backend);
        let (args, shown): (Vec<&str>, String) = match command {
            "init" => (
                vec!["project", "init", &frontend],
                plan(
                    "init",
                    &place.store,
                    &format!("frontend  {NEW}"),
                    &frontend,
                    "0",
                ),
            ),
            "adopt" => (
                vec!["project", "adopt", &id, &frontend],
                plan(
                    "adopt",
                    &place.store,
                    &format!("p [{id}]  (existing)"),
                    &frontend,
                    "0",
                ),
            ),
            _ => (
                vec!["project", "unbind", &id, &backend],
                unbind_plan(&place.store, &id, &backend, "none"),
            ),
        };
        let done = place.ask_while(&place.work, &args, || {
            fs::write(place.database(), b"not a database").unwrap()
        });
        check(&done, 2, &format!("{shown}{PROMPT}{STORE_FAILURE}"));
    }
}

#[test]
fn unbind_of_a_project_removed_during_confirmation_says_the_binding_changed() {
    let place = place();
    let backend = place.work.join("backend");
    let id = place.init_bound(&backend, "p");
    let backend = s(&backend);
    let done = place.ask_while(&place.work, &["project", "unbind", &id, &backend], || {
        place
            .sql()
            .execute("DELETE FROM projects WHERE id = ?1", [&id])
            .unwrap();
    });
    let expected = format!(
        "{}{PROMPT}{BINDING_CHANGED}",
        unbind_plan(&place.store, &id, &backend, "none")
    );
    check(&done, 2, &expected);
}
