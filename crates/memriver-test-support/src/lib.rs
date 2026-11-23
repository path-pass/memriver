//! Test isolation: a temporary home and store, and the one way tests start a process.

use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};
use std::process::Command;

use tempfile::TempDir;

/// A temporary directory holding a home directory and the place of a store.
pub struct Sandbox {
    pub dir: TempDir,
    /// Exists.
    pub home: PathBuf,
    /// Does not exist yet: the first write creates a store.
    pub root: PathBuf,
}

impl Sandbox {
    pub fn new() -> Sandbox {
        let dir = TempDir::new().expect("a temporary directory");
        let home = dir.path().join("home");
        std::fs::create_dir(&home).expect("the sandbox home");
        let root = dir.path().join("store");
        Sandbox { dir, home, root }
    }

    /// The whole environment of a sandboxed process.
    pub fn env(&self) -> HashMap<String, String> {
        let text = |path: &Path| path.to_str().expect("a UTF-8 temporary path").to_string();
        HashMap::from([
            ("PATH".to_string(), "/usr/bin:/bin".to_string()),
            ("HOME".to_string(), text(&self.home)),
            ("MEMRIVER_ROOT".to_string(), text(&self.root)),
        ])
    }
}

impl Default for Sandbox {
    fn default() -> Sandbox {
        Sandbox::new()
    }
}

/// Whether `path` never leaves `dir`: it is written below `dir`, and following it one
/// component at a time -- each link read as a link, its target followed the same way --
/// no step stands outside `dir`. A chain that goes out and comes back is outside, and so
/// is whatever cannot be told (a link back to itself, a component that cannot be
/// examined). `path` need not exist (a store is created by its first write): what is not
/// there yet is taken as written, and a link whose target is missing is still a link.
fn is_inside(path: &Path, dir: &Path) -> bool {
    let (Ok(below), Ok(root)) = (path.strip_prefix(dir), dir.canonicalize()) else {
        return false;
    };
    let (mut at, mut rest, mut links_left) = (root.clone(), below.to_path_buf(), 40);
    loop {
        let mut parts = rest.components();
        let Some(part) = parts.next() else {
            return true;
        };
        let after = parts.as_path().to_path_buf();
        match part {
            Component::Prefix(_) | Component::RootDir => return false,
            Component::CurDir => {}
            Component::ParentDir => drop(at.pop()),
            Component::Normal(name) => {
                let next = at.join(name);
                match next.symlink_metadata() {
                    Ok(found) if found.file_type().is_symlink() => {
                        let Ok(target) = next.read_link() else {
                            return false;
                        };
                        if links_left == 0 {
                            return false;
                        }
                        links_left -= 1;
                        // a relative target goes on from the link's directory; an absolute
                        // one must itself be written below the sandbox, and starts at its top
                        rest = if target.is_absolute() {
                            let Ok(inside) = target
                                .strip_prefix(dir)
                                .or_else(|_| target.strip_prefix(&root))
                            else {
                                return false;
                            };
                            at = root.clone();
                            inside.join(after)
                        } else {
                            target.join(after)
                        };
                        continue;
                    }
                    Ok(_) => at = next,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => at = next,
                    Err(_) => return false,
                }
            }
        }
        if !at.starts_with(&root) {
            return false;
        }
        rest = after;
    }
}

