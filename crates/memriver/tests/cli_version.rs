use std::collections::HashMap;
use std::path::Path;

use memriver::cli::{Io, run};
use memriver_test_support::{Sandbox, isolated_command};

#[test]
fn version_through_the_library_entry() {
    let sandbox = Sandbox::new();
    let (mut stdin, mut stdout, mut stderr) = (&b""[..], Vec::new(), Vec::new());
    let mut io = Io {
        stdin: &mut stdin,
        stdin_is_tty: false,
        stdout: &mut stdout,
        stderr: &mut stderr,
    };
    let argv = vec!["memriver".to_string(), "--version".to_string()];
    let code = run(
        argv,
        &sandbox.env(),
        &sandbox.home,
        sandbox.dir.path(),
        &mut io,
    );
    assert_eq!(
        (code, stdout.as_slice(), stderr.as_slice()),
        (0, &b"0.2.0\n"[..], &b""[..])
    );
}

#[test]
fn anything_else_is_not_implemented() {
    let (mut stdin, mut stdout, mut stderr) = (&b""[..], Vec::new(), Vec::new());
    let mut io = Io {
        stdin: &mut stdin,
        stdin_is_tty: false,
        stdout: &mut stdout,
        stderr: &mut stderr,
    };
    let argv = vec!["memriver".to_string(), "list".to_string()];
    let code = run(
        argv,
        &HashMap::new(),
        Path::new("/nonexistent"),
        Path::new("/nonexistent"),
        &mut io,
    );
    assert_eq!(code, 2);
    assert_eq!(stdout, b"");
    assert_eq!(stderr, b"memriver: not implemented\n");
}

#[test]
fn version_through_the_binary() {
    let sandbox = Sandbox::new();
    let output = isolated_command(&sandbox, Path::new(env!("CARGO_BIN_EXE_memriver")))
        .arg("--version")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(output.stdout, b"0.2.0\n");
    assert_eq!(output.stderr, b"");
}
