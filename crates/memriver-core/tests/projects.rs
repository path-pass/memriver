//! Projects and their directories: planning, binding, unbinding and resolving, every
//! refusal by the scenario that causes it, and the context a directory grants.

use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Barrier};
use std::thread;

use memriver_core::application::contexts::{NONE_HEADER, STORE_UNREADABLE_HEADER};
use memriver_core::application::projects::{InitError, ProjectService};
use memriver_core::models::context::{ProjectContextState, ReadWriteSet, ResolutionState};
use memriver_core::models::errors::{BindingReason, CoreError};
use memriver_core::models::helpers::new_id;
use memriver_core::models::project::Project;
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::repository::sqlite::project_store::{GLOBAL_PROJECT_NAME, ProjectStore};
use memriver_core::settings::{HEADER_FIELD_CHARS, PROJECT_NAME_MAX_CHARS};
use memriver_test_support::Sandbox;
use rusqlite::Connection;

struct Env {
    _sandbox: Sandbox,
    tmp: PathBuf,
    home: PathBuf,
    store: PathBuf,
    work: PathBuf,
    projects: ProjectStore,
}

fn setup() -> Env {
    let sandbox = Sandbox::new();
    let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
    let (home, store, work) = (tmp.join("home"), tmp.join("store"), tmp.join("work"));
    fs::create_dir_all(&home).unwrap();
    fs::create_dir(&work).unwrap();
    let projects = ProjectStore::with_busy_timeout(&store, &home, 2000);
    Env {
        _sandbox: sandbox,
        tmp,
        home,
        store,
        work,
        projects,
    }
}

impl Env {
    fn dir(&self, name: &str) -> PathBuf {
        let dir = self.tmp.join(name);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn service(&self) -> ProjectService {
        ProjectService::new(
            ProjectStore::with_busy_timeout(&self.store, &self.home, 2000),
            HEADER_FIELD_CHARS,
            PROJECT_NAME_MAX_CHARS,
        )
    }

    /// A connection of someone who is not memriver.
    fn raw(&self) -> Connection {
        Connection::open(self.store.join(DATABASE_FILENAME)).unwrap()
    }

    fn insert(&self, id: &str, name: &str, root: Option<&str>, is_global: bool) {
        self.raw()
            .execute(
                "INSERT INTO projects (id, name, root, is_global) VALUES (?1, ?2, ?3, ?4)",
                (id, name, root, is_global),
            )
            .unwrap();
    }

    fn unbind(&self, project: &Project) {
        let root = project.root.as_deref().unwrap();
        let (plan, _) = self
            .projects
            .plan_unbind(&project.id, root, &self.tmp)
            .unwrap();
        self.projects.unbind(&plan).unwrap();
    }
}

fn text(path: &Path) -> &str {
    path.to_str().unwrap()
}

fn init(projects: &ProjectStore, directory: &Path, name: &str) -> Project {
    let project = Project::new(name, 120).unwrap();
    let plan = projects.plan_root(directory, None).unwrap();
    projects.create(&project, &plan).unwrap();
    Project {
        root: Some(plan.root),
        ..project
    }
}

/// The reason a call was refused with, and the project it names.
fn refused<T: std::fmt::Debug>(result: Result<T, CoreError>) -> (BindingReason, Option<String>) {
    match result {
        Err(CoreError::BindingRefused { reason, project_id }) => (reason, project_id),
        other => panic!("not refused: {other:?}"),
    }
}

fn reason<T: std::fmt::Debug>(result: Result<T, CoreError>) -> BindingReason {
    refused(result).0
}

fn new_project(name: &str) -> Project {
    Project::new(name, 120).unwrap()
}

/// Whether two names that differ only in case are one directory here.
fn case_alias(env: &Env) -> Option<PathBuf> {
    let alias = env.tmp.join("WORK");
    alias.exists().then_some(alias)
}

// --- creating and reading ---

#[test]
fn init_creates_the_store_and_global() {
    let env = setup();
    let service = env.service();
    assert!(!env.store.exists());
    assert_eq!(service.global_project_id(), Ok(None));
    assert_eq!(service.list_projects(), Ok(vec![]));
    assert!(!env.store.exists(), "a read creates nothing");

    let global = service.ensure_global().unwrap();
    assert_eq!(service.ensure_global().as_ref(), Ok(&global));
    assert_eq!(service.global_project_id(), Ok(Some(global.clone())));
    let read = service.read_project(&global).unwrap();
    assert_eq!(
        (read.name.as_str(), read.root.as_deref()),
        (GLOBAL_PROJECT_NAME, None)
    );

    let plan = service.plan_root(&env.work, None).unwrap();
    assert_eq!(
        (plan.root.as_str(), plan.store.as_str()),
        (text(&env.work), text(&env.store))
    );
    assert!(plan.nested.is_empty() && !plan.already_bound);
    let project = service.init_project("  demo\nproject ", &plan).unwrap();
    assert_eq!(
        (project.name.as_str(), project.root.as_deref()),
        ("demo project", Some(text(&env.work)))
    );
    assert_eq!(service.read_project(&project.id), Ok(project.clone()));
    assert_eq!(service.list_projects().unwrap(), [project, read]);
}

#[test]
fn init_creates_the_project_and_its_directory_together() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let read = env.projects.read(&project.id).unwrap();
    assert_eq!(
        (read.name.as_str(), read.root.as_deref()),
        ("demo", Some(text(&env.work)))
    );
    // the store did not exist before, and what was made is private
    let mode = |path: &Path| fs::metadata(path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(&env.store), 0o700);
    assert_eq!(mode(&env.store.join(DATABASE_FILENAME)), 0o600);
}

