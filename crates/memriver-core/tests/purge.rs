//! Deleting a whole store: what is refused before anything is opened, and what happens
//! when the target changes between the plan and the deletion.

use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};

use memriver_core::repository::directories::{
    DATABASE_FILENAME, PurgeOutcome, PurgeRefusalKind, plan_purge, purge,
};
use memriver_test_support::Sandbox;

struct Places {
    _sandbox: Sandbox,
    base: PathBuf,
    home: PathBuf,
    cwd: PathBuf,
    store: PathBuf,
}

/// Home, working directory and a store with a database and a sub-directory.
fn places() -> Places {
    let sandbox = Sandbox::new();
    let base = fs::canonicalize(sandbox.dir.path()).unwrap();
    let (home, cwd, store) = (base.join("home"), base.join("cwd"), base.join("store"));
    for directory in [&home, &cwd, &store.join("sub")] {
        fs::create_dir_all(directory).unwrap();
    }
    fs::write(store.join(DATABASE_FILENAME), "x").unwrap();
    fs::write(store.join("sub/f"), "y").unwrap();
    Places {
        _sandbox: sandbox,
        base,
        home,
        cwd,
        store,
    }
}

fn mode(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}

/// Whether a directory without permissions really stops us (it does not stop root).
fn permissions_hold(base: &Path) -> bool {
    let probe = base.join("permission-probe");
    fs::create_dir(&probe).unwrap();
    mode(&probe, 0o000);
    let held = fs::read_dir(&probe).is_err();
    fs::remove_dir(&probe).unwrap();
    held
}

#[test]
fn a_confirmed_store_is_removed() {
    let at = places();
    // a link inside the store is removed where it stands, never followed
    fs::create_dir(at.base.join("outside")).unwrap();
    fs::write(at.base.join("outside/keep"), "k").unwrap();
    symlink(at.base.join("outside"), at.store.join("sub/link")).unwrap();
    let plan = plan_purge(&at.store, &at.home, &at.cwd, false).unwrap();
    assert!(plan.exists && plan.is_open());
    assert_eq!((&plan.given, &plan.canonical), (&at.store, &at.store));
    let result = purge(plan);
    assert_eq!(
        (result.outcome, &result.path),
        (PurgeOutcome::Removed, &at.store)
    );
    assert_eq!(
        (
            result.detail.as_str(),
            result.replaced.len(),
            result.refusal
        ),
        ("", 0, None)
    );
    assert!(!at.store.exists());
    assert!(at.base.join("outside/keep").exists());
}

#[test]
fn a_nonempty_directory_without_a_store_is_refused() {
    let at = places();
    let target = at.base.join("documents");
    fs::create_dir(&target).unwrap();
    fs::write(target.join("notes.txt"), "mine").unwrap();
    // a database further down does not make it a store
    fs::create_dir(target.join("deeper")).unwrap();
    fs::write(target.join("deeper").join(DATABASE_FILENAME), "x").unwrap();
    for dry_run in [true, false] {
        let refusal = plan_purge(&target, &at.home, &at.cwd, dry_run).unwrap_err();
        assert_eq!(refusal.kind, PurgeRefusalKind::NotAStore);
        assert_eq!(
            (&refusal.path, refusal.canonical.as_ref()),
            (&target, Some(&target))
        );
    }
    assert_eq!(
        fs::read_to_string(target.join("notes.txt")).unwrap(),
        "mine"
    );
}

#[test]
fn an_empty_directory_is_still_purged() {
    let at = places();
    let target = at.base.join("empty");
    fs::create_dir(&target).unwrap();
    let plan = plan_purge(&target, &at.home, &at.cwd, false).unwrap();
    assert!(plan.exists && plan.is_open());
    assert_eq!(purge(plan).outcome, PurgeOutcome::Removed);
    assert!(!target.exists());
}

#[test]
fn an_unlistable_target_is_refused_not_purged() {
    let at = places();
    if !permissions_hold(&at.base) {
        return;
    }
    let target = at.base.join("locked");
    fs::create_dir(&target).unwrap();
    fs::write(target.join("keep.txt"), "mine").unwrap();
    mode(&target, 0o000);
    for dry_run in [true, false] {
        let refusal = plan_purge(&target, &at.home, &at.cwd, dry_run).unwrap_err();
        assert_eq!(refusal.kind, PurgeRefusalKind::Unopenable, "{dry_run}");
        assert_eq!(refusal.detail, "Permission denied");
        assert_eq!(refusal.canonical.as_ref(), Some(&target));
    }
    mode(&target, 0o700);
    assert_eq!(fs::read_to_string(target.join("keep.txt")).unwrap(), "mine");
}

