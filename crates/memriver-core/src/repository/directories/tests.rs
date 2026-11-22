//! What needs a failure planted in a look at a name, or a change made at one exact
//! moment of a purge. Everything else is tested from outside, in `tests/`.

use std::os::unix::fs::symlink;

use memriver_test_support::Sandbox;

use super::*;

fn base() -> (Sandbox, PathBuf) {
    let sandbox = Sandbox::new();
    let base = realpath(sandbox.dir.path(), true).unwrap();
    (sandbox, base)
}

/// Home, working directory and a store with a database and a sub-directory.
fn places(base: &Path) -> (PathBuf, PathBuf, PathBuf) {
    let (home, cwd, store) = (base.join("home"), base.join("cwd"), base.join("store"));
    for directory in [&home, &cwd, &store.join("sub")] {
        fs::create_dir_all(directory).unwrap();
    }
    fs::write(store.join(DATABASE_FILENAME), "x").unwrap();
    fs::write(store.join("sub/f"), "y").unwrap();
    (home, cwd, store)
}

/// Every look of that kind at `path` fails from now on, or only the next one.
fn plant(follow: bool, path: &Path, errno: i32, once: bool) {
    FAULTS.with_borrow_mut(|faults| faults.push((follow, path.to_path_buf(), errno, once)));
}

/// The directory at `path` counts as lying on another device, or on another mount of the
/// same one, from now on.
fn elsewhere(path: &Path, how: Elsewhere) {
    MOUNTS.with_borrow_mut(|mounts| mounts.push((path.to_path_buf(), how)));
}

#[test]
fn a_failure_is_shown_as_python_shows_an_os_error() {
    let failure = OsFailure {
        errno: Some(libc::EACCES),
        path: PathBuf::from("/it's/here"),
    };
    assert_eq!(
        failure.to_string(),
        format!("[Errno {}] Permission denied: \"/it's/here\"", libc::EACCES)
    );
    assert_eq!(failure.reason(), "Permission denied");
    let missing = lstat(Path::new("/definitely/not/there")).unwrap_err();
    assert_eq!(
        missing.to_string(),
        format!(
            "[Errno {}] No such file or directory: '/definitely/not/there'",
            libc::ENOENT
        )
    );
    assert!(missing.is_absent() && missing.is_missing());
    let unaddressable = stat(Path::new("/x\0y")).unwrap_err();
    assert_eq!(unaddressable.errno, None);
    assert_eq!(unaddressable.to_string(), "embedded null byte");
}

