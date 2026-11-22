//! The command line: the one entry every command goes through.

use std::io::{BufRead, Write};
use std::path::Path;

use memriver_core::settings::Env;

/// The streams of one invocation.
pub struct Io<'a> {
    pub stdin: &'a mut dyn BufRead,
    pub stdin_is_tty: bool,
    pub stdout: &'a mut dyn Write,
    pub stderr: &'a mut dyn Write,
}

/// Runs `argv` (the program name first) and returns the exit code.
pub fn run(argv: Vec<String>, _env: &Env, _home: &Path, _cwd: &Path, io: &mut Io) -> i32 {
    if argv.get(1).map(String::as_str) == Some("--version") && argv.len() == 2 {
        let _ = writeln!(io.stdout, "{}", env!("CARGO_PKG_VERSION"));
        return 0;
    }
    let _ = writeln!(io.stderr, "memriver: not implemented");
    2
}
