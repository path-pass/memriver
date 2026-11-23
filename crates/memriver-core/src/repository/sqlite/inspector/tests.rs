//! What needs a moment inside an inspection, or a look at a directory that fails: a
//! peer's write while the store is read, and roots that cannot be told apart.

use std::fs;

use memriver_test_support::Sandbox;

use super::*;
use crate::models::project::Project;
use crate::repository::sqlite::project_store::ProjectStore;

struct World {
    _sandbox: Sandbox,
    tmp: PathBuf,
    store: PathBuf,
    projects: ProjectStore,
    work: Project,
}

fn world() -> World {
    let sandbox = Sandbox::new();
    let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
    let (home, store) = (tmp.join("home"), tmp.join("store"));
    fs::create_dir_all(&home).unwrap();
    let projects = ProjectStore::with_busy_timeout(&store, &home, 2000);
    projects.ensure_global().unwrap();
    let mut world = World {
        _sandbox: sandbox,
        tmp,
        store,
        projects,
        work: Project::new("placeholder", 120).unwrap(),
    };
    world.work = world.bound("work");
    world
}

impl World {
    fn bound(&self, name: &str) -> Project {
        let directory = self.tmp.join(name);
        fs::create_dir(&directory).unwrap();
        let project = Project::new(name, 120).unwrap();
        let plan = self.projects.plan_root(&directory, None).unwrap();
        self.projects.create(&project, &plan).unwrap();
        project
    }

    fn root(&self, name: &str) -> PathBuf {
        self.tmp.join(name)
    }

    fn inspect(&self) -> StoreReport {
        StoreInspector::with_busy_timeout(&self.store, 2000)
            .inspect()
            .unwrap()
    }

    /// What a peer with a short patience gets when it writes a memory now.
    fn peer(&self) -> impl FnOnce() -> bool + 'static {
        let (database, project) = (self.store.join(DATABASE_FILENAME), self.work.id.clone());
        move || {
            let conn = Connection::open(database).unwrap();
            conn.busy_timeout(Duration::from_millis(100)).unwrap();
            conn.execute(
                "INSERT INTO memories (id, project_id, type, source_harness, source_method, \
                 trust, sync, description, body, created, updated, version) VALUES \
                 ('mmmmmmmmmm', ?1, 'project', 't', 'agent', 'agent', 1, '', 'landed', \
                 '2026-09-24T00:00:00.000000Z', '2026-09-24T00:00:00.000000Z', 1)",
                [project],
            )
            .is_ok()
        }
    }
}

fn of_kind(report: &StoreReport, kind: &str) -> Vec<(String, Option<String>, String)> {
    report
        .findings
        .iter()
        .filter(|found| found.kind == kind)
        .map(|found| {
            (
                found.location_hint.clone(),
                found.project_id.clone(),
                found.reason.clone(),
            )
        })
        .collect()
}

fn about(project: &Project, reason: &str) -> (String, Option<String>, String) {
    (
        format!("projects/{}", project.id),
        Some(project.id.clone()),
        reason.to_string(),
    )
}

const UNVERIFIABLE: &str = "bound directory could not be checked";

/// A peer cannot commit between the inspector's queries: under the rollback journal a
/// writer needs every reader gone, so it is refused while the inspection runs, and the
/// report's counts and entries agree.
#[test]
fn one_inspection_reads_one_snapshot() {
    let world = world();
    let landed = std::rc::Rc::new(std::cell::Cell::new(None));
    let (peer, noted) = (world.peer(), landed.clone());
    MEANWHILE
        .with_borrow_mut(|planted| planted[0] = Some(Box::new(move || noted.set(Some(peer())))));
    let report = world.inspect();
    assert_eq!(landed.get(), Some(false));
    assert_eq!(
        (report.projects[0].active_memories, report.entries.len()),
        (0, 0)
    );
}

/// A hung mount under a directory check must not hold the read lock: a writer commits
/// meanwhile, and the report is still the snapshot read before.
#[test]
fn directory_checks_run_after_the_read_transaction() {
    let world = world();
    let landed = std::rc::Rc::new(std::cell::Cell::new(None));
    let (peer, noted) = (world.peer(), landed.clone());
    MEANWHILE
        .with_borrow_mut(|planted| planted[1] = Some(Box::new(move || noted.set(Some(peer())))));
    let report = world.inspect();
    assert_eq!(landed.get(), Some(true));
    assert_eq!(
        (report.projects[0].active_memories, report.entries.len()),
        (0, 0)
    );
    assert_eq!(report.projects[0].root_state, RootState::Ok);
    // the next inspection sees it: a row without a version
    assert_eq!(world.inspect().findings[0].kind, "version-gap");
}

