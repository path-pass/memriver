//! Directory rules: one directory under two spellings, which bound root a directory lies
//! under, and what a root that moved, vanished or cannot be looked at does to the answer.

use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};

use memriver_core::repository::directories::{
    Match, RootState, UNRESOLVABLE_START, canonical, covers, integrity_diagnostic, nearest_bound,
    root_state, same_directory, transcript_moved, usable_directory,
};
use memriver_test_support::Sandbox;

const A: &str = "aaaaaaaaaa";
const B: &str = "bbbbbbbbbb";

/// A sandbox and its directory as the file system spells it.
fn base() -> (Sandbox, PathBuf) {
    let sandbox = Sandbox::new();
    let base = fs::canonicalize(sandbox.dir.path()).unwrap();
    (sandbox, base)
}

fn bound(pairs: &[(&str, &Path)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(id, root)| (id.to_string(), root.to_str().unwrap().to_string()))
        .collect()
}

fn registered(id: &str) -> Match {
    Match::Registered(id.to_string())
}

fn text(path: &Path) -> &str {
    path.to_str().unwrap()
}

/// Whether two names that differ only in case are one directory here.
fn case_insensitive(base: &Path) -> bool {
    fs::create_dir(base.join("CaseProbe")).unwrap();
    base.join("caseprobe").exists()
}

/// A directory nothing below can be looked at through, until the guard is dropped.
struct Locked(PathBuf);

impl Locked {
    fn new(path: &Path) -> Option<Locked> {
        fs::set_permissions(path, fs::Permissions::from_mode(0o000)).unwrap();
        let locked = Locked(path.to_path_buf());
        // someone no permission stops (root) cannot run these cases
        fs::metadata(path.join("x"))
            .is_err_and(|err| err.kind() == std::io::ErrorKind::PermissionDenied)
            .then_some(locked)
    }
}

impl Drop for Locked {
    fn drop(&mut self) {
        let _ = fs::set_permissions(&self.0, fs::Permissions::from_mode(0o700));
    }
}

#[test]
fn canonical_is_strict_and_never_fails() {
    let (_sandbox, base) = base();
    fs::create_dir(base.join("d")).unwrap();
    fs::write(base.join("f"), "x").unwrap();
    symlink(base.join("d"), base.join("link")).unwrap();
    symlink(base.join("loop"), base.join("loop")).unwrap();
    assert_eq!(canonical(&base.join("link")), Some(base.join("d")));
    assert_eq!(canonical(&base.join("d/../link/.")), Some(base.join("d")));
    for bad in ["f", "missing", "loop", "f/below", "x\0y"] {
        assert_eq!(canonical(&base.join(bad)), None, "{bad:?}");
    }
    // as Python's Path(""): the working directory
    assert_eq!(
        canonical(Path::new("")),
        Some(fs::canonicalize(".").unwrap())
    );
}

#[test]
fn usable_directory_is_an_absolute_path_without_nul() {
    assert!(usable_directory("/a/b") && usable_directory("/"));
    for bad in ["", "rel", "a/b", "/a\0b"] {
        assert!(!usable_directory(bad), "{bad:?}");
    }
}

#[test]
fn transcript_moved_compares_real_parent_directories() {
    let (_sandbox, base) = base();
    fs::create_dir_all(base.join("projects/x")).unwrap();
    symlink(base.join("projects"), base.join("alias")).unwrap();
    let stored = base.join("projects/x/s.jsonl");
    assert!(transcript_moved("/t/a/s.jsonl", "/t/b/s.jsonl"));
    assert!(!transcript_moved("/t/a/s.jsonl", "/t/a/other.jsonl"));
    for spelling in [
        "alias/x/s.jsonl",
        "projects/./x/s.jsonl",
        "projects/x/../x/s.jsonl",
    ] {
        assert!(
            !transcript_moved(text(&stored), &format!("{}/{spelling}", text(&base))),
            "{spelling}"
        );
    }
    for bad in ["", "rel/s.jsonl", "/a\0/s.jsonl"] {
        assert!(!transcript_moved(bad, "/t/b/s.jsonl"), "{bad:?}");
        assert!(!transcript_moved("/t/a/s.jsonl", bad), "{bad:?}");
    }
    // a file directly below the root has the root as its directory
    assert!(!transcript_moved("/s.jsonl", "/other.jsonl"));
    assert!(transcript_moved("/s.jsonl", "/t/s.jsonl"));
}