#[test]
fn a_missing_store_is_a_plan_with_nothing_to_remove() {
    let at = places();
    for absent in ["absent", "absent/deeper", "home/../absent"] {
        let plan = plan_purge(&at.base.join(absent), &at.home, &at.cwd, false).unwrap();
        assert!(!plan.exists && !plan.is_open(), "{absent}");
    }
    let plan = plan_purge(&at.base.join("home/../absent"), &at.home, &at.cwd, true).unwrap();
    assert_eq!(plan.canonical, at.base.join("absent"));
}

#[test]
fn a_dry_run_plan_opens_nothing() {
    let at = places();
    let plan = plan_purge(&at.store, &at.home, &at.cwd, true).unwrap();
    assert!(plan.exists && !plan.is_open());
    assert!(at.store.join(DATABASE_FILENAME).exists());
}

#[test]
#[should_panic(expected = "purge needs an open plan made for deletion")]
fn purge_without_an_opened_plan_is_a_programming_error() {
    let at = places();
    purge(plan_purge(&at.store, &at.home, &at.cwd, true).unwrap());
}

#[test]
fn a_target_holding_home_or_cwd_is_too_broad() {
    let at = places();
    fs::create_dir(at.home.join("below")).unwrap();
    for (target, home) in [
        (&at.home, &at.home),
        (&at.cwd, &at.home),
        (&at.base, &at.home),
        (&PathBuf::from("/"), &at.home),
        // the home directory lies in it
        (&at.home, &at.home.join("below")),
    ] {
        for dry_run in [true, false] {
            let refusal = plan_purge(target, home, &at.cwd, dry_run).unwrap_err();
            assert_eq!(refusal.kind, PurgeRefusalKind::TooBroad, "{target:?}");
            assert_eq!(
                (&refusal.path, refusal.canonical.as_ref()),
                (target, Some(target))
            );
        }
    }
    // beside it is not in it
    let beside = at.base.join("home-2");
    fs::create_dir(&beside).unwrap();
    assert!(plan_purge(&beside, &at.home, &at.cwd, true).is_ok());
}

#[test]
fn a_symlinked_target_is_refused_not_followed() {
    let at = places();
    let link = at.base.join("link");
    symlink(&at.store, &link).unwrap();
    let refusal = plan_purge(&link, &at.home, &at.cwd, false).unwrap_err();
    assert_eq!(
        (refusal.kind, &refusal.path),
        (PurgeRefusalKind::Symlink, &link)
    );
    assert_eq!(refusal.canonical.as_ref(), Some(&at.store));
    // through a linked ancestor the target is where the link leads, and that is shown
    fs::create_dir(at.base.join("real")).unwrap();
    fs::create_dir(at.base.join("real/store")).unwrap();
    symlink(at.base.join("real"), at.base.join("via")).unwrap();
    let plan = plan_purge(&at.base.join("via/store"), &at.home, &at.cwd, false).unwrap();
    assert_eq!(plan.canonical, at.base.join("real/store"));
    assert_eq!(purge(plan).path, at.base.join("real/store"));
    assert!(at.store.exists() && !at.base.join("real/store").exists());
}

#[test]
fn a_file_target_is_not_a_directory() {
    let at = places();
    let target = at.base.join("file");
    fs::write(&target, "x").unwrap();
    let refusal = plan_purge(&target, &at.home, &at.cwd, false).unwrap_err();
    assert_eq!(refusal.kind, PurgeRefusalKind::NotDirectory);
    assert!(target.exists());
}

#[test]
fn a_target_that_cannot_be_resolved_is_refused() {
    let at = places();
    let looping = at.base.join("loop");
    symlink(&looping, &looping).unwrap();
    let refusal = plan_purge(&looping, &at.home, &at.cwd, false).unwrap_err();
    assert_eq!(
        (refusal.kind, &refusal.path),
        (PurgeRefusalKind::Unresolvable, &looping)
    );
    assert_eq!(refusal.canonical, None);
    let tail = format!(
        "] Too many levels of symbolic links: '{}'",
        looping.display()
    );
    assert!(
        refusal.detail.starts_with("[Errno ") && refusal.detail.ends_with(&tail),
        "{}",
        refusal.detail
    );
}