#[test]
fn realpath_reads_every_link_and_takes_missing_names_as_written() {
    let (_sandbox, base) = base();
    fs::create_dir_all(base.join("real/sub")).unwrap();
    fs::write(base.join("file"), "x").unwrap();
    symlink("real", base.join("rel")).unwrap();
    symlink(base.join("real/sub"), base.join("abs")).unwrap();
    symlink("../rel/sub", base.join("real/chain")).unwrap();
    symlink("loop", base.join("loop")).unwrap();
    symlink("b", base.join("a")).unwrap();
    symlink("a", base.join("b")).unwrap();
    symlink("nowhere/else", base.join("dangling")).unwrap();
    for (written, real) in [
        ("real", "real"),
        ("rel", "real"),
        ("rel/sub", "real/sub"),
        ("abs", "real/sub"),
        ("abs/..", "real"),
        ("real/chain", "real/sub"),
        ("real/chain/../chain/.//", "real/sub"),
        ("rel/../rel/./sub/", "real/sub"),
    ] {
        for strict in [true, false] {
            assert_eq!(
                realpath(&base.join(written), strict),
                Ok(base.join(real)),
                "{written} {strict}"
            );
        }
    }
    // not there, below a file, a loop: failures when strict, names as written otherwise
    for (written, errno, loose) in [
        ("missing/x", libc::ENOENT, "missing/x"),
        ("rel/missing", libc::ENOENT, "real/missing"),
        ("missing/../rel", libc::ENOENT, "real"),
        ("file/below", libc::ENOTDIR, "file/below"),
        ("file/below/../../rel", libc::ENOTDIR, "real"),
        ("loop", libc::ELOOP, "loop"),
        ("loop/x/../y", libc::ELOOP, "loop/y"),
        ("a", libc::ELOOP, "a"),
        ("dangling", libc::ENOENT, "nowhere/else"),
        ("dangling/../x", libc::ENOENT, "nowhere/x"),
    ] {
        let failure = realpath(&base.join(written), true).unwrap_err();
        assert_eq!(failure.errno, Some(errno), "{written}");
        assert_eq!(
            realpath(&base.join(written), false),
            Ok(base.join(loose)),
            "{written}"
        );
    }
    // nothing lies below what is no directory, whatever is written after it: answers
    // recorded from Python 3.13 (strict: the component the failure names; else the path)
    symlink("file", base.join("linkfile")).unwrap();
    symlink("linkfile", base.join("link2")).unwrap();
    for (written, named, loose) in [
        ("file/", "file", "file"),
        ("file/./", "file", "file"),
        ("linkfile/", "file", "file"),
        ("linkfile/..", "file", ""),
        ("linkfile/x", "file", "file/x"),
        ("file/..", "file", ""),
        ("file/.", "file", "file"),
        ("file//", "file", "file"),
        ("file/below", "file", "file/below"),
        ("link2/", "file", "file"),
        ("rel/../file/", "file", "file"),
    ] {
        let failure = realpath(&base.join(written), true).unwrap_err();
        assert_eq!(
            (failure.errno, &failure.path),
            (Some(libc::ENOTDIR), &base.join(named)),
            "{written}"
        );
        let loose = match loose {
            "" => base.clone(),
            loose => base.join(loose),
        };
        assert_eq!(realpath(&base.join(written), false), Ok(loose), "{written}");
    }
    for (written, real) in [
        ("file", "file"),
        ("linkfile", "file"),
        ("link2", "file"),
        ("rel/", "real"),
        ("rel/.", "real"),
        ("real/sub/", "real/sub"),
    ] {
        assert_eq!(
            realpath(&base.join(written), true),
            Ok(base.join(real)),
            "{written}"
        );
    }
    // so a start written through a file is no directory, where its text alone names one
    assert_eq!(canonical(&base.join("linkfile/..")), None);
    assert_eq!(canonical(&base.join("file/..")), None);
    assert_eq!(
        canonical(&base.join("rel/../real/sub/.")),
        Some(base.join("real/sub"))
    );
    assert_eq!(
        realpath(&base.join("rel/missing/x"), true)
            .unwrap_err()
            .path,
        base.join("real/missing")
    );
    // above the root there is only the root, and a relative path starts where we stand
    assert_eq!(realpath(Path::new("/../.."), true), Ok(PathBuf::from("/")));
    let here = realpath(&std::env::current_dir().unwrap(), true).unwrap();
    assert_eq!(realpath(Path::new(""), true), Ok(here.clone()));
    assert_eq!(realpath(Path::new("./nothing-here/.."), false), Ok(here));
    assert_eq!(realpath(Path::new("/x\0y"), false).unwrap_err().errno, None);
}

#[test]
fn a_look_that_fails_once_is_taken_as_written_only_where_nothing_must_be_proved() {
    let (_sandbox, base) = base();
    fs::create_dir(base.join("real")).unwrap();
    symlink("real", base.join("link")).unwrap();
    plant(false, &base.join("link"), libc::EIO, true);
    assert_eq!(realpath(&base.join("link"), false), Ok(base.join("link")));
    plant(false, &base.join("link"), libc::EIO, true);
    assert_eq!(
        realpath(&base.join("link"), true).unwrap_err().errno,
        Some(libc::EIO)
    );
    assert_eq!(realpath(&base.join("link"), true), Ok(base.join("real")));
    // a root whose own look fails is not offline
    plant(false, &base.join("real"), libc::EACCES, false);
    assert_eq!(
        root_state(base.join("real").to_str().unwrap()),
        RootState::Unverifiable
    );
}

#[test]
fn a_target_whose_existence_cannot_be_checked_is_refused() {
    // not a plan with nothing to remove: the store may well be there
    let (_sandbox, base) = base();
    let (home, cwd, store) = places(&base);
    plant(true, &store, libc::EACCES, false);
    let refusal = plan_purge(&store, &home, &cwd, false).unwrap_err();
    assert_eq!(
        (refusal.kind, &refusal.path, &refusal.canonical),
        (PurgeRefusalKind::Unresolvable, &store, &Some(store.clone()))
    );
    assert_eq!(
        refusal.detail,
        format!(
            "[Errno {}] Permission denied: '{}'",
            libc::EACCES,
            store.display()
        )
    );
    assert!(store.join(DATABASE_FILENAME).exists());
}