#[test]
fn the_database_file_is_private() {
    let env = setup();
    env.projects.ensure_global().unwrap();
    let mode = fs::metadata(env.store.join(DATABASE_FILENAME))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
}

#[test]
fn a_name_that_is_not_one_creates_nothing() {
    let env = setup();
    let service = env.service();
    let plan = service.plan_root(&env.work, None).unwrap();
    for (name, sentence) in [
        ("", "project name is empty"),
        (" \n ", "project name is empty"),
        (
            &"x".repeat(121),
            "project name is longer than 120 characters",
        ),
    ] {
        let Err(InitError::Name(err)) = service.init_project(name, &plan) else {
            panic!("accepted {name:?}");
        };
        assert_eq!(err.to_string(), sentence);
    }
    assert!(!env.store.exists());
    assert!(service.init_project(&"\u{9879}".repeat(120), &plan).is_ok());
}

#[test]
fn create_never_reuses_an_id() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let other = env.dir("other");
    let again = Project {
        id: project.id.clone(),
        name: "again".to_string(),
        root: None,
    };
    let plan = env.projects.plan_root(&other, None).unwrap();
    assert_eq!(
        env.projects.create(&again, &plan),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(env.projects.read(&project.id).unwrap().name, "demo");
    // an id that is none is not written either
    let malformed = Project {
        id: "../../evil".to_string(),
        ..again
    };
    assert_eq!(
        env.projects.create(&malformed, &plan),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(env.projects.list().unwrap().len(), 1);
}

#[test]
fn list_projects_puts_global_last() {
    let env = setup();
    let global = env.projects.ensure_global().unwrap();
    let b = init(&env.projects, &env.dir("one"), "b");
    let a = init(&env.projects, &env.dir("two"), "a");
    let upper = init(&env.projects, &env.dir("three"), "B");
    let ids: Vec<String> = env
        .projects
        .list()
        .unwrap()
        .into_iter()
        .map(|p| p.id)
        .collect();
    // by name as bytes, then global
    assert_eq!(ids, [upper.id, a.id, b.id, global]);
}

#[test]
fn ensure_global_racing_from_nothing_returns_one_id() {
    let env = setup();
    let barrier = Arc::new(Barrier::new(2));
    let calls: Vec<_> = (0..2)
        .map(|_| {
            let (store, home, barrier) = (env.store.clone(), env.home.clone(), barrier.clone());
            thread::spawn(move || {
                let projects = ProjectStore::new(&store, &home);
                barrier.wait();
                projects.ensure_global().unwrap()
            })
        })
        .collect();
    let ids: Vec<String> = calls.into_iter().map(|call| call.join().unwrap()).collect();
    assert_eq!(ids[0], ids[1]);
    let count: i64 = env
        .raw()
        .query_row(
            "SELECT count(*) FROM projects WHERE is_global = 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}

#[test]
fn an_invalid_project_row_is_storage_failure_not_absence() {
    let env = setup();
    let global = env.projects.ensure_global().unwrap();
    let bad = new_id();
    env.insert(&bad, "two\nlines", None, false);
    assert_eq!(env.projects.read(&bad), Err(CoreError::StorageFailure));
    let absent = new_id();
    assert_eq!(
        env.projects.read(&absent),
        Err(CoreError::ProjectNotFound(absent))
    );
    for malformed in ["not-an-id", "", "../../evil"] {
        assert_eq!(
            env.projects.read(malformed),
            Err(CoreError::ProjectNotFound(malformed.to_string()))
        );
    }
    // a bulk scan skips the row that a read of it still fails on
    let ids: Vec<String> = env
        .projects
        .list()
        .unwrap()
        .into_iter()
        .map(|p| p.id)
        .collect();
    assert_eq!(ids, [global]);
    assert_eq!(env.projects.read(&bad), Err(CoreError::StorageFailure));
}

#[test]
fn an_invalid_bound_row_fails_resolution_and_planning_whole() {
    // a partly read set of bindings could hide the nearer project: no skipping
    let env = setup();
    env.projects.ensure_global().unwrap();
    let elsewhere = env.dir("elsewhere");
    env.insert(&new_id(), "two\nlines", Some(text(&elsewhere)), false);
    assert_eq!(
        env.projects.resolve(&env.work, None, None),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(
        env.projects.plan_root(&env.work, None),
        Err(CoreError::StorageFailure)
    );
    // an invalid global row
    let env = setup();
    env.projects.ensure_global().unwrap();
    env.raw()
        .execute("UPDATE projects SET name = 'two\nlines'", [])
        .unwrap();
    assert_eq!(
        env.projects.global_project_id(),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(env.projects.ensure_global(), Err(CoreError::StorageFailure));
}

// --- planning: every refusal by its scenario ---

#[test]
fn plan_refuses_a_missing_directory_and_a_file() {
    let env = setup();
    fs::write(env.tmp.join("file"), "x").unwrap();
    symlink(env.tmp.join("loop"), env.tmp.join("loop")).unwrap();
    for target in ["nope", "file", "loop", "file/below", "x\0y"] {
        assert_eq!(
            refused(env.projects.plan_root(&env.tmp.join(target), None)),
            (BindingReason::NotADirectory, None),
            "{target:?}"
        );
    }
    assert!(!env.store.exists());
}

#[test]
fn plan_refuses_home_its_ancestors_and_the_filesystem_root() {
    let env = setup();
    symlink(&env.home, env.tmp.join("home-alias")).unwrap();
    for target in [
        &env.home,
        &env.tmp,
        &PathBuf::from("/"),
        &env.tmp.join("home-alias"),
    ] {
        assert_eq!(
            reason(env.projects.plan_root(target, None)),
            BindingReason::CoversHome,
            "{target:?}"
        );
    }
    // a home that is written through a link is the directory it leads to
    let real_home = env.dir("elsewhere/real-home");
    symlink(&real_home, env.tmp.join("linked-home")).unwrap();
    let projects = ProjectStore::new(&env.store, &env.tmp.join("linked-home"));
    assert_eq!(
        reason(projects.plan_root(&real_home, None)),
        BindingReason::CoversHome
    );
    assert_eq!(
        reason(projects.plan_root(&env.tmp.join("elsewhere"), None)),
        BindingReason::CoversHome
    );
    // below the home directory is fine
    let below = env.dir("home/projects/x");
    assert!(env.projects.plan_root(&below, None).is_ok());
}

#[test]
fn plan_refuses_the_store_inside_the_directory_and_the_directory_inside_the_store() {
    let env = setup();
    let inner = env.dir("store/inner");
    let outer = env.dir("outer");
    fs::create_dir(outer.join("store")).unwrap();
    let nested = ProjectStore::new(&outer.join("store"), &env.home);
    assert_eq!(
        reason(nested.plan_root(&outer, None)),
        BindingReason::CoversStore
    );
    assert_eq!(
        reason(nested.plan_root(&outer.join("store"), None)),
        BindingReason::CoversStore
    );
    assert_eq!(
        reason(env.projects.plan_root(&inner, None)),
        BindingReason::InsideStore
    );
    // a store that does not exist yet is where it will be
    let planned = ProjectStore::new(&outer.join("later/store"), &env.home);
    assert_eq!(
        reason(planned.plan_root(&outer, None)),
        BindingReason::CoversStore
    );
    // a store reached through a link is where the link leads
    symlink(&outer, env.tmp.join("via")).unwrap();
    let linked = ProjectStore::new(&env.tmp.join("via/store"), &env.home);
    assert_eq!(
        reason(linked.plan_root(&outer, None)),
        BindingReason::CoversStore
    );
    assert_eq!(
        linked.plan_root(&env.work, None).unwrap().store,
        text(&outer.join("store"))
    );
}

#[test]
fn a_directory_bound_elsewhere_is_refused_and_names_the_owner() {
    let env = setup();
    let owner = init(&env.projects, &env.work, "demo");
    symlink(&env.work, env.tmp.join("alias")).unwrap();
    for target in [&env.work, &env.tmp.join("alias"), &env.work.join(".")] {
        assert_eq!(
            refused(env.projects.plan_root(target, None)),
            (BindingReason::BoundElsewhere, Some(owner.id.clone())),
            "{target:?}"
        );
    }
    // also for another project that wants it
    let other = init(&env.projects, &env.dir("other"), "other");
    env.unbind(&other);
    assert_eq!(
        refused(env.projects.plan_root(&env.work, Some(&other.id))),
        (BindingReason::BoundElsewhere, Some(owner.id))
    );
}

#[test]
fn on_a_file_system_that_ignores_case_a_case_alias_of_a_bound_directory_is_bound_elsewhere() {
    let env = setup();
    let owner = init(&env.projects, &env.work, "demo");
    let Some(alias) = case_alias(&env) else {
        return;
    };
    assert_eq!(
        refused(env.projects.plan_root(&alias, None)),
        (BindingReason::BoundElsewhere, Some(owner.id.clone()))
    );
    // adopting the alias of the directory the project holds is a no-op
    let plan = env.projects.plan_root(&alias, Some(&owner.id)).unwrap();
    assert!(plan.already_bound);
    let before = fs::read(env.store.join(DATABASE_FILENAME)).unwrap();
    env.projects.bind(&owner.id, &plan).unwrap();
    assert_eq!(
        env.projects.read(&owner.id).unwrap().root.as_deref(),
        Some(text(&env.work))
    );
    assert_eq!(fs::read(env.store.join(DATABASE_FILENAME)).unwrap(), before);
}

#[test]
fn an_uncheckable_comparison_is_unverifiable_never_a_non_match() {
    let env = setup();
    let locked = env.dir("locked");
    let hidden = env.dir("locked/bound");
    let project = init(&env.projects, &hidden, "demo");
    let other = env.dir("other");
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    let stopped = fs::metadata(&hidden).is_err();
    let planned = env.projects.plan_root(&other, None);
    let unbound = env.projects.plan_unbind(&project.id, text(&hidden), &other);
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o700)).unwrap();
    if !stopped {
        return; // nothing stops this user
    }
    // whether `other` is the bound directory could not be checked
    assert_eq!(reason(planned), BindingReason::Unverifiable);
    // the binding can still be named and removed by its text
    let (plan, preview) = unbound.unwrap();
    assert_eq!(plan.root, text(&hidden));
    assert_eq!(preview.state, ResolutionState::None);
}

#[test]
fn plan_lists_bound_directories_under_the_target_as_nested() {
    let env = setup();
    let inner = init(&env.projects, &env.dir("work/child"), "inner");
    let deeper = init(&env.projects, &env.dir("work/a/b"), "deeper");
    init(&env.projects, &env.dir("beside"), "beside");
    let plan = env.projects.plan_root(&env.work, None).unwrap();
    let mut nested: Vec<&str> = plan.nested.iter().map(|p| p.id.as_str()).collect();
    nested.sort();
    let mut expected = [inner.id.as_str(), deeper.id.as_str()];
    expected.sort();
    assert_eq!(nested, expected);
    // nested projects stay independent: the outer one can be created, and each
    // directory resolves to its nearest
    let outer = init(&env.projects, &env.work, "outer");
    let resolved = |dir: &str| {
        let resolution = env
            .projects
            .resolve(&env.tmp.join(dir), None, None)
            .unwrap();
        resolution.project.unwrap().id
    };
    assert_eq!(resolved("work"), outer.id);
    assert_eq!(resolved("work/child"), inner.id);
    assert_eq!(resolved("work/a"), outer.id);
    assert_eq!(resolved("work/a/b"), deeper.id);
    // and a directory inside a bound one can be bound too
    assert!(
        env.projects
            .plan_root(&env.dir("work/a"), None)
            .unwrap()
            .nested
            .len()
            == 1
    );
}

#[test]
fn adopt_refuses_global_and_unknown_projects() {
    let env = setup();
    assert_eq!(
        reason(env.projects.plan_root(&env.work, Some(&new_id()))),
        BindingReason::NoSuchProject,
        "no store"
    );
    let global = env.projects.ensure_global().unwrap();
    assert_eq!(
        reason(env.projects.plan_root(&env.work, Some(&global))),
        BindingReason::IsGlobal
    );
    for unknown in [new_id().as_str(), "not-an-id", ""] {
        assert_eq!(
            reason(env.projects.plan_root(&env.work, Some(unknown))),
            BindingReason::NoSuchProject
        );
        let plan = env.projects.plan_root(&env.work, None).unwrap();
        assert_eq!(
            reason(env.projects.bind(unknown, &plan)),
            BindingReason::NoSuchProject
        );
    }
    let plan = env.projects.plan_root(&env.work, None).unwrap();
    assert_eq!(
        reason(env.projects.bind(&global, &plan)),
        BindingReason::IsGlobal
    );
    // the project is looked at before the directory
    assert_eq!(
        reason(env.projects.plan_root(&env.tmp.join("nope"), Some(&global))),
        BindingReason::IsGlobal
    );
}

#[test]
fn a_project_with_another_directory_must_unbind_first() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let other = env.dir("other");
    assert_eq!(
        reason(env.projects.plan_root(&other, Some(&project.id))),
        BindingReason::HasDirectory
    );
}

// --- binding ---

#[test]
fn adopt_binds_an_unbound_project_and_is_idempotent() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let plan = env
        .projects
        .plan_root(&env.work, Some(&project.id))
        .unwrap();
    assert!(plan.already_bound);
    env.projects.bind(&project.id, &plan).unwrap(); // same directory: nothing to do
    env.unbind(&project);
    assert_eq!(env.projects.read(&project.id).unwrap().root, None);
    let plan = env
        .projects
        .plan_root(&env.work, Some(&project.id))
        .unwrap();
    assert!(!plan.already_bound);
    env.projects.bind(&project.id, &plan).unwrap();
    assert_eq!(
        env.projects.read(&project.id).unwrap().root.as_deref(),
        Some(text(&env.work))
    );
}

#[test]
fn bind_decides_on_the_current_row_not_on_the_plan() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let stale = env
        .projects
        .plan_root(&env.work, Some(&project.id))
        .unwrap();
    assert!(stale.already_bound);
    env.unbind(&project); // a peer unbinds meanwhile
    env.projects.bind(&project.id, &stale).unwrap();
    assert_eq!(
        env.projects.read(&project.id).unwrap().root.as_deref(),
        Some(text(&env.work))
    );
}