#[test]
fn a_relative_target_resolves_against_cwd() {
    let at = places();
    fs::create_dir(at.cwd.join("rel")).unwrap();
    let plan = plan_purge(Path::new("rel"), &at.home, &at.cwd, true).unwrap();
    assert_eq!(plan.canonical, at.cwd.join("rel"));
    assert_eq!(plan.given, at.cwd.join("rel"));
}

#[test]
fn a_directory_swapped_after_confirmation_is_left_alone() {
    let at = places();
    let plan = plan_purge(&at.store, &at.home, &at.cwd, false).unwrap();
    let moved = at.base.join("moved");
    fs::rename(&at.store, &moved).unwrap();
    fs::create_dir(&at.store).unwrap();
    let result = purge(plan);
    assert_eq!(result.outcome, PurgeOutcome::NotConfirmed);
    assert!(at.store.exists() && moved.join(DATABASE_FILENAME).exists());
}

#[test]
fn a_symlink_swapped_in_between_plan_and_purge_is_never_followed() {
    // at the leaf: nothing can be checked there
    let at = places();
    let plan = plan_purge(&at.store, &at.home, &at.cwd, false).unwrap();
    let moved = at.base.join("moved");
    fs::rename(&at.store, &moved).unwrap();
    symlink(&moved, &at.store).unwrap();
    let result = purge(plan);
    assert_eq!(
        (result.outcome, &result.path),
        (PurgeOutcome::Uncheckable, &at.store)
    );
    assert!(!result.detail.is_empty());
    assert!(moved.join(DATABASE_FILENAME).exists() && moved.join("sub/f").exists());

    // gone altogether
    let at = places();
    let plan = plan_purge(&at.store, &at.home, &at.cwd, false).unwrap();
    fs::rename(&at.store, at.base.join("moved")).unwrap();
    let result = purge(plan);
    assert_eq!(
        (result.outcome, result.detail.as_str()),
        (PurgeOutcome::Uncheckable, "No such file or directory")
    );

    // above the leaf: the parent cannot be opened without following it
    let at = places();
    let store = at.base.join("deep/store");
    fs::create_dir_all(&store).unwrap();
    fs::write(store.join(DATABASE_FILENAME), "x").unwrap();
    let plan = plan_purge(&store, &at.home, &at.cwd, false).unwrap();
    fs::rename(at.base.join("deep"), at.base.join("moved")).unwrap();
    symlink(at.base.join("moved"), at.base.join("deep")).unwrap();
    let result = purge(plan);
    assert_eq!(
        (result.outcome, &result.path),
        (PurgeOutcome::UnopenableParent, &at.base.join("deep"))
    );
    assert!(at.base.join("moved/store").join(DATABASE_FILENAME).exists());
}

#[test]
fn a_failing_walk_reports_partial_removal() {
    let at = places();
    if !permissions_hold(&at.base) {
        return;
    }
    mode(&at.store.join("sub"), 0o000);
    let plan = plan_purge(&at.store, &at.home, &at.cwd, false).unwrap();
    let result = purge(plan);
    mode(&at.store.join("sub"), 0o700);
    assert_eq!(
        (result.outcome, result.detail.as_str(), &result.path),
        (PurgeOutcome::PartlyRemoved, "Permission denied", &at.store)
    );
    assert!(at.store.join("sub/f").exists());
}

/// A spelling whose strict resolution stops before a symlink loop: a missing component,
/// or a component below a regular file, then `..`.
fn loop_behind(base: &Path, prefix: &str) -> PathBuf {
    let looping = base.join("loop");
    symlink(&looping, &looping).unwrap();
    if prefix == "missing" {
        return base.join("missing/../loop");
    }
    fs::write(base.join("file"), "x").unwrap();
    base.join("file/below/../../loop")
}

#[test]
fn a_protected_base_looping_behind_an_unresolvable_prefix_is_refused() {
    for prefix in ["missing", "not-directory"] {
        for moved in ["home", "cwd"] {
            let at = places();
            let behind = loop_behind(&at.base, prefix);
            let (home, cwd) = match moved {
                "home" => (&behind, &at.cwd),
                _ => (&at.home, &behind),
            };
            let refusal = plan_purge(&at.store, home, cwd, false).unwrap_err();
            assert_eq!(
                (refusal.kind, &refusal.path),
                (PurgeRefusalKind::Unresolvable, &behind),
                "{prefix} {moved}"
            );
            assert!(refusal.detail.contains("Too many levels of symbolic links"));
            assert!(at.store.join(DATABASE_FILENAME).exists());
        }
    }
}