#[test]
fn a_target_whose_link_check_fails_after_confirmation_is_left_alone() {
    // a check that cannot run proves nothing and must not read as "no link"
    let (_sandbox, base) = base();
    let (home, cwd, store) = places(&base);
    let plan = plan_purge(&store, &home, &cwd, false).unwrap();
    plant(false, &store, libc::EACCES, false);
    let result = purge(plan);
    assert_eq!(result.outcome, PurgeOutcome::Refused);
    let refusal = result.refusal.unwrap();
    assert_eq!(
        (refusal.kind, &refusal.path),
        (PurgeRefusalKind::Unresolvable, &store)
    );
    assert!(store.join(DATABASE_FILENAME).exists());
}

#[test]
fn a_target_whose_link_check_fails_is_not_planned() {
    let (_sandbox, base) = base();
    let (home, cwd, store) = places(&base);
    // the walk to the target takes the name as written; the look at the leaf must not
    fs::create_dir(base.join("elsewhere")).unwrap();
    let given = base.join("elsewhere/../store");
    plant(false, &given, libc::EIO, false);
    let refusal = plan_purge(&given, &home, &cwd, false).unwrap_err();
    assert_eq!(
        (refusal.kind, &refusal.path),
        (PurgeRefusalKind::Unresolvable, &given)
    );
    assert_eq!(refusal.canonical, Some(store));
}

#[test]
fn a_base_resolved_after_a_one_off_error_is_checked_where_it_really_is() {
    // the fallback reads a link it could not look at as a plain component; the strict
    // second look that then succeeds is where the base really is, and that is what the
    // store must not hold
    for moved in ["home", "cwd"] {
        let (_sandbox, base) = base();
        let (home, cwd, store) = places(&base);
        fs::create_dir(base.join("outside")).unwrap();
        fs::create_dir(store.join("protected")).unwrap();
        fs::write(store.join("protected/keep"), "k").unwrap();
        let link = base.join("link");
        symlink(base.join("outside"), &link).unwrap();
        let behind = base.join("missing/../link");
        let (home, cwd) = match moved {
            "home" => (behind, cwd),
            _ => (home, behind),
        };
        let plan = plan_purge(&store, &home, &cwd, false).unwrap();
        fs::remove_file(&link).unwrap();
        symlink(store.join("protected"), &link).unwrap();
        plant(false, &link, libc::EIO, true);
        let result = purge(plan);
        assert!(
            FAULTS.with_borrow(|faults| faults.is_empty()),
            "the error was hit"
        );
        assert_eq!(result.outcome, PurgeOutcome::Refused, "{moved}");
        assert_eq!(result.refusal.unwrap().kind, PurgeRefusalKind::TooBroad);
        assert!(store.join("protected/keep").exists());
    }
}

#[test]
fn a_directory_swapped_in_before_the_open_is_refused_not_purged() {
    // checking the names by path would run before the open -- a directory swapped in
    // between passes unchecked. The check has to run on the object the open returns
    let (_sandbox, base) = base();
    let (home, cwd, store) = places(&base);
    let moved = base.join("moved-away");
    let refusal = plan_purge_with(&store, &home, &cwd, false, &mut || {
        fs::rename(&store, &moved).unwrap();
        fs::create_dir(&store).unwrap();
        fs::write(store.join("decoy.txt"), "not memriver's").unwrap();
    })
    .unwrap_err();
    assert_eq!(refusal.kind, PurgeRefusalKind::NotAStore);
    assert_eq!(
        fs::read_to_string(store.join("decoy.txt")).unwrap(),
        "not memriver's"
    );
    assert!(moved.join(DATABASE_FILENAME).exists());
}

#[test]
fn a_link_swapped_in_before_the_open_is_not_followed() {
    let (_sandbox, base) = base();
    let (home, cwd, _) = places(&base);
    let store = base.join("deep/store");
    fs::create_dir_all(&store).unwrap();
    fs::write(store.join(DATABASE_FILENAME), "x").unwrap();
    // the leaf, then an ancestor
    for swapped in [store.clone(), base.join("deep")] {
        let aside = base.join("aside");
        let refusal = plan_purge_with(&store, &home, &cwd, false, &mut || {
            fs::rename(&swapped, &aside).unwrap();
            symlink(&aside, &swapped).unwrap();
        })
        .unwrap_err();
        assert_eq!(refusal.kind, PurgeRefusalKind::Unopenable, "{swapped:?}");
        assert!(!refusal.detail.is_empty() && !refusal.detail.contains("os error"));
        fs::remove_file(&swapped).unwrap();
        fs::rename(&aside, &swapped).unwrap();
        assert!(store.join(DATABASE_FILENAME).exists());
    }
}