#[test]
fn create_refuses_a_target_a_peer_bound_after_planning() {
    let env = setup();
    let other = env.dir("other");
    let plan = env.projects.plan_root(&other, None).unwrap();
    let peer = init(&env.projects, &other, "peer");
    assert_eq!(
        refused(env.projects.create(&new_project("x"), &plan)),
        (BindingReason::BoundElsewhere, Some(peer.id.clone()))
    );
    let ids: Vec<String> = env
        .projects
        .list()
        .unwrap()
        .into_iter()
        .map(|p| p.id)
        .collect();
    assert_eq!(ids, [peer.id]);
}

#[test]
fn bind_refuses_a_project_a_peer_bound_elsewhere_after_planning() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    env.unbind(&project);
    let plan = env
        .projects
        .plan_root(&env.dir("other"), Some(&project.id))
        .unwrap();
    let third = env.dir("third");
    let peer = env.projects.plan_root(&third, Some(&project.id)).unwrap();
    env.projects.bind(&project.id, &peer).unwrap();
    assert_eq!(
        reason(env.projects.bind(&project.id, &plan)),
        BindingReason::HasDirectory
    );
    assert_eq!(
        env.projects.read(&project.id).unwrap().root.as_deref(),
        Some(text(&third))
    );
    // and a directory a peer gave to another project meanwhile
    let fourth = env.dir("fourth");
    let unbound = init(&env.projects, &env.dir("fifth"), "unbound");
    env.unbind(&unbound);
    let plan = env.projects.plan_root(&fourth, Some(&unbound.id)).unwrap();
    let owner = init(&env.projects, &fourth, "owner");
    assert_eq!(
        refused(env.projects.bind(&unbound.id, &plan)),
        (BindingReason::BoundElsewhere, Some(owner.id))
    );
}