/// The check that the database is a regular file and its opening are two looks at one
/// name: a link put there in between is not followed.
#[test]
fn a_database_swapped_for_a_link_after_its_check_is_not_inspected() {
    let world = world();
    // another store, with a project of its own
    let other = world.tmp.join("other-store");
    let theirs = ProjectStore::with_busy_timeout(&other, &world.tmp.join("home"), 2000);
    theirs.ensure_global().unwrap();
    let (mine, target) = (
        world.store.join(DATABASE_FILENAME),
        other.join(DATABASE_FILENAME),
    );
    let before = fs::read(&target).unwrap();
    database::before_open(move || {
        fs::rename(&mine, mine.with_extension("moved")).unwrap();
        std::os::unix::fs::symlink(&target, &mine).unwrap();
    });
    let inspector = StoreInspector::with_busy_timeout(&world.store, 2000);
    assert_eq!(inspector.inspect(), Err(CoreError::StorageFailure));
    assert_eq!(fs::read(other.join(DATABASE_FILENAME)).unwrap(), before);
    assert_eq!(fs::read_dir(&other).unwrap().count(), 1);
    // the link that now stands there is what the next inspection reports
    let report = inspector.inspect().unwrap();
    assert_eq!(report.findings[0].kind, "unsafe-database");
}

#[test]
fn two_projects_bound_to_one_directory_by_alias_is_a_finding() {
    let world = world();
    let other = world.bound("other");
    // a third project, bound by hand to the second one's directory in another case: one
    // directory where the file system ignores case, and counted as one where it does not
    let _ = fs::create_dir(world.root("OTHER"));
    let alias = Project::new("zz-alias", 120).unwrap();
    Connection::open(world.store.join(DATABASE_FILENAME))
        .unwrap()
        .execute(
            "INSERT INTO projects (id, name, root) VALUES (?1, ?2, ?3)",
            (
                &alias.id,
                &alias.name,
                world.root("OTHER").to_str().unwrap(),
            ),
        )
        .unwrap();
    directories::ignore_case();
    let report = world.inspect();
    // "other" sorts before "zz-alias": the pair is filed on the second
    assert_eq!(
        of_kind(&report, "root-conflict"),
        [about(
            &alias,
            "two projects are bound to the same directory under different spellings"
        )]
    );
    assert_eq!(report.findings.len(), 1);
    assert!(report.projects.iter().any(|project| project.id == other.id));
}

#[test]
fn an_already_unverifiable_root_is_not_paired_and_not_duplicated() {
    let world = world();
    let second = world.bound("second");
    world.bound("third");
    // every look at the second root fails: its own state says so once, and no pair
    // with it is looked at
    for follow in [false, true] {
        directories::fault(follow, &world.root("second"), libc::EACCES, false);
    }
    let report = world.inspect();
    assert_eq!(
        of_kind(&report, "unverifiable-root"),
        [about(&second, UNVERIFIABLE)]
    );
    assert_eq!(report.findings.len(), 1);
    let states: Vec<_> = report.projects.iter().map(|p| p.root_state).collect();
    assert_eq!(
        states,
        [
            RootState::Unverifiable,
            RootState::Ok,
            RootState::Ok,
            RootState::Unbound
        ]
    );
}

#[test]
fn an_unverifiable_pair_is_filed_once_per_project_not_duplicated() {
    let world = world();
    let (second, third) = (world.bound("second"), world.bound("third"));
    let mut projects = world.inspect().projects;
    projects.pop(); // global is bound nowhere
    // each root was fine on its own; now every comparison of two fails. All three
    // pairs fail, each project is in two of them, and each is filed once
    for name in ["work", "second", "third"] {
        directories::fault(true, &world.root(name), libc::EIO, false);
    }
    let mut findings = Vec::new();
    root_pairs(&projects, &mut findings);
    let report = StoreReport {
        initialized: true,
        entries: Vec::new(),
        projects,
        findings: sorted(findings),
        incomplete_changes: Vec::new(),
    };
    let mut expected = [&world.work, &second, &third].map(|p| about(p, UNVERIFIABLE));
    expected.sort();
    assert_eq!(of_kind(&report, "unverifiable-root"), expected);
    assert_eq!(report.findings.len(), 3);
}

#[test]
fn ids_that_are_text_and_ids_that_are_not_are_ordered_as_python_writes_them() {
    let text = |text: &str| Raw::Text(text.to_string());
    let bytes = |bytes: &[u8]| Raw::Bytes(bytes.to_vec());
    assert_eq!(written(&text("it's")), "\"it's\"");
    assert_eq!(written(&bytes(b"\x80ab\n'\\")), "b\"\\x80ab\\n'\\\\\"");
    assert_eq!(written(&bytes(b"a'\"")), "b'a\\'\"'");
    let mut ids = [bytes(b"\x80"), text("b"), bytes(b"a"), text("a")];
    ids.sort_by_cached_key(written);
    // 'a' 'b' b'\x80' b'a'
    assert_eq!(ids, [text("a"), text("b"), bytes(b"\x80"), bytes(b"a")]);
}

#[test]
fn a_walk_that_comes_back_is_a_cycle_and_one_that_only_reaches_one_is_not() {
    let id = |name: &str| Raw::Text(name.to_string());
    let mut edges: HashMap<Raw, HashSet<Raw>> = HashMap::new();
    for (from, to) in [
        ("a", "b"),
        ("b", "c"),
        ("c", "a"),
        ("d", "a"),
        ("e", "e"),
        ("b", "x"),
    ] {
        edges.entry(id(from)).or_default().insert(id(to));
    }
    let mut looped: Vec<_> = on_cycles(&edges).into_iter().cloned().collect();
    looped.sort();
    assert_eq!(looped, [id("a"), id("b"), id("c"), id("e")]);
}