#[test]
fn a_root_that_changes_hands_while_it_is_emptied_is_left_standing() {
    let (_sandbox, base) = base();
    let (home, cwd, store) = places(&base);
    let moved = base.join("moved-away");
    let plan = plan_purge(&store, &home, &cwd, false).unwrap();
    let result = purge_with(plan, &mut |emptied| {
        if emptied == store {
            fs::rename(&store, &moved).unwrap();
            fs::create_dir(&store).unwrap();
            fs::write(store.join("stranger"), "s").unwrap();
        }
    });
    assert_eq!(
        (result.outcome, &result.path, result.replaced.len()),
        (PurgeOutcome::ReplacedRoot, &store, 0)
    );
    // what the user confirmed was emptied; what stands at the name now was not touched
    assert_eq!(fs::read_dir(&moved).unwrap().count(), 0);
    assert!(store.join("stranger").exists());
}

#[test]
fn a_child_that_changes_hands_while_it_is_emptied_is_named_and_left() {
    let (_sandbox, base) = base();
    let (home, cwd, store) = places(&base);
    let (child, moved) = (store.join("sub"), base.join("moved-away"));
    let plan = plan_purge(&store, &home, &cwd, false).unwrap();
    let result = purge_with(plan, &mut |emptied| {
        if emptied == child {
            fs::rename(&child, &moved).unwrap();
            fs::create_dir(&child).unwrap();
            fs::write(child.join("stranger"), "s").unwrap();
        }
    });
    // the stranger keeps the store from being removed, and is named
    assert_eq!(result.outcome, PurgeOutcome::PartlyRemoved);
    assert_eq!(result.replaced, std::slice::from_ref(&child));
    assert_eq!(result.detail, "Directory not empty");
    assert!(child.join("stranger").exists());
    assert!(!store.join(DATABASE_FILENAME).exists());
}

// --- one directory under two spellings, on any file system ---

/// Two directories whose names differ only in case, made to count as one.
fn alias(base: &Path, real: &str, other: &str) -> (PathBuf, PathBuf) {
    for name in [real, other] {
        fs::create_dir_all(base.join(name)).unwrap();
    }
    ignore_case();
    (base.join(real), base.join(other))
}

fn pairs(bound: &[(&str, &Path)]) -> Vec<(String, String)> {
    bound
        .iter()
        .map(|(id, root)| (id.to_string(), root.to_str().unwrap().to_string()))
        .collect()
}

#[test]
fn a_root_under_another_spelling_is_matched_by_identity() {
    let (_sandbox, base) = base();
    let (real, other) = alias(&base, "Work", "work");
    assert_eq!(same_directory(&real, &other), Some(true));
    assert_eq!(covers(&other, &real.join("x")), Some(true));
    assert_eq!(
        nearest_bound(&real, &pairs(&[("aaaaaaaaaa", &other)]), None),
        Match::Registered("aaaaaaaaaa".to_string())
    );
    // without the pretence the two are different directories, or one: either way the
    // answer above did not come from the text
    assert_ne!(real.as_os_str(), other.as_os_str());
}

#[test]
fn a_nearer_alias_beats_a_farther_exact_match() {
    let (_sandbox, base) = base();
    let parent = base.join("work");
    let (child, other) = alias(&base, "work/Child", "work/child");
    let bound = pairs(&[("aaaaaaaaaa", &parent), ("bbbbbbbbbb", &other)]);
    assert_eq!(
        nearest_bound(&child, &bound, None),
        Match::Registered("bbbbbbbbbb".to_string())
    );
}

#[test]
fn an_exact_root_and_an_alias_of_two_projects_at_one_level_is_degraded() {
    let (_sandbox, base) = base();
    let (real, other) = alias(&base, "Work", "work");
    let bound = pairs(&[("aaaaaaaaaa", &real), ("bbbbbbbbbb", &other)]);
    assert_eq!(
        nearest_bound(&real, &bound, None),
        Match::Degraded(format!(
            "{}: matched by more than one project",
            real.display()
        ))
    );
    // the same project under both spellings is one project
    let twice = pairs(&[("aaaaaaaaaa", &real), ("aaaaaaaaaa", &other)]);
    assert_eq!(
        nearest_bound(&real, &twice, None),
        Match::Registered("aaaaaaaaaa".to_string())
    );
}

// --- a purge target that is a protected directory under another spelling ---