#[test]
fn bind_and_unbind_against_a_deleted_store_create_nothing() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let (unbind_plan, _) = env
        .projects
        .plan_unbind(&project.id, text(&env.work), &env.tmp)
        .unwrap();
    env.projects.unbind(&unbind_plan).unwrap();
    let bind_plan = env
        .projects
        .plan_root(&env.work, Some(&project.id))
        .unwrap();
    fs::remove_dir_all(&env.store).unwrap();
    assert_eq!(
        reason(env.projects.unbind(&unbind_plan)),
        BindingReason::BindingChanged
    );
    assert!(!env.store.exists());
    assert_eq!(
        reason(env.projects.bind(&project.id, &bind_plan)),
        BindingReason::NoSuchProject
    );
    assert!(!env.store.exists());
}

// --- the plan is out of date ---

#[test]
fn plan_out_of_date_is_refused() {
    // the directory is gone
    let env = setup();
    let plan = env.projects.plan_root(&env.work, None).unwrap();
    fs::remove_dir(&env.work).unwrap();
    assert_eq!(
        reason(env.projects.create(&new_project("x"), &plan)),
        BindingReason::PlanChanged
    );
    assert_eq!(env.projects.list(), Ok(vec![]));
    assert!(!env.store.exists(), "a refused plan creates no store");

    // the directory was re-pointed through a link
    let env = setup();
    let plan = env.projects.plan_root(&env.work, None).unwrap();
    let elsewhere = env.dir("elsewhere");
    fs::remove_dir(&env.work).unwrap();
    symlink(&elsewhere, &env.work).unwrap();
    assert_eq!(
        reason(env.projects.create(&new_project("x"), &plan)),
        BindingReason::PlanChanged
    );
    assert_eq!(env.projects.list(), Ok(vec![]));

    // the same for a project that exists
    let env = setup();
    let project = init(&env.projects, &env.dir("first"), "demo");
    env.unbind(&project);
    let plan = env
        .projects
        .plan_root(&env.work, Some(&project.id))
        .unwrap();
    fs::remove_dir(&env.work).unwrap();
    assert_eq!(
        reason(env.projects.bind(&project.id, &plan)),
        BindingReason::PlanChanged
    );
    assert_eq!(env.projects.read(&project.id).unwrap().root, None);

    // what the plan was refused for when it was made is checked again when it is used:
    // the home directory moved into the planned directory
    let env = setup();
    let moving = env.tmp.join("moving-home");
    symlink(&env.home, &moving).unwrap();
    let projects = ProjectStore::new(&env.store, &moving);
    let plan = projects.plan_root(&env.work, None).unwrap();
    fs::remove_file(&moving).unwrap();
    symlink(env.dir("work/inside"), &moving).unwrap();
    assert_eq!(
        reason(projects.create(&new_project("x"), &plan)),
        BindingReason::CoversHome
    );
    assert!(!env.store.exists());
}