#[test]
fn a_protected_base_changed_after_confirmation_is_looked_at_again() {
    for moved in ["home", "cwd"] {
        // turned into a loop
        let at = places();
        fs::create_dir(at.base.join("real")).unwrap();
        let link = at.base.join("link");
        symlink(at.base.join("real"), &link).unwrap();
        let behind = at.base.join("missing/../link");
        let (home, cwd) = match moved {
            "home" => (&behind, &at.cwd),
            _ => (&at.home, &behind),
        };
        let plan = plan_purge(&at.store, home, cwd, false).unwrap();
        fs::remove_file(&link).unwrap();
        symlink(&link, &link).unwrap();
        let result = purge(plan);
        assert_eq!(result.outcome, PurgeOutcome::Refused, "{moved}");
        let refusal = result.refusal.unwrap();
        assert_eq!(
            (refusal.kind, &refusal.path),
            (PurgeRefusalKind::Unresolvable, &behind)
        );
        assert!(at.store.join(DATABASE_FILENAME).exists());

        // pointed into the store
        let plan = plan_purge(&at.store, home, cwd, false).unwrap_err();
        assert_eq!(plan.kind, PurgeRefusalKind::Unresolvable);
        fs::remove_file(&link).unwrap();
        symlink(at.base.join("real"), &link).unwrap();
        let plan = plan_purge(&at.store, home, cwd, false).unwrap();
        fs::remove_file(&link).unwrap();
        symlink(at.store.join("sub"), &link).unwrap();
        let result = purge(plan);
        assert_eq!(result.outcome, PurgeOutcome::Refused, "{moved}");
        assert_eq!(result.refusal.unwrap().kind, PurgeRefusalKind::TooBroad);
        assert!(at.store.join("sub/f").exists());
    }
}

#[test]
fn a_symlinked_target_is_refused_however_its_name_ends() {
    // `link/` is what a shell completes a link to a directory to; looked at as written,
    // it is the directory behind the link
    for ending in ["/", "/.", "//", "/./", "/.//."] {
        for dry_run in [true, false] {
            let at = places();
            let link = at.base.join("link");
            symlink(&at.store, &link).unwrap();
            let written = PathBuf::from(format!("{}{ending}", link.display()));
            let refusal = plan_purge(&written, &at.home, &at.cwd, dry_run).unwrap_err();
            assert_eq!(
                (refusal.kind, &refusal.path),
                (PurgeRefusalKind::Symlink, &link),
                "{ending:?} {dry_run}"
            );
            assert!(at.store.join(DATABASE_FILENAME).exists() && at.store.join("sub/f").exists());
        }
    }
    // relative to the working directory, too
    let at = places();
    symlink(&at.store, at.cwd.join("link")).unwrap();
    let refusal = plan_purge(Path::new("./link/"), &at.home, &at.cwd, false).unwrap_err();
    assert_eq!(
        (refusal.kind, &refusal.path),
        (PurgeRefusalKind::Symlink, &at.cwd.join("link"))
    );
    assert!(at.store.join(DATABASE_FILENAME).exists());
    // a real directory is found under any of these spellings, and shown tidied
    let plan = plan_purge(&at.base.join("store/./"), &at.home, &at.cwd, true).unwrap();
    assert_eq!((&plan.given, &plan.canonical), (&at.store, &at.store));
    // `..` is kept: it is resolved, not read off the text
    symlink(at.store.join("sub"), at.base.join("into")).unwrap();
    let plan = plan_purge(&at.base.join("into/.."), &at.home, &at.cwd, true).unwrap();
    assert_eq!(plan.given, at.base.join("into/.."));
    assert_eq!(plan.canonical, at.store);
}

/// Whether names that differ only in case are one name here.
fn ignores_case(base: &Path) -> bool {
    fs::create_dir(base.join("CaseProbe")).unwrap();
    base.join("caseprobe").exists()
}