#[test]
fn a_target_that_is_home_or_the_working_directory_under_another_spelling_is_too_broad() {
    // the names say "neither is below the other"; the directories say otherwise
    for protected in ["home", "cwd"] {
        for below in ["", "deep/er"] {
            for dry_run in [true, false] {
                let (_sandbox, base) = base();
                let (home, cwd, _) = places(&base);
                let (real, target) = alias(&base, "Kept", "kept");
                fs::create_dir_all(real.join(below)).unwrap();
                fs::write(target.join(DATABASE_FILENAME), "x").unwrap();
                let (home, cwd) = match protected {
                    "home" => (real.join(below), cwd),
                    _ => (home, real.join(below)),
                };
                let refusal = plan_purge(&target, &home, &cwd, dry_run).unwrap_err();
                assert_eq!(
                    (refusal.kind, &refusal.path, &refusal.canonical),
                    (PurgeRefusalKind::TooBroad, &target, &Some(target.clone())),
                    "{protected} {below:?} {dry_run}"
                );
            }
        }
    }
}

#[test]
fn a_protected_base_moved_into_the_target_under_another_spelling_is_seen_before_deleting() {
    for moved in ["home", "cwd"] {
        let (_sandbox, base) = base();
        let (home, cwd, store) = places(&base);
        fs::create_dir(base.join("outside")).unwrap();
        let (_, inside) = alias(&base, "store/sub", "STORE/sub");
        let link = base.join("link");
        symlink(base.join("outside"), &link).unwrap();
        let (home, cwd) = match moved {
            "home" => (link.clone(), cwd),
            _ => (home, link.clone()),
        };
        let plan = plan_purge(&store, &home, &cwd, false).unwrap();
        fs::remove_file(&link).unwrap();
        symlink(&inside, &link).unwrap();
        let result = purge(plan);
        assert_eq!(result.outcome, PurgeOutcome::Refused, "{moved}");
        assert_eq!(result.refusal.unwrap().kind, PurgeRefusalKind::TooBroad);
        assert!(store.join("sub/f").exists() && store.join(DATABASE_FILENAME).exists());
    }
}

#[test]
fn a_protected_directory_that_cannot_be_compared_with_the_target_refuses_the_purge() {
    // "could not look" is not "another directory"
    for protected in ["home", "cwd"] {
        let (_sandbox, base) = base();
        let (home, cwd, store) = places(&base);
        let unseen = match protected {
            "home" => &home,
            _ => &cwd,
        };
        plant(true, unseen, libc::EACCES, false);
        let refusal = plan_purge(&store, &home, &cwd, true).unwrap_err();
        assert_eq!(
            (refusal.kind, &refusal.path, &refusal.canonical),
            (PurgeRefusalKind::Unresolvable, unseen, &Some(store.clone())),
            "{protected}"
        );
        assert_eq!(
            refusal.detail,
            format!(
                "[Errno {}] Permission denied: '{}'",
                libc::EACCES,
                unseen.display()
            )
        );
        // an ancestor of it, too
        FAULTS.with_borrow_mut(Vec::clear);
        plant(true, &base, libc::EIO, false);
        let refusal = plan_purge(&store, &home, &cwd, true).unwrap_err();
        // home is the first to be compared
        assert_eq!(
            (refusal.kind, &refusal.path),
            (PurgeRefusalKind::Unresolvable, &home)
        );
        FAULTS.with_borrow_mut(Vec::clear);
    }
}

// --- a mount below the purged directory ---

#[test]
fn a_directory_on_another_mount_below_the_target_is_not_entered() {
    for other in [Elsewhere::Device, Elsewhere::Mount] {
        let (_sandbox, base) = base();
        let (home, cwd, store) = places(&base);
        fs::create_dir_all(store.join("sub/deeper")).unwrap();
        fs::write(store.join("sub/deeper/theirs"), "t").unwrap();
        elsewhere(&store.join("sub/deeper"), other);
        let plan = plan_purge(&store, &home, &cwd, false).unwrap();
        let result = purge(plan);
        assert_eq!(
            (result.outcome, &result.path),
            (PurgeOutcome::PartlyRemoved, &store),
            "{other:?}"
        );
        assert_eq!(result.detail, strerror(libc::EXDEV));
        assert_eq!(
            fs::read_to_string(store.join("sub/deeper/theirs")).unwrap(),
            "t"
        );
        MOUNTS.with_borrow_mut(Vec::clear);
    }
}