#[test]
fn a_store_redirected_to_an_empty_place_during_the_prompt_gets_no_database() {
    let env = setup();
    let (link, real) = (env.tmp.join("link"), env.dir("real"));
    symlink(&real, &link).unwrap();
    let projects = ProjectStore::new(&link, &env.home);
    let plan = projects.plan_root(&env.work, None).unwrap();
    assert_eq!(plan.store, text(&real));
    let other = env.dir("other-store");
    fs::remove_file(&link).unwrap();
    symlink(&other, &link).unwrap();
    assert_eq!(
        reason(projects.create(&new_project("x"), &plan)),
        BindingReason::PlanChanged
    );
    assert!(!other.join(DATABASE_FILENAME).exists());
    assert!(!real.join(DATABASE_FILENAME).exists());
}

#[test]
fn an_unbind_plan_catches_a_store_redirected_during_the_prompt() {
    let env = setup();
    let (link, real) = (env.tmp.join("link"), env.dir("real"));
    symlink(&real, &link).unwrap();
    let projects = ProjectStore::new(&link, &env.home);
    let project = init(&projects, &env.work, "demo");
    let (plan, _) = projects
        .plan_unbind(&project.id, text(&env.work), &env.tmp)
        .unwrap();
    let copy = env.dir("copy");
    fs::copy(real.join(DATABASE_FILENAME), copy.join(DATABASE_FILENAME)).unwrap();
    fs::remove_file(&link).unwrap();
    symlink(&copy, &link).unwrap();
    assert_eq!(reason(projects.unbind(&plan)), BindingReason::PlanChanged);
    let root: String = Connection::open(copy.join(DATABASE_FILENAME))
        .unwrap()
        .query_row(
            "SELECT root FROM projects WHERE id = ?1",
            [&project.id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(root, text(&env.work));
    // a bind is caught the same way
    env.raw_at(&copy, &project.id);
    let bind_plan = ProjectStore::new(&copy, &env.home)
        .plan_root(&env.work, Some(&project.id))
        .unwrap();
    assert_eq!(
        reason(projects.bind(&project.id, &plan_for(&bind_plan, &real))),
        BindingReason::PlanChanged
    );
}

impl Env {
    /// Unbind `project_id` in the store at `root`, as someone who is not memriver.
    fn raw_at(&self, root: &Path, project_id: &str) {
        Connection::open(root.join(DATABASE_FILENAME))
            .unwrap()
            .execute(
                "UPDATE projects SET root = NULL WHERE id = ?1",
                [project_id],
            )
            .unwrap();
    }
}

/// `plan` as it would have been made against the store at `store`.
fn plan_for(
    plan: &memriver_core::models::project::RootPlan,
    store: &Path,
) -> memriver_core::models::project::RootPlan {
    memriver_core::models::project::RootPlan {
        store: text(store).to_string(),
        ..plan.clone()
    }
}

// --- unbinding ---

#[test]
fn unbind_needs_the_exact_confirmed_pair() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let (plan, preview) = env
        .projects
        .plan_unbind(&project.id, text(&env.work), &env.work)
        .unwrap();
    assert_eq!(
        (
            plan.project_id.as_str(),
            plan.root.as_str(),
            plan.store.as_str()
        ),
        (project.id.as_str(), text(&env.work), text(&env.store))
    );
    assert_eq!(
        preview.state,
        ResolutionState::None,
        "the preview ignores this binding"
    );
    env.projects.unbind(&plan).unwrap();
    assert_eq!(
        reason(env.projects.unbind(&plan)),
        BindingReason::BindingChanged
    );
    let again = env
        .projects
        .plan_root(&env.work, Some(&project.id))
        .unwrap();
    env.projects.bind(&project.id, &again).unwrap();
    env.projects.unbind(&plan).unwrap(); // unbound then rebound: same pair, allowed
    // bound to another directory since: not the pair that was shown
    let elsewhere = env
        .projects
        .plan_root(&env.dir("elsewhere"), Some(&project.id))
        .unwrap();
    env.projects.bind(&project.id, &elsewhere).unwrap();
    assert_eq!(
        reason(env.projects.unbind(&plan)),
        BindingReason::BindingChanged
    );
}

#[test]
fn the_preview_of_an_unbind_shows_what_the_directory_resolves_to_afterwards() {
    let env = setup();
    let outer = init(&env.projects, &env.work, "outer");
    let inner = init(&env.projects, &env.dir("work/inner"), "inner");
    let (_, preview) = env
        .projects
        .plan_unbind(
            &inner.id,
            text(&env.work.join("inner")),
            &env.work.join("inner"),
        )
        .unwrap();
    assert_eq!(preview.project.unwrap().id, outer.id);
    // nothing was changed by planning
    let resolved = env
        .projects
        .resolve(&env.work.join("inner"), None, None)
        .unwrap();
    assert_eq!(resolved.project.unwrap().id, inner.id);
}

#[test]
fn unbind_of_a_directory_deleted_from_disk_still_works() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    fs::remove_dir(&env.work).unwrap();
    let (plan, _) = env
        .projects
        .plan_unbind(&project.id, text(&env.work), &env.tmp)
        .unwrap();
    assert_eq!(plan.root, text(&env.work));
    env.projects.unbind(&plan).unwrap();
    assert_eq!(env.projects.read(&project.id).unwrap().root, None);
}

