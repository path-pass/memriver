//! The command line's grammar: what is a command, what is none, and the commands that
//! are parsed before they are built.

mod common;

use common::{Done, empty, run_in};
use memriver_core::settings::Env;

fn run(args: &[&str]) -> Done {
    let (_sandbox, tmp) = empty();
    let store = tmp.join("mem").to_string_lossy().into_owned();
    let env = Env::from([("MEMRIVER_ROOT".to_string(), store)]);
    let done = run_in(&env, &tmp.join("home"), &tmp.join("work"), args, "", false);
    assert!(!tmp.join("mem").exists(), "{args:?} made a store");
    done
}

fn usage_error(args: &[&str]) -> String {
    let done = run(args);
    assert_eq!((done.code, done.stdout.as_str()), (2, ""), "{args:?}");
    assert!(
        done.stderr.starts_with("error: "),
        "{args:?}: {}",
        done.stderr
    );
    done.stderr
}

#[test]
fn the_version_is_printed_alone_and_survives_the_legacy_store_options() {
    for args in [
        &["--version"][..],
        &["--root", "X", "--version"],
        &["--root=X", "--project-dir", "D", "--version"],
        &["serve", "--version"],
    ] {
        let done = run(args);
        assert_eq!(
            (done.code, done.stdout.as_str(), done.stderr.as_str()),
            (0, "0.2.0\n", ""),
            "{args:?}"
        );
    }
}

#[test]
fn the_form_without_a_command_is_serve() {
    // every registration written before the commands existed spells the server so
    for args in [
        &[][..],
        &["--root", "R", "--project-dir", "D"],
        &["--project-dir=D"],
        &["serve"],
        &[
            "serve",
            "--root",
            "R",
            "--project-dir",
            "D",
            "--harness",
            "cursor",
        ],
    ] {
        let done = run(args);
        assert_eq!(
            (done.code, done.stdout.as_str(), done.stderr.as_str()),
            (2, "", "memriver: not implemented\n"),
            "{args:?}"
        );
    }
    // an unknown first word stays an error: it is never served
    usage_error(&["nothing"]);
    usage_error(&["--harness", "codex"]);
}

#[test]
fn the_commands_of_later_work_are_parsed_and_say_they_are_not_built() {
    for args in [
        &["hook", "session-start", "--harness", "claude-code"][..],
        &[
            "hook",
            "pre-tool-use",
            "--harness",
            "codex",
            "--root",
            "R",
            "--project-dir",
            "D",
        ],
        &["install"],
        &["install", "--harness", "kiro", "--yes", "--dry-run"],
        &["install", "--all"],
        &["uninstall", "--all", "--purge-data", "--root", "R"],
        &["doctor", "--json", "--stale-days", "30"],
        &[
            "dream",
            "init",
            "--executor",
            "codex",
            "--ttl-days",
            "7",
            "--yes",
        ],
        &["dream", "run", "--phase", "retire", "--trigger", "schedule"],
        &["dream", "report", "--list"],
        &["dream", "report", "rrrrrrrrrr", "--list", "3"],
        &["dream", "uninstall"],
    ] {
        let done = run(args);
        assert_eq!(
            (done.code, done.stdout.as_str(), done.stderr.as_str()),
            (2, "", "memriver: not implemented\n"),
            "{args:?}"
        );
    }
}

#[test]
fn a_value_outside_a_commands_choices_is_a_usage_error() {
    for args in [
        &["serve", "--harness", "vim"][..],
        &["hook", "session-start", "--harness", "cursor"],
        &["hook", "session-start"],
        &["hook", "start", "--harness", "codex"],
        &["install", "--harness", "codex", "--all"],
        &["uninstall", "--clean-uv-cache"],
        &["doctor", "--stale-days", "0"],
        &["dream"],
        &["dream", "init", "--at", "04:00"],
        &["dream", "run", "--phase", "all"],
        &["project"],
        &["project", "explain", "--yes"],
        &["project", "unbind", "pppppppppp"],
        &["search"],
        &["search", "x", "--limit", "0"],
        &["search", "x", "--limit", "-3"],
        &["sessions", "--limit", "1.5"],
        &["history", "mmmmmmmmmm", "--show", "0"],
        &["restore", "mmmmmmmmmm"],
        &["restore", "mmmmmmmmmm", "--to", "none"],
        &[
            "delete",
            "mmmmmmmmmm",
            "--hard",
            "--dry-run",
            "--confirm",
            "0123456789abcdef",
        ],
        &["list", "extra"],
    ] {
        usage_error(args);
    }
}

#[test]
fn help_goes_to_stdout_and_names_the_commands() {
    let done = run(&["--help"]);
    assert_eq!((done.code, done.stderr.as_str()), (0, ""));
    for command in [
        "serve",
        "hook",
        "install",
        "uninstall",
        "doctor",
        "project",
        "list",
        "show",
        "search",
        "export",
        "sessions",
        "history",
        "restore",
        "undo",
        "delete",
        "dream",
    ] {
        assert!(
            done.stdout.contains(&format!("\n  {command} ")),
            "{command}"
        );
    }
    for command in ["search", "sessions"] {
        let done = run(&[command, "--help"]);
        let words = done.stdout.split_whitespace().collect::<Vec<_>>().join(" ");
        assert_eq!(done.code, 0);
        assert!(words.contains("Space-separated keywords"), "{words}");
        assert!(
            words.contains("entries matching more keywords first"),
            "{words}"
        );
    }
}

#[test]
fn a_long_option_may_be_shortened_while_it_names_one() {
    // as the Python implementation's parser allowed
    let done = run(&["--vers"]);
    assert_eq!((done.code, done.stdout.as_str()), (0, "0.2.0\n"));
    let done = run(&["show", "mmmmmmmmmm", "--del"]);
    assert_eq!(
        (done.code, done.stdout.as_str()),
        (2, "no such memory: mmmmmmmmmm\n")
    );
    usage_error(&["delete", "mmmmmmmmmm", "--version", "1", "--d"]);
}