#[test]
fn nearest_bound_prefers_the_deepest_root() {
    let (_sandbox, base) = base();
    let parent = base.join("work");
    let child = parent.join("frontend");
    fs::create_dir_all(child.join("src")).unwrap();
    fs::create_dir(parent.join("backend")).unwrap();
    let bound = bound(&[(A, &parent), (B, &child)]);
    assert_eq!(
        nearest_bound(&child.join("src"), &bound, None),
        registered(B)
    );
    assert_eq!(
        nearest_bound(&parent.join("backend"), &bound, None),
        registered(A)
    );
    assert_eq!(nearest_bound(&parent, &bound, None), registered(A));
}

#[test]
fn an_unregistered_directory_is_none() {
    let (_sandbox, base) = base();
    assert_eq!(nearest_bound(&base, &[], None), Match::None);
    assert_eq!(
        nearest_bound(&base, &bound(&[(A, &base.join("elsewhere"))]), None),
        Match::None
    );
}

#[test]
fn on_a_file_system_that_ignores_case_a_case_alias_of_a_root_is_that_root() {
    let (_sandbox, base) = base();
    if !case_insensitive(&base) {
        return;
    }
    let real = base.join("Work");
    fs::create_dir(&real).unwrap();
    let alias = base.join("work");
    assert_eq!(
        nearest_bound(&real, &bound(&[(A, &alias)]), None),
        registered(A)
    );
    // the nearer alias beats the farther exact match
    let child = real.join("Child");
    fs::create_dir(&child).unwrap();
    let both = bound(&[(A, &real), (B, &real.join("child"))]);
    assert_eq!(nearest_bound(&child, &both, None), registered(B));
    // an exact root and an alias of two projects at one level
    let Match::Degraded(diagnostic) =
        nearest_bound(&real, &bound(&[(A, &real), (B, &alias)]), None)
    else {
        panic!("two projects at one level must degrade");
    };
    assert_eq!(
        diagnostic,
        format!("{}: matched by more than one project", text(&real))
    );
}

#[test]
fn two_spellings_of_one_directory_are_the_same_root() {
    let (_sandbox, base) = base();
    fs::create_dir_all(base.join("Home/x")).unwrap();
    fs::create_dir(base.join("Other")).unwrap();
    symlink(base.join("Home"), base.join("alias")).unwrap();
    assert_eq!(
        same_directory(&base.join("Home"), &base.join("alias")),
        Some(true)
    );
    assert_eq!(
        same_directory(&base.join("Home"), &base.join("Home/x/..")),
        Some(true)
    );
    assert_eq!(
        same_directory(&base.join("Home"), &base.join("Other")),
        Some(false)
    );
    // absent, or not addressable at all: not this directory
    assert_eq!(
        same_directory(&base.join("Home"), &base.join("gone")),
        Some(false)
    );
    assert_eq!(
        same_directory(&base.join("gone"), &base.join("Home")),
        Some(false)
    );
    assert_eq!(
        same_directory(&base.join("Home"), Path::new("/x\0y")),
        Some(false)
    );
    // covers: the directory itself or an ancestor, by text or by identity
    assert_eq!(covers(&base.join("Home"), &base.join("Home/x")), Some(true));
    assert_eq!(
        covers(&base.join("alias"), &base.join("Home/x")),
        Some(true)
    );
    assert_eq!(
        covers(&base.join("Home/x"), &base.join("Home")),
        Some(false)
    );
    assert_eq!(covers(Path::new("/"), &base), Some(true));
    assert_eq!(covers(Path::new("/x\0y"), &base), Some(false));
    // by text alone when nothing is there to compare
    assert_eq!(
        covers(&base.join("gone"), &base.join("gone/deeper")),
        Some(true)
    );
}