#[test]
fn unbind_matches_the_stored_root_by_its_realpath_too() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let link = env.tmp.join("via");
    symlink(&env.work, &link).unwrap();
    for given in [
        text(&link).to_string(),
        format!("{}/../work/.", text(&env.work)),
    ] {
        let (plan, _) = env
            .projects
            .plan_unbind(&project.id, &given, &env.tmp)
            .unwrap();
        assert_eq!(plan.root, text(&env.work), "{given}");
    }
}

#[test]
fn unbind_plans_refuse_unknown_projects_and_foreign_roots() {
    let env = setup();
    let root = Path::new("/");
    assert_eq!(
        reason(env.projects.plan_unbind(&new_id(), "/x", root)),
        BindingReason::NoSuchProject,
        "no store"
    );
    let project = init(&env.projects, &env.work, "demo");
    for unknown in [new_id().as_str(), "not-an-id"] {
        assert_eq!(
            reason(env.projects.plan_unbind(unknown, "/x", root)),
            BindingReason::NoSuchProject
        );
    }
    for foreign in [text(&env.tmp.join("other")), text(&env.tmp), "", "work"] {
        assert_eq!(
            reason(env.projects.plan_unbind(&project.id, foreign, root)),
            BindingReason::BindingChanged,
            "{foreign:?}"
        );
    }
    // a project that holds no directory
    env.unbind(&project);
    assert_eq!(
        reason(env.projects.plan_unbind(&project.id, text(&env.work), root)),
        BindingReason::BindingChanged
    );
    let global = env.projects.ensure_global().unwrap();
    assert_eq!(
        reason(env.projects.plan_unbind(&global, text(&env.work), root)),
        BindingReason::BindingChanged
    );
}

