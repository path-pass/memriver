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

/// Whether `path`, with every link followed, lies under `dir`. `path` need not exist (a
/// store is created by its first write): its nearest existing ancestor is resolved and
/// the rest, which may not climb with `..`, is appended.
fn is_inside(path: &Path, dir: &Path) -> bool {
    if path.components().any(|part| part == Component::ParentDir) {
        return false;
    }
    let Some(existing) = path.ancestors().find(|ancestor| ancestor.exists()) else {
        return false;
    };
    let (Ok(resolved), Ok(dir)) = (existing.canonicalize(), dir.canonicalize()) else {
        return false;
    };
    resolved
        .join(path.strip_prefix(existing).unwrap_or(Path::new("")))
        .starts_with(dir)
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