#[test]
fn on_a_file_system_that_ignores_case_home_or_cwd_under_another_spelling_is_too_broad() {
    // only where the file system really ignores case; the same branches are reached on
    // any file system by the unit tests, with a stand-in
    for protected in ["home", "cwd"] {
        for below in ["", "deep/er"] {
            for dry_run in [true, false] {
                let at = places();
                if !ignores_case(&at.base) {
                    return;
                }
                let (real, target) = (at.base.join("Kept"), at.base.join("kept"));
                fs::create_dir_all(real.join(below)).unwrap();
                fs::write(real.join(DATABASE_FILENAME), "x").unwrap();
                let (home, cwd) = match protected {
                    "home" => (real.join(below), at.cwd.clone()),
                    _ => (at.home.clone(), real.join(below)),
                };
                // as written it is refused, and so it is under the other spelling
                for target in [&real, &target] {
                    let refusal = plan_purge(target, &home, &cwd, dry_run).unwrap_err();
                    assert_eq!(
                        (refusal.kind, &refusal.path),
                        (PurgeRefusalKind::TooBroad, target),
                        "{protected} {below:?} {dry_run}"
                    );
                }
                assert!(real.join(DATABASE_FILENAME).exists());
            }
        }
    }
}

#[test]
fn on_a_file_system_that_ignores_case_a_base_moved_in_under_another_spelling_stops_the_purge() {
    for moved in ["home", "cwd"] {
        let at = places();
        if !ignores_case(&at.base) {
            return;
        }
        fs::create_dir(at.base.join("outside")).unwrap();
        let link = at.base.join("link");
        symlink(at.base.join("outside"), &link).unwrap();
        let (home, cwd) = match moved {
            "home" => (&link, &at.cwd),
            _ => (&at.home, &link),
        };
        let plan = plan_purge(&at.store, home, cwd, false).unwrap();
        fs::remove_file(&link).unwrap();
        symlink(at.base.join("STORE/sub"), &link).unwrap();
        let result = purge(plan);
        assert_eq!(result.outcome, PurgeOutcome::Refused, "{moved}");
        assert_eq!(result.refusal.unwrap().kind, PurgeRefusalKind::TooBroad);
        assert!(at.store.join("sub/f").exists() && at.store.join(DATABASE_FILENAME).exists());
    }
}

/// `sudo -n <args>`, started as every process of a test is.
#[cfg(target_os = "linux")]
fn as_root(sandbox: &Sandbox, args: &[&str], at: &Path) -> std::process::Command {
    let mut command = memriver_test_support::isolated_command(sandbox, Path::new("/usr/bin/sudo"));
    command.arg("-n").args(args).arg(at);
    command
}

/// A real mount, taken away again when the test ends, however it ends.
#[cfg(target_os = "linux")]
struct Mounted(std::process::Command);

#[cfg(target_os = "linux")]
impl Drop for Mounted {
    fn drop(&mut self) {
        let _ = self.0.status();
    }
}

/// `mount <args> <at>` as root. Where that cannot be had the caller has nothing to test
/// with -- except in CI, where it can be had and a mount that fails is a failure.
#[cfg(target_os = "linux")]
fn mounted(sandbox: &Sandbox, args: &[&str], at: &Path) -> Option<Mounted> {
    let done = as_root(sandbox, &[&["mount"], args].concat(), at)
        .status()
        .is_ok_and(|status| status.success());
    if !done {
        assert!(
            std::env::var_os("CI").is_none(),
            "mount {args:?} failed in CI"
        );
        eprintln!("not run: mount {args:?} needs sudo without a password");
        return None;
    }
    Some(Mounted(as_root(sandbox, &["umount"], at)))
}

#[cfg(target_os = "linux")]
#[test]
fn a_mount_below_the_store_is_not_entered() {
    // the same device mounted a second time, then another file system
    for kind in ["bind", "tmpfs"] {
        let at = places();
        let outside = at.base.join("outside");
        let held = at.store.join("sub/held");
        fs::create_dir(&outside).unwrap();
        fs::create_dir(&held).unwrap();
        let source = outside.to_str().unwrap();
        let args = match kind {
            "bind" => vec!["--bind", source],
            _ => vec!["-t", "tmpfs", "tmpfs"],
        };
        let Some(_mounted) = mounted(&at._sandbox, &args, &held) else {
            return;
        };
        // written through the mount: it is what lies behind it
        fs::write(held.join("theirs"), "t").unwrap();
        let plan = plan_purge(&at.store, &at.home, &at.cwd, false).unwrap();
        let result = purge(plan);
        assert_eq!(
            (result.outcome, &result.path),
            (PurgeOutcome::PartlyRemoved, &at.store),
            "{kind}"
        );
        assert!(
            result.detail.to_lowercase().contains("cross-device"),
            "{kind}: {}",
            result.detail
        );
        assert_eq!(
            fs::read_to_string(held.join("theirs")).unwrap(),
            "t",
            "{kind}"
        );
        if kind == "bind" {
            assert_eq!(fs::read_to_string(outside.join("theirs")).unwrap(), "t");
        }
    }
}