// --- resolving ---

#[test]
fn resolve_finds_the_nearest_bound_directory_and_reports_its_project() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let src = env.dir("work/src");
    let resolution = env.projects.resolve(&src, None, None).unwrap();
    assert_eq!(resolution.state, ResolutionState::Registered);
    assert_eq!(resolution.project, Some(project.clone()));
    assert_eq!(resolution.diagnostic, None);
    let outside = env.projects.resolve(&env.tmp, None, None).unwrap();
    assert_eq!(
        (outside.state, outside.project, outside.diagnostic),
        (ResolutionState::None, None, None)
    );
    // without that binding
    let ignored = env
        .projects
        .resolve(&src, Some((&project.id, text(&env.work))), None)
        .unwrap();
    assert_eq!(ignored.state, ResolutionState::None);
    // the pair must be the binding: another root for that project ignores nothing
    let kept = env
        .projects
        .resolve(&src, Some((&project.id, "/elsewhere")), None)
        .unwrap();
    assert_eq!(kept.state, ResolutionState::Registered);
}

#[test]
fn resolve_walks_the_logical_path_when_given() {
    let env = setup();
    let project = init(&env.projects, &env.work, "demo");
    let elsewhere = env.dir("elsewhere");
    let logical = env.work.join("only-on-a-branch");
    let resolution = env
        .projects
        .resolve(&elsewhere, None, Some(&logical))
        .unwrap();
    assert_eq!(resolution.project.map(|p| p.id), Some(project.id));
    assert_eq!(
        env.projects.resolve(&elsewhere, None, None).unwrap().state,
        ResolutionState::None
    );
}

#[test]
fn resolve_of_a_missing_store_is_none_and_creates_nothing() {
    let env = setup();
    assert_eq!(
        env.projects.resolve(&env.work, None, None).unwrap().state,
        ResolutionState::None
    );
    // a start that is no directory is degraded even then
    let degraded = env
        .projects
        .resolve(&env.tmp.join("nope"), None, None)
        .unwrap();
    assert_eq!(
        (degraded.state, degraded.diagnostic.as_deref()),
        (
            ResolutionState::Degraded,
            Some("working directory could not be resolved")
        )
    );
    assert!(!env.store.exists());
}

#[test]
fn resolve_degrades_on_a_re_pointed_root() {
    let env = setup();
    init(&env.projects, &env.work, "demo");
    let moved = env.tmp.join("moved");
    fs::rename(&env.work, &moved).unwrap();
    symlink(&moved, &env.work).unwrap();
    let resolution = env.projects.resolve(&moved, None, None).unwrap();
    assert_eq!(resolution.state, ResolutionState::Degraded);
    assert_eq!(
        resolution.diagnostic,
        Some(format!(
            "{}: registered root is no longer a canonical path",
            text(&env.work)
        ))
    );
    assert_eq!(resolution.project, None);
}

// --- the context a directory grants ---

#[test]
fn open_project_context_registered_names_the_project_and_its_root() {
    let env = setup();
    let service = env.service();
    let global = service.ensure_global().unwrap();
    let plan = service.plan_root(&env.work, None).unwrap();
    let project = service.init_project("demo", &plan).unwrap();
    let context = service.open_project_context(&env.dir("work/src")).unwrap();
    assert_eq!(context.state, ProjectContextState::Registered);
    assert_eq!(
        context.header,
        format!("project: demo [{}] (root {})", project.id, text(&env.work))
    );
    assert_eq!(
        context.read_write_set,
        ReadWriteSet {
            project_id: Some(project.id.clone()),
            global_project_id: Some(global),
        }
    );
    assert_eq!(context.project, Some(project));
    assert_eq!((context.diagnostic, context.session_key), (None, None));
}

#[test]
fn open_project_context_none_and_degraded_keep_global_readable_and_write_nothing() {
    let env = setup();
    let service = env.service();
    // no store at all
    let nothing = service.open_project_context(&env.work).unwrap();
    assert_eq!(
        (nothing.state, nothing.header.as_str()),
        (ProjectContextState::None, NONE_HEADER)
    );
    assert_eq!(
        nothing.read_write_set,
        ReadWriteSet {
            project_id: None,
            global_project_id: None,
        }
    );
    assert!(!env.store.exists());

    let global = service.ensure_global().unwrap();
    let no_project = ReadWriteSet {
        project_id: None,
        global_project_id: Some(global),
    };
    let none = service.open_project_context(&env.work).unwrap();
    assert_eq!(
        (none.state, none.header.as_str()),
        (ProjectContextState::None, NONE_HEADER)
    );
    assert_eq!(none.read_write_set, no_project);

    let degraded = service.open_project_context(&env.tmp.join("nope")).unwrap();
    assert_eq!(degraded.state, ProjectContextState::Degraded);
    assert_eq!(degraded.read_write_set, no_project);
    assert_eq!(
        degraded.header,
        "project: unavailable \u{2014} this directory could not be matched to one project \
         (working directory could not be resolved); ask the user to run memriver project explain"
    );
    assert_eq!(
        degraded.diagnostic.as_deref(),
        Some("working directory could not be resolved")
    );
    assert!(degraded.read_write_set.writable().is_empty());
}

