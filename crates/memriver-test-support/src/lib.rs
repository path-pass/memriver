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

/// Where an absolute `path` leads once every link on it is followed, whether or not the
/// place exists: a link is read as a link even when its target is missing, and what is
/// not there yet is taken as written. None when that cannot be told (a link that leads
/// back to itself, a component that cannot be examined).
fn resolve(path: &Path, links_left: u8) -> Option<PathBuf> {
    let mut at = PathBuf::new();
    for part in path.components() {
        match part {
            Component::Prefix(_) | Component::RootDir => at.push(part),
            Component::CurDir => {}
            Component::ParentDir => drop(at.pop()),
            Component::Normal(name) => {
                let next = at.join(name);
                match next.symlink_metadata() {
                    Ok(found) if found.file_type().is_symlink() => {
                        // a relative target starts from the link's directory, an absolute one replaces it
                        at = resolve(&at.join(next.read_link().ok()?), links_left.checked_sub(1)?)?;
                    }
                    Ok(_) => at = next,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => at = next,
                    Err(_) => return None,
                }
            }
        }
    }
    Some(at)
}

/// Whether `path` stays under `dir` at every step: it is written below `dir` without
/// `..`, and each of its components -- a link whose target is missing included -- leads
/// to a place under `dir` once links are followed. `path` need not exist (a store is
/// created by its first write).
fn is_inside(path: &Path, dir: &Path) -> bool {
    let (Ok(below), Some(dir)) = (path.strip_prefix(dir), resolve(dir, 40)) else {
        return false;
    };
    let mut at = dir.clone();
    for part in below.components() {
        let Component::Normal(name) = part else {
            return false;
        };
        match resolve(&at.join(name), 40) {
            Some(next) if next.starts_with(&dir) => at = next,
            _ => return false,
        }
    }
    true
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