#[test]
fn uncheckable_is_never_different() {
    let (_sandbox, base) = base();
    let main = base.join("main");
    fs::create_dir_all(main.join("locked/x")).unwrap();
    fs::create_dir_all(main.join("sub")).unwrap();
    fs::create_dir(base.join("wt")).unwrap();
    let Some(_locked) = Locked::new(&main.join("locked")) else {
        return;
    };
    let hidden = main.join("locked/x");
    assert_eq!(same_directory(&main, &hidden), None);
    assert_eq!(same_directory(&hidden, &main), None);
    // the first path decides: an absent one is "not this directory" before the second
    // is looked at
    assert_eq!(same_directory(&base.join("gone"), &hidden), Some(false));
    assert_eq!(covers(&main.join("sub"), &hidden), None);
    // a walk that meets a level it cannot check never falls through to the project above
    let both = bound(&[(A, &main), (B, &main.join("sub"))]);
    assert_eq!(
        nearest_bound(&base.join("wt"), &both, Some(&hidden.join("y"))),
        Match::Degraded(format!(
            "{}: registered root could not be checked",
            text(&main)
        ))
    );
    // a root that cannot be looked at degrades every directory
    assert_eq!(root_state(text(&hidden)), RootState::Unverifiable);
    assert_eq!(
        integrity_diagnostic(&[text(&main), text(&hidden)]),
        Some(format!(
            "{}: registered root could not be checked",
            text(&hidden)
        ))
    );
    assert_eq!(
        nearest_bound(&base, &bound(&[(A, &hidden)]), None),
        Match::Degraded(format!(
            "{}: registered root could not be checked",
            text(&hidden)
        ))
    );
}

#[test]
fn missing_root_does_not_degrade() {
    let (_sandbox, base) = base();
    let gone = base.join("gone");
    let bound = bound(&[(A, &gone)]);
    assert_eq!(nearest_bound(&base, &bound, None), Match::None);
    assert_eq!(root_state(text(&gone)), RootState::Missing);
    assert_eq!(root_state(text(&gone.join("deeper"))), RootState::Missing);
    assert_eq!(integrity_diagnostic(&[text(&gone.join("deeper"))]), None);
    // back again, it is matched by its text
    fs::create_dir(&gone).unwrap();
    assert_eq!(nearest_bound(&gone, &bound, None), registered(A));
}

#[test]
fn one_repointed_root_degrades_every_directory() {
    let (_sandbox, base) = base();
    let (old, new, other) = (base.join("old"), base.join("new"), base.join("other"));
    fs::create_dir(&old).unwrap();
    fs::create_dir(&other).unwrap();
    let bound = bound(&[(A, &old), (B, &other)]);
    assert_eq!(nearest_bound(&old, &bound, None), registered(A));
    fs::remove_dir(&old).unwrap();
    fs::create_dir(&new).unwrap();
    symlink(&new, &old).unwrap();
    let degraded = Match::Degraded(format!(
        "{}: registered root is no longer a canonical path",
        text(&old)
    ));
    // the link's target is not authorized, and neither is any other project's directory
    for start in [&new, &old, &other, &base] {
        assert_eq!(nearest_bound(start, &bound, None), degraded, "{start:?}");
    }
}