/// `program` with exactly `Sandbox::env` as its environment and the sandbox as its
/// working directory. The only way a test starts a process.
pub fn isolated_command(sandbox: &Sandbox, program: &Path) -> Command {
    for path in [&sandbox.home, &sandbox.root] {
        assert!(
            is_inside(path, sandbox.dir.path()),
            "{} is outside the sandbox",
            path.display()
        );
    }
    let mut command = Command::new(program);
    command
        .env_clear()
        .envs(sandbox.env())
        .current_dir(sandbox.dir.path());
    command
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sandbox_paths_are_inside_its_directory() {
        let sandbox = Sandbox::new();
        assert!(sandbox.home.starts_with(sandbox.dir.path()));
        assert!(sandbox.root.starts_with(sandbox.dir.path()));
        assert!(sandbox.home.is_dir());
        assert_ne!(sandbox.home, sandbox.root);
        let env = sandbox.env();
        let mut names: Vec<&str> = env.keys().map(String::as_str).collect();
        names.sort();
        assert_eq!(names, ["HOME", "MEMRIVER_ROOT", "PATH"]);
        assert_eq!(env["PATH"], "/usr/bin:/bin");
        assert_eq!(Path::new(&env["HOME"]), sandbox.home);
        assert_eq!(Path::new(&env["MEMRIVER_ROOT"]), sandbox.root);
    }

    #[test]
    fn isolated_command_carries_no_inherited_variable() {
        let sandbox = Sandbox::new();
        let output = isolated_command(&sandbox, Path::new("/usr/bin/env"))
            .output()
            .unwrap();
        let mut lines: Vec<String> = String::from_utf8(output.stdout)
            .unwrap()
            .lines()
            .map(str::to_string)
            .collect();
        lines.sort();
        let mut expected: Vec<String> = sandbox
            .env()
            .iter()
            .map(|(name, value)| format!("{name}={value}"))
            .collect();
        expected.sort();
        assert_eq!(lines, expected);
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_home_outside_the_sandbox() {
        let mut sandbox = Sandbox::new();
        sandbox.home = PathBuf::from("/");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_home_that_is_a_link_out_of_the_sandbox() {
        let other = Sandbox::new();
        let mut sandbox = Sandbox::new();
        let link = sandbox.dir.path().join("link");
        std::os::unix::fs::symlink(&other.home, &link).unwrap();
        sandbox.home = link;
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_root_that_would_be_created_through_a_link_out() {
        let other = Sandbox::new();
        let mut sandbox = Sandbox::new();
        let link = sandbox.dir.path().join("link");
        std::os::unix::fs::symlink(other.dir.path(), &link).unwrap();
        sandbox.root = link.join("not-there-yet").join("store");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    /// A sandbox whose `root` is `rest` below a link named `link` pointing at `target`.
    fn linked(target: &Path, rest: &str) -> Sandbox {
        let mut sandbox = Sandbox::new();
        let link = sandbox.dir.path().join("link");
        std::os::unix::fs::symlink(target, &link).unwrap();
        sandbox.root = if rest.is_empty() {
            link
        } else {
            link.join(rest)
        };
        sandbox
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_root_that_is_a_dangling_link_out() {
        let other = Sandbox::new();
        let sandbox = linked(&other.dir.path().join("not-there"), "");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_root_below_a_dangling_link_out() {
        let other = Sandbox::new();
        let sandbox = linked(&other.dir.path().join("not-there"), "deeper/store");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_dangling_link_with_a_relative_target_out() {
        let sandbox = linked(Path::new("../not-there-either"), "");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_link_out_reached_through_a_link_inside() {
        let other = Sandbox::new();
        let mut sandbox = Sandbox::new();
        let (first, second) = (
            sandbox.dir.path().join("first"),
            sandbox.dir.path().join("second"),
        );
        std::os::unix::fs::symlink("second", &first).unwrap();
        std::os::unix::fs::symlink(other.dir.path().join("not-there"), &second).unwrap();
        sandbox.root = first;
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    /// A sandbox whose `home` is a link named `link` with `target`, written as given.
    fn home_linked(target: &Path) -> Sandbox {
        let mut sandbox = Sandbox::new();
        std::fs::write(sandbox.home.join("marker"), "inside").unwrap();
        let link = sandbox.dir.path().join("link");
        std::os::unix::fs::symlink(target, &link).unwrap();
        sandbox.home = link;
        sandbox
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_chain_that_leaves_the_sandbox_and_comes_back() {
        // link (inside) -> back (in another sandbox) -> home (inside again)
        let other = Sandbox::new();
        let back = other.dir.path().join("back");
        let sandbox = home_linked(&back);
        std::os::unix::fs::symlink(sandbox.dir.path().join("home"), &back).unwrap();
        assert!(sandbox.home.join("marker").is_file());
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_relative_chain_through_another_sandbox() {
        let other = Sandbox::new();
        let other = other.dir.path().canonicalize().unwrap();
        let sandbox = home_linked(
            &Path::new("..")
                .join(other.file_name().unwrap())
                .join("back"),
        );
        let inside = sandbox.dir.path().canonicalize().unwrap();
        std::os::unix::fs::symlink(inside.join("home"), other.join("back")).unwrap();
        assert!(sandbox.home.join("marker").is_file());
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_target_that_steps_out_and_back_by_name() {
        // no other place is touched: ../<this sandbox>/home
        let mut sandbox = Sandbox::new();
        let name = sandbox.dir.path().canonicalize().unwrap();
        let link = sandbox.dir.path().join("link");
        std::os::unix::fs::symlink(
            Path::new("..").join(name.file_name().unwrap()).join("home"),
            &link,
        )
        .unwrap();
        assert!(link.is_dir());
        sandbox.home = link;
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_component_below_a_link_that_leads_out_and_back() {
        // root = link/store, link -> sub (inside), sub/store -> back (outside) -> inside
        let other = Sandbox::new();
        let mut sandbox = Sandbox::new();
        let dir = sandbox.dir.path().to_path_buf();
        std::fs::create_dir(dir.join("sub")).unwrap();
        std::os::unix::fs::symlink("sub", dir.join("link")).unwrap();
        std::os::unix::fs::symlink(other.dir.path().join("back"), dir.join("sub").join("store"))
            .unwrap();
        std::os::unix::fs::symlink(dir.join("real-store"), other.dir.path().join("back")).unwrap();
        sandbox.root = dir.join("link").join("store");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    fn isolated_command_accepts_links_that_never_leave_the_sandbox() {
        // relative with `..` that stays below the root, and absolute by the resolved root
        let mut sandbox = Sandbox::new();
        let dir = sandbox.dir.path().to_path_buf();
        std::fs::create_dir(dir.join("sub")).unwrap();
        std::os::unix::fs::symlink("../home", dir.join("sub").join("up")).unwrap();
        std::os::unix::fs::symlink(
            dir.canonicalize().unwrap().join("sub").join("up"),
            dir.join("link"),
        )
        .unwrap();
        sandbox.home = dir.join("link");
        sandbox.root = dir.join("sub").join("up").join("..").join("store");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_link_that_leads_to_itself() {
        let mut sandbox = Sandbox::new();
        let link = sandbox.dir.path().join("loop");
        std::os::unix::fs::symlink(&link, &link).unwrap();
        sandbox.root = link.join("store");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_relative_path() {
        let mut sandbox = Sandbox::new();
        sandbox.root = PathBuf::from("store");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    fn isolated_command_accepts_a_dangling_link_that_stays_inside_the_sandbox() {
        let inside = Sandbox::new();
        let target = inside.dir.path().join("later");
        let mut sandbox = linked(&target, "store");
        // the link was made in a new sandbox: point it into that one
        let link = sandbox.dir.path().join("link");
        std::fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink("later", &link).unwrap();
        sandbox.root = link.join("store");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    fn isolated_command_accepts_a_link_that_stays_inside_the_sandbox() {
        let mut sandbox = Sandbox::new();
        let link = sandbox.dir.path().join("link");
        std::os::unix::fs::symlink(&sandbox.home, &link).unwrap();
        sandbox.home = link;
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }

    #[test]
    #[should_panic(expected = "outside the sandbox")]
    fn isolated_command_refuses_a_path_that_climbs_out_of_the_sandbox() {
        let mut sandbox = Sandbox::new();
        sandbox.root = sandbox.dir.path().join("..").join("..");
        isolated_command(&sandbox, Path::new("/usr/bin/env"));
    }
}
