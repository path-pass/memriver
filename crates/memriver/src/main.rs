//! Composition only: gather the environment, the home and the arguments, then run.

use std::io::{IsTerminal, Write};

use memriver::cli::{Io, run};
use memriver_core::settings::Env;

fn main() {
    let text = |value: std::ffi::OsString| value.into_string().ok();
    let argv: Vec<String> = std::env::args_os().filter_map(text).collect();
    let env: Env = std::env::vars_os()
        .filter_map(|(name, value)| Some((text(name)?, text(value)?)))
        .collect();
    let home = std::env::home_dir().unwrap_or_default();
    let cwd = std::env::current_dir().unwrap_or_default();
    let stdin = std::io::stdin();
    let stdin_is_tty = stdin.is_terminal();
    let mut stdout = std::io::stdout().lock();
    let code = run(
        argv,
        &env,
        &home,
        &cwd,
        &mut Io {
            stdin: &mut stdin.lock(),
            stdin_is_tty,
            stdout: &mut stdout,
            stderr: &mut std::io::stderr().lock(),
        },
    );
    let _ = stdout.flush();
    std::process::exit(code);
}