#[test]
fn a_root_below_a_replaced_ancestor_is_repointed_not_offline() {
    let (_sandbox, base) = base();
    let (x, y) = (base.join("x"), base.join("y"));
    fs::create_dir_all(x.join("work")).unwrap();
    fs::rename(&x, &y).unwrap();
    symlink(&y, &x).unwrap();
    let root = x.join("work");
    let diagnostic = format!(
        "{}: registered root is no longer a canonical path",
        text(&root)
    );
    assert_eq!(
        nearest_bound(&y.join("work"), &bound(&[(A, &root)]), None),
        Match::Degraded(diagnostic.clone())
    );
    assert_eq!(
        integrity_diagnostic(&[text(&root)]),
        Some(diagnostic.clone())
    );
    // the same with the leaf gone: the nearest thing that exists is what was replaced
    fs::remove_dir(y.join("work")).unwrap();
    assert_eq!(root_state(text(&root)), RootState::NotCanonical);
    assert_eq!(integrity_diagnostic(&[text(&root)]), Some(diagnostic));
    // a link that leads nowhere is a replacement too
    symlink(base.join("nowhere"), base.join("dangling")).unwrap();
    assert_eq!(
        root_state(text(&base.join("dangling"))),
        RootState::NotCanonical
    );
}

#[test]
fn a_root_the_system_cannot_address_is_unverifiable() {
    assert_eq!(root_state("/x\0y"), RootState::Unverifiable);
    assert_eq!(
        integrity_diagnostic(&["/x\0y"]),
        Some("/x\0y: registered root could not be checked".to_string())
    );
}

#[test]
fn root_state_of_a_healthy_and_of_a_file_root() {
    let (_sandbox, base) = base();
    fs::create_dir(base.join("d")).unwrap();
    fs::write(base.join("f"), "x").unwrap();
    assert_eq!(root_state(text(&base.join("d"))), RootState::Ok);
    assert_eq!(root_state(text(&base.join("f"))), RootState::Missing);
    // below a file nothing can be looked at: that is not "offline"
    assert_eq!(
        root_state(text(&base.join("f/below"))),
        RootState::Unverifiable
    );
    assert_eq!(root_state("/"), RootState::Ok);
}

#[test]
fn a_start_that_is_a_file_missing_or_unaddressable_is_degraded() {
    let (_sandbox, base) = base();
    fs::write(base.join("file.txt"), "x").unwrap();
    for start in ["file.txt", "does-not-exist", "SENTINEL\0cwd"] {
        assert_eq!(
            nearest_bound(&base.join(start), &[], None),
            Match::Degraded(UNRESOLVABLE_START.to_string()),
            "{start:?}"
        );
    }
    assert_eq!(
        UNRESOLVABLE_START,
        "working directory could not be resolved"
    );
}

#[test]
fn a_start_written_through_a_link_is_walked_where_it_points() {
    let (_sandbox, base) = base();
    fs::create_dir_all(base.join("work/src")).unwrap();
    fs::create_dir(base.join("far")).unwrap();
    symlink(base.join("work/src"), base.join("far/alias")).unwrap();
    let bound = bound(&[(A, &base.join("work")), (B, &base.join("far"))]);
    assert_eq!(
        nearest_bound(&base.join("far/alias"), &bound, None),
        registered(A)
    );
}

#[test]
fn a_logical_path_is_walked_instead_of_the_start() {
    let (_sandbox, base) = base();
    let (main, worktree) = (base.join("main"), base.join("wt"));
    fs::create_dir_all(main.join("sub")).unwrap();
    fs::create_dir(&worktree).unwrap();
    // an ancestor of a path that does not exist
    let one = bound(&[(A, &main)]);
    assert_eq!(
        nearest_bound(&worktree, &one, Some(&main.join("newdir"))),
        registered(A)
    );
    assert_eq!(nearest_bound(&worktree, &one, None), Match::None);
    // the nearest bound ancestor of the logical path
    let two = bound(&[(A, &main), (B, &main.join("sub"))]);
    assert_eq!(
        nearest_bound(&worktree, &two, Some(&main.join("sub/x"))),
        registered(B)
    );
    // the start must still be a real directory
    assert_eq!(
        nearest_bound(
            &base.join("gone"),
            &bound(&[(A, &base)]),
            Some(&base.join("x"))
        ),
        Match::Degraded(UNRESOLVABLE_START.to_string())
    );
}
