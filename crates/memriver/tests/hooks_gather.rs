//! A hook started where the process cannot be gathered -- an argument or a `MEMRIVER_`
//! variable that is not UTF-8, no working directory -- exits 0 and says
//! nothing: a harness may read exit code 2 as a blocking error. Other commands refuse
//! with one line and exit 2 (spec item 27), which `cli_version.rs` asserts.

use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::process::{Command, Output};

use memriver_test_support::{Sandbox, isolated_command};

const PAYLOAD: &[u8] = br#"{"session_id": "s", "source": "startup", "stop_hook_active": false}"#;

fn binary() -> &'static Path {
    Path::new(env!("CARGO_BIN_EXE_memriver"))
}

fn fed(mut command: Command) -> Output {
    use std::io::Write;
    use std::process::Stdio;
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(PAYLOAD).unwrap();
    child.wait_with_output().unwrap()
}

/// Silent, exit 0, and no store made.
fn assert_silent(output: &Output, sandbox: &Sandbox) {
    assert_eq!(
        (
            output.status.code(),
            output.stdout.as_slice(),
            output.stderr.as_slice()
        ),
        (Some(0), &b""[..], &b""[..]),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!sandbox.root.exists());
}

#[test]
fn a_hook_with_an_argument_that_is_not_utf8_is_silent() {
    for event in ["session-start", "stop"] {
        let sandbox = Sandbox::new();
        let mut root = sandbox.root.clone().into_os_string();
        root.push(OsStr::from_bytes(b"\xff"));
        let mut command = isolated_command(&sandbox, binary());
        command
            .args(["hook", event, "--harness", "claude-code", "--root"])
            .arg(&root);
        assert_silent(&fed(command), &sandbox);
    }
}

#[test]
fn a_hook_with_a_memriver_variable_that_is_not_utf8_is_silent() {
    for event in ["session-start", "user-prompt-submit", "session-end"] {
        let sandbox = Sandbox::new();
        let mut command = isolated_command(&sandbox, binary());
        command
            .env("MEMRIVER_MAX_BODY_CHARS", OsStr::from_bytes(b"\xff"))
            .args(["hook", event, "--harness", "codex"]);
        assert_silent(&fed(command), &sandbox);
    }
}

// No home cannot be made here: `home_dir` falls back to the password database when the
// variable is unset or empty. `main.rs`'s unit test
// `a_hook_is_silent_and_exits_0_on_every_failure_of_gather` covers it.

#[test]
fn a_hook_without_a_working_directory_is_silent() {
    // the shell enters a directory, removes it, then becomes the hook
    let sandbox = Sandbox::new();
    let gone = sandbox.dir.path().join("gone");
    std::fs::create_dir(&gone).unwrap();
    let script = r#"cd "$1" && rmdir "$1" && exec "$2" hook "$3" --harness codex"#;
    for event in ["session-start", "stop"] {
        std::fs::create_dir_all(&gone).unwrap();
        let mut command = isolated_command(&sandbox, Path::new("/bin/sh"));
        command
            .args(["-c", script, "sh"])
            .arg(&gone)
            .arg(binary())
            .arg(event);
        assert_silent(&fed(command), &sandbox);
        assert!(!gone.exists());
    }
    let mut command = isolated_command(&sandbox, Path::new("/bin/sh"));
    std::fs::create_dir_all(&gone).unwrap();
    let refused = r#"cd "$1" && rmdir "$1" && exec "$2" list"#;
    let output = command
        .args(["-c", refused, "sh"])
        .arg(&gone)
        .arg(binary())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .starts_with("memriver: cannot determine the current directory: ")
    );
}
