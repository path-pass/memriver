//! Composition only: gather the environment, the home and the arguments, then run.

use std::ffi::OsString;
use std::io::{IsTerminal, Write};
use std::path::PathBuf;

use memriver::cli::{Io, run};
use memriver_core::settings::Env;

type Gathered = (Vec<String>, Env, PathBuf, PathBuf);

/// What `run` needs from the process, or the one line to print before exiting with 2.
/// Nothing memriver would act on is dropped: an argument or a `MEMRIVER_` variable that
/// is not UTF-8 is an error, never a shorter command line or a default store.
fn gather(
    args: impl Iterator<Item = OsString>,
    vars: impl Iterator<Item = (OsString, OsString)>,
    home: Option<PathBuf>,
    cwd: std::io::Result<PathBuf>,
) -> Result<Gathered, String> {
    let mut argv = Vec::new();
    for (position, arg) in args.enumerate() {
        let arg = arg
            .into_string()
            .map_err(|_| format!("argument {position} is not valid UTF-8"))?;
        argv.push(arg);
    }
    let mut env = Env::new();
    for (name, value) in vars {
        // a name that is not UTF-8 cannot be one of memriver's
        let Ok(name) = name.into_string() else {
            continue;
        };
        match value.into_string() {
            Ok(value) => drop(env.insert(name, value)),
            Err(_) if name.starts_with("MEMRIVER_") => {
                return Err(format!("environment variable {name} is not valid UTF-8"));
            }
            Err(_) => {}
        }
    }
    let home = home
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or("cannot determine the home directory")?;
    let cwd = cwd.map_err(|error| format!("cannot determine the current directory: {error}"))?;
    Ok((argv, env, home, cwd))
}

fn main() {
    let gathered = gather(
        std::env::args_os(),
        std::env::vars_os(),
        std::env::home_dir(),
        std::env::current_dir(),
    );
    let (argv, env, home, cwd) = match gathered {
        Ok(gathered) => gathered,
        Err(message) => {
            eprintln!("memriver: {message}");
            std::process::exit(2);
        }
    };
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

#[cfg(test)]
mod tests {
    use std::os::unix::ffi::OsStringExt;

    use super::*;

    fn os(text: &str) -> OsString {
        OsString::from(text)
    }

    fn not_utf8() -> OsString {
        OsString::from_vec(vec![b'x', 0xff])
    }

    fn gathered(
        args: Vec<OsString>,
        vars: Vec<(OsString, OsString)>,
        home: Option<&str>,
        cwd: std::io::Result<PathBuf>,
    ) -> Result<Gathered, String> {
        gather(
            args.into_iter(),
            vars.into_iter(),
            home.map(PathBuf::from),
            cwd,
        )
    }

    #[test]
    fn everything_usable_is_handed_on() {
        let vars = vec![
            (os("MEMRIVER_ROOT"), os("/s")),
            (os("OTHER"), not_utf8()),
            (not_utf8(), os("v")),
        ];
        let (argv, env, home, cwd) = gathered(
            vec![os("memriver"), os("list")],
            vars,
            Some("/h"),
            Ok(PathBuf::from("/w")),
        )
        .unwrap();
        assert_eq!(argv, ["memriver", "list"]);
        assert_eq!(
            env,
            Env::from([("MEMRIVER_ROOT".to_string(), "/s".to_string())])
        );
        assert_eq!((home, cwd), (PathBuf::from("/h"), PathBuf::from("/w")));
    }

    #[test]
    fn an_argument_that_is_not_utf8_is_an_error() {
        let error = gathered(
            vec![os("memriver"), os("show"), not_utf8()],
            vec![],
            Some("/h"),
            Ok(PathBuf::new()),
        );
        assert_eq!(error.unwrap_err(), "argument 2 is not valid UTF-8");
    }

    #[test]
    fn a_memriver_variable_that_is_not_utf8_is_an_error() {
        let vars = vec![(os("MEMRIVER_SEARCH_LIMIT_MAX"), not_utf8())];
        let error = gathered(vec![], vars, Some("/h"), Ok(PathBuf::new()));
        assert_eq!(
            error.unwrap_err(),
            "environment variable MEMRIVER_SEARCH_LIMIT_MAX is not valid UTF-8"
        );
    }

    #[test]
    fn a_missing_or_empty_home_is_an_error() {
        for home in [None, Some("")] {
            let error = gathered(vec![], vec![], home, Ok(PathBuf::new()));
            assert_eq!(error.unwrap_err(), "cannot determine the home directory");
        }
    }

    #[test]
    fn an_unreadable_current_directory_is_an_error() {
        let gone = std::io::Error::from(std::io::ErrorKind::NotFound);
        let error = gathered(vec![], vec![], Some("/h"), Err(gone));
        assert!(
            error
                .unwrap_err()
                .starts_with("cannot determine the current directory: ")
        );
    }
}
