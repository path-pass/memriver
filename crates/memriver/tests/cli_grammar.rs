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
        &["install"][..],
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

#[test]
fn an_option_given_twice_counts_once_and_the_last_value_is_taken() {
    // as the Python implementation's parser did; before, each was a usage error
    let done = run(&["show", "mmmmmmmmmm", "--deleted", "--deleted"]);
    assert_eq!(
        (done.code, done.stdout.as_str()),
        (2, "no such memory: mmmmmmmmmm\n")
    );
    let done = run(&["history", "mmmmmmmmmm", "--show", "1", "--show", "2"]);
    assert_eq!(
        (done.code, done.stdout.as_str()),
        (2, "no such memory: mmmmmmmmmm\n")
    );
    // each value is still read: one that is no number is refused wherever it stands
    usage_error(&["history", "mmmmmmmmmm", "--show", "2", "--show", "0"]);
    usage_error(&["history", "mmmmmmmmmm", "--show", "0", "--show", "2"]);
    let done = run(&["delete", "mmmmmmmmmm", "--version", "1", "--yes", "--yes"]);
    assert_eq!(
        (done.code, done.stdout.as_str()),
        (2, "no such memory: mmmmmmmmmm\n")
    );
}

// A hyphen and a number written in ASCII digits is a value, as the Python implementation's
// parser took it. Every other argument that begins with a hyphen is an option; that parser
// took more of them for values (see the list of differences), and none is taken here.
#[test]
fn a_hyphen_and_a_number_make_a_value_and_anything_else_an_option() {
    for (args, printed) in [
        (&["show", "-5"][..], "no such memory: -5\n"),
        (&["show", "-5", "--deleted"], "no such memory: -5\n"),
        (&["show", "--deleted", "-1.5"], "no such memory: -1.5\n"),
        (&["show", "-1."], "no such memory: -1.\n"),
        (&["show", "-1e3"], "no such memory: -1e3\n"),
        (&["show", "-"], "no such memory: -\n"),
        (&["show", "--", "-x y"], "no such memory: -x y\n"),
        (
            &["undo", "-7", "--yes"],
            "refused (not-found): no change -7\n",
        ),
        (&["list", "--project", "-1"], "no such project: -1\n"),
        (&["list", "--project=-x"], "no such project: -x\n"),
        (&["list", "--project=- x"], "no such project: - x\n"),
        (&["show", "\u{e000}-5"], "no such memory: \u{e000}-5\n"),
    ] {
        let done = run(args);
        assert_eq!(
            (done.code, done.stdout.as_str(), done.stderr.as_str()),
            (2, printed, ""),
            "{args:?}"
        );
    }
    for args in [
        &["show", "-x"][..],
        &["show", "-5", "-6"],
        &["list", "-1"],
        &["list", "--project", "-x"],
        &["list", "--project", "--root"],
        &["list", "--root", "--project", "x"],
        &["search", "x", "--limit", "-1"],
        &["-1"],
        &["show", "-."],
        &["show", "-.5"],
        &["show", "-0x10"],
        &["show", "-1_0"],
        &["show", "-1e"],
        &["show", "-1e+3"],
        &["show", "-1.2.3"],
        &["show", "-\u{ff11}"],
        &["show", "-\u{663}"],
        &["show", "- x"],
        &["show", "-x y"],
        &["show", "--not an option"],
        &["list", "--project", "- 1"],
        &["delete", "mmmmmmmmmm", "--hard", "--confirm", "-x"],
        &["delete", "mmmmmmmmmm", "--hard", "--confirm", "--yes"],
    ] {
        usage_error(args);
    }
}
