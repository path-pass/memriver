//! Test isolation: a temporary home and store, and the one way tests start a process.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
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

/// `program` with exactly `Sandbox::env` as its environment and the sandbox as its
/// working directory. The only way a test starts a process.
pub fn isolated_command(sandbox: &Sandbox, program: &Path) -> Command {
    for path in [&sandbox.home, &sandbox.root] {
        assert!(
            path.starts_with(sandbox.dir.path()),
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
}