#[test]
fn open_project_context_turns_a_storage_failure_into_an_unavailable_project_context() {
    let nothing = ReadWriteSet {
        project_id: None,
        global_project_id: None,
    };
    // the bindings cannot be read
    let env = setup();
    let service = env.service();
    service.ensure_global().unwrap();
    env.insert(
        &new_id(),
        "two\nlines",
        Some(text(&env.dir("elsewhere"))),
        false,
    );
    let context = service.open_project_context(&env.work).unwrap();
    assert_eq!(context.state, ProjectContextState::Unavailable);
    assert_eq!(context.header, STORE_UNREADABLE_HEADER);
    assert_eq!(context.read_write_set, nothing);

    // the directory resolves, the global project cannot be read
    let env = setup();
    let service = env.service();
    service.ensure_global().unwrap();
    init(&env.projects, &env.work, "demo");
    env.raw()
        .execute(
            "UPDATE projects SET name = 'two\nlines' WHERE is_global = 1",
            [],
        )
        .unwrap();
    let context = service.open_project_context(&env.work).unwrap();
    assert_eq!(context.state, ProjectContextState::Unavailable);
    assert_eq!(
        (context.read_write_set, context.project),
        (nothing.clone(), None)
    );

    // something that is no database stands where the store is
    let env = setup();
    fs::create_dir_all(env.store.join(DATABASE_FILENAME)).unwrap();
    let context = env.service().open_project_context(&env.work).unwrap();
    assert_eq!(context.state, ProjectContextState::Unavailable);
    assert_eq!(context.read_write_set, nothing);
}

#[test]
fn a_directory_bound_to_the_global_project_by_hand_grants_nothing() {
    // only a hand-edited store can say this; the global project is never a directory's
    let env = setup();
    let service = env.service();
    let global = service.ensure_global().unwrap();
    let raw = env.raw();
    raw.execute_batch("PRAGMA ignore_check_constraints = ON")
        .unwrap();
    raw.execute(
        "UPDATE projects SET root = ?1 WHERE is_global = 1",
        [text(&env.work)],
    )
    .unwrap();
    let context = service.open_project_context(&env.work).unwrap();
    assert_eq!(
        (context.state, context.header.as_str()),
        (ProjectContextState::None, NONE_HEADER)
    );
    assert_eq!(context.read_write_set.global_project_id, Some(global));
    assert_eq!(context.read_write_set.project_id, None);
}

#[test]
fn binding_calls_reach_the_store_through_the_service() {
    let env = setup();
    let service = env.service();
    let plan = service.plan_root(&env.work, None).unwrap();
    let project = service.init_project("demo", &plan).unwrap();
    let (unbind_plan, resolution) = service
        .plan_unbind(&project.id, text(&env.work), &env.work)
        .unwrap();
    assert_eq!(resolution.state, ResolutionState::None);
    service.unbind(&unbind_plan).unwrap();
    assert_eq!(service.read_project(&project.id).unwrap().root, None);
    let plan = service.plan_root(&env.work, Some(&project.id)).unwrap();
    service.adopt(&project.id, &plan).unwrap();
    assert_eq!(
        service.read_project(&project.id).unwrap().root.as_deref(),
        Some(text(&env.work))
    );
    // a refusal comes back as the store gave it
    assert_eq!(
        refused(service.plan_root(&env.work, None)),
        (BindingReason::BoundElsewhere, Some(project.id.clone()))
    );
    let Err(InitError::Store(err)) = service.init_project("again", &plan) else {
        panic!("created a second project on one directory");
    };
    assert_eq!(
        err,
        CoreError::BindingRefused {
            reason: BindingReason::BoundElsewhere,
            project_id: Some(project.id),
        }
    );
}

#[test]
fn a_store_of_an_older_schema_is_not_an_unreadable_store() {
    // the caller must learn the version: it answers with what the store needs, not with
    // "run memriver doctor"
    for version in [1, 3, 5] {
        let env = setup();
        let service = env.service();
        service.ensure_global().unwrap();
        init(&env.projects, &env.work, "demo");
        env.raw()
            .execute_batch(&format!("PRAGMA user_version = {version}"))
            .unwrap();
        let before = fs::read(env.store.join(DATABASE_FILENAME)).unwrap();
        assert_eq!(
            service.open_project_context(&env.work),
            Err(CoreError::StoreNeedsUpgrade(version))
        );
        assert_eq!(
            service.open_project_context(&env.tmp.join("nope")),
            Err(CoreError::StoreNeedsUpgrade(version))
        );
        assert_eq!(fs::read(env.store.join(DATABASE_FILENAME)).unwrap(), before);
    }
}
