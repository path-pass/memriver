//! The command line: the one entry every command goes through.

pub mod args;
mod memory_commands;
mod project_commands;
pub mod views;

use std::fmt;
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

use clap::error::ErrorKind;
use clap::{CommandFactory, Parser};
use memriver_core::models::errors::{CoreError, WriteError};
use memriver_core::models::helpers::py_strip;
use memriver_core::models::session::Harness;
use memriver_core::settings::{Env, SettingsError, load_settings};
use memriver_core::{Services, build_services};

use args::{Cli, Command, ProjectCommand};

/// The streams of one invocation.
pub struct Io<'a> {
    pub stdin: &'a mut dyn BufRead,
    pub stdin_is_tty: bool,
    pub stdout: &'a mut dyn Write,
    pub stderr: &'a mut dyn Write,
}

/// What one command is run with.
pub(crate) struct Invocation<'a, 'io> {
    pub env: &'a Env,
    pub home: &'a Path,
    pub cwd: &'a Path,
    pub io: &'a mut Io<'io>,
}

impl Invocation<'_, '_> {
    /// Every line of a human command, refusals included, goes to stdout.
    pub fn say(&mut self, text: fmt::Arguments<'_>) {
        let _ = self.io.stdout.write_fmt(text);
    }
}

/// What ends a command before it has an answer of its own: one line on stderr, exit 1.
#[derive(Debug)]
pub(crate) enum Stop {
    /// The store is below the schema this version reads.
    NeedsUpgrade(i64),
    /// settings.toml or a `MEMRIVER_*` variable cannot be used.
    Settings(SettingsError),
    /// An error no command expects from the call it made: its kind, a fixed name. Never
    /// the error's own text, which may hold an id a caller typed.
    Unexpected(&'static str),
}

/// The fixed name of an error's kind: no id, no path, nothing of a user's.
fn kind(error: &CoreError) -> &'static str {
    match error {
        CoreError::BatchConflict { .. } => "batch-conflict",
        CoreError::BindingRefused { .. } => "binding-refused",
        CoreError::CapExceeded { .. } => "cap-exceeded",
        CoreError::ContentRejected { .. } => "content-rejected",
        CoreError::GlobalReadOnly => "global-read-only",
        CoreError::MemoryNotFound(_) => "memory-not-found",
        CoreError::PlanChanged(_) => "plan-changed",
        CoreError::ProjectNotFound(_) => "project-not-found",
        CoreError::ProjectUnavailable { .. } => "project-unavailable",
        CoreError::SessionMoved => "session-moved",
        CoreError::StorageFailure => "storage-failure",
        CoreError::StoreNeedsUpgrade(_) => "store-needs-upgrade",
        CoreError::UndoRefused { .. } => "undo-refused",
        CoreError::VersionConflict(_) => "version-conflict",
    }
}

impl From<SettingsError> for Stop {
    fn from(error: SettingsError) -> Stop {
        Stop::Settings(error)
    }
}

impl From<CoreError> for Stop {
    fn from(error: CoreError) -> Stop {
        match error {
            CoreError::StoreNeedsUpgrade(version) => Stop::NeedsUpgrade(version),
            other => Stop::Unexpected(kind(&other)),
        }
    }
}

impl From<WriteError> for Stop {
    fn from(error: WriteError) -> Stop {
        match error {
            WriteError::Core(error) => error.into(),
            WriteError::Value(_) => Stop::Unexpected("invalid-value"),
        }
    }
}

/// A command's exit code, or what stopped it.
pub(crate) type Outcome = Result<i32, Stop>;

/// The services over the store `--root` names (relative: from the working directory), or
/// the configured one, and that store's root. Building them reads the settings, never
/// the store.
pub(crate) fn services(root: Option<&str>, inv: &Invocation) -> Result<(Services, PathBuf), Stop> {
    let given = root.map(|root| inv.cwd.join(root));
    let settings = load_settings(given.as_deref(), inv.env, inv.home)?;
    let services = build_services(&settings, inv.home, inv.env, None);
    Ok((services, settings.root))
}

/// The answer to "may this go ahead".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Confirm {
    Yes,
    Declined,
    NotATerminal,
}

impl Confirm {
    /// None to go ahead, else the exit code: 1 when declined, 2 without a terminal.
    pub(crate) fn refused(self) -> Option<i32> {
        match self {
            Confirm::Yes => None,
            Confirm::Declined => Some(1),
            Confirm::NotATerminal => Some(2),
        }
    }
}

/// Asks before a write, unless `yes`. Without a terminal nothing is asked and nothing
/// goes ahead; the end of the input declines. The line that says so is printed here.
pub fn confirm(io: &mut Io, yes: bool, declined_line: &str) -> Confirm {
    if yes {
        return Confirm::Yes;
    }
    if !io.stdin_is_tty {
        let _ = writeln!(
            io.stdout,
            "refused: stdin is not a terminal; pass --yes to confirm non-interactively"
        );
        return Confirm::NotATerminal;
    }
    let _ = write!(io.stdout, "Proceed? [y/N] ");
    let _ = io.stdout.flush();
    let mut answer = String::new();
    // input that cannot be read is no answer, and no answer declines
    if io.stdin.read_line(&mut answer).is_err() {
        answer.clear();
    }
    match py_strip(&answer).to_lowercase().as_str() {
        "y" | "yes" => Confirm::Yes,
        _ => {
            let _ = writeln!(io.stdout, "{declined_line}");
            Confirm::Declined
        }
    }
}

// the only tokens that can open the form without a command, which every registration
// written before the commands existed still uses
const LEGACY_SERVE_FLAGS: [&str; 2] = ["--root", "--project-dir"];

fn normalize_legacy_serve(mut argv: Vec<String>) -> Vec<String> {
    if argv.is_empty() {
        argv.push("memriver".to_string());
    }
    let legacy = match argv.get(1) {
        None => true,
        Some(first) => LEGACY_SERVE_FLAGS.contains(&first.split('=').next().unwrap_or_default()),
    };
    if legacy {
        argv.insert(1, "serve".to_string());
    }
    argv
}

fn usage_error(error: &clap::Error, io: &mut Io) -> i32 {
    let text = error.render().to_string();
    let _ = match error.use_stderr() {
        true => io.stderr.write_all(text.as_bytes()),
        false => io.stdout.write_all(text.as_bytes()),
    };
    error.exit_code()
}

fn not_implemented(io: &mut Io) -> Outcome {
    let _ = writeln!(io.stderr, "memriver: not implemented");
    Ok(2)
}

fn dispatch(command: Command, inv: &mut Invocation) -> Outcome {
    match command {
        Command::Project { command } => match command {
            ProjectCommand::Init {
                directory,
                name,
                root,
                yes,
            } => project_commands::init(
                directory.as_deref(),
                name.as_deref(),
                root.as_deref(),
                yes,
                inv,
            ),
            ProjectCommand::Adopt {
                project_id,
                directory,
                root,
                yes,
            } => project_commands::adopt(
                &project_id,
                directory.as_deref(),
                root.as_deref(),
                yes,
                inv,
            ),
            ProjectCommand::Unbind {
                project_id,
                directory,
                root,
                yes,
            } => project_commands::unbind(&project_id, &directory, root.as_deref(), yes, inv),
            ProjectCommand::Explain { project_dir, root } => {
                project_commands::explain(project_dir.as_deref(), root.as_deref(), inv)
            }
        },
        Command::List(args) => views::list(&args, inv),
        Command::Show(args) => views::show(&args, inv),
        Command::Search(args) => views::search(&args, inv),
        Command::Export(args) => views::export(&args, inv),
        Command::Sessions(args) => views::sessions(&args, inv),
        Command::History(args) => memory_commands::history(&args, inv),
        Command::Restore(args) => memory_commands::restore(&args, inv),
        Command::Undo(args) => memory_commands::undo(&args, inv),
        Command::Delete(args) => {
            // --version belongs to the soft delete, --dry-run and --confirm to --hard (the
            // grammar keeps those two apart)
            let second_step = args.dry_run || args.confirm.is_some();
            let wrong = match (args.hard, args.version.is_some()) {
                (true, true) => Some("--version is not accepted with --hard"),
                (false, _) if second_step => Some("--dry-run and --confirm need --hard"),
                (false, false) => Some("--version is required without --hard"),
                _ => None,
            };
            if let Some(message) = wrong {
                let mut delete = Cli::command();
                let delete = delete
                    .find_subcommand_mut("delete")
                    .expect("the grammar has the delete command");
                let error = delete.error(ErrorKind::ArgumentConflict, message);
                return Ok(usage_error(&error, inv.io));
            }
            memory_commands::delete(&args, inv)
        }
        Command::Serve(args) if args.version => {
            inv.say(format_args!("{}\n", env!("CARGO_PKG_VERSION")));
            Ok(0)
        }
        Command::Hook(args) => Ok(hook(&args, inv)),
        Command::Serve(_)
        | Command::Install(_)
        | Command::Uninstall(_)
        | Command::Doctor(_)
        | Command::Dream { .. } => not_implemented(inv.io),
    }
}

/// One hook event over the payload on stdin. Input that cannot be read, or is not UTF-8,
/// is no payload: SessionStart says so, the others do nothing.
fn hook(args: &args::Hook, inv: &mut Invocation) -> i32 {
    let mut payload = String::new();
    if inv.io.stdin.read_to_string(&mut payload).is_err() {
        payload.clear();
    }
    let harness = match args.harness {
        args::SessionHarness::ClaudeCode => Harness::ClaudeCode,
        args::SessionHarness::Codex => Harness::Codex,
    };
    let result = crate::hooks::run_hook(
        args.event,
        harness,
        &payload,
        args.root.as_deref().map(Path::new),
        args.project_dir.as_deref().map(Path::new),
        inv.cwd,
        inv.env,
        inv.home,
    );
    // only what the hook composed: anything else on stdout is read by the harness as a
    // malformed hook response
    let _ = inv.io.stdout.write_all(result.stdout.as_bytes());
    let _ = inv.io.stderr.write_all(result.stderr.as_bytes());
    result.exit_code
}

/// Runs `argv` (the program name first) and returns the exit code.
pub fn run(argv: Vec<String>, env: &Env, home: &Path, cwd: &Path, io: &mut Io) -> i32 {
    let cli = match Cli::try_parse_from(normalize_legacy_serve(argv)) {
        Ok(cli) => cli,
        Err(error) => return usage_error(&error, io),
    };
    if cli.version {
        let _ = writeln!(io.stdout, "{}", env!("CARGO_PKG_VERSION"));
        return 0;
    }
    let Some(command) = cli.command else {
        let error = Cli::command().error(ErrorKind::MissingSubcommand, "a command is required");
        return usage_error(&error, io);
    };
    let mut inv = Invocation { env, home, cwd, io };
    let stopped = match dispatch(command, &mut inv) {
        Ok(code) => return code,
        Err(Stop::NeedsUpgrade(version)) => views::unsupported_store(version),
        // its text names only the file (or the variable) and the fields
        Err(Stop::Settings(error)) => error.to_string(),
        Err(Stop::Unexpected(error)) => format!("unexpected error: {error}"),
    };
    let _ = writeln!(inv.io.stderr, "memriver: {stopped}");
    1
}

#[cfg(test)]
mod tests {
    use memriver_core::models::changes::HardDeletePlan;
    use memriver_core::models::errors::{
        BatchConflictReason, BindingReason, Cap, UndoRefusedReason, ValueError,
    };

    use super::*;

    #[test]
    fn an_unexpected_error_is_named_by_its_kind_and_never_by_what_it_holds() {
        let typed = "an id\nsomebody typed /a/path".to_string();
        let plan = HardDeletePlan {
            target: typed.clone(),
            items: Vec::new(),
            code: typed.clone(),
        };
        // every kind of error the core has, each holding what a caller typed where it can
        let named = [
            (
                CoreError::BatchConflict {
                    index: 0,
                    memory_id: Some(typed.clone()),
                    reason: BatchConflictReason::Version,
                },
                "batch-conflict",
            ),
            (
                CoreError::BindingRefused {
                    reason: BindingReason::BoundElsewhere,
                    project_id: Some(typed.clone()),
                },
                "binding-refused",
            ),
            (
                CoreError::CapExceeded {
                    cap: Cap::TagsMax,
                    memory_id: Some(typed.clone()),
                },
                "cap-exceeded",
            ),
            (
                CoreError::ContentRejected {
                    message: typed.clone(),
                    rule_id: typed.clone(),
                    memory_id: Some(typed.clone()),
                    detail: typed.clone(),
                },
                "content-rejected",
            ),
            (CoreError::GlobalReadOnly, "global-read-only"),
            (CoreError::MemoryNotFound(typed.clone()), "memory-not-found"),
            (CoreError::PlanChanged(plan), "plan-changed"),
            (
                CoreError::ProjectNotFound(typed.clone()),
                "project-not-found",
            ),
            (
                CoreError::ProjectUnavailable {
                    reason: typed.clone(),
                },
                "project-unavailable",
            ),
            (CoreError::SessionMoved, "session-moved"),
            (CoreError::StorageFailure, "storage-failure"),
            (CoreError::StoreNeedsUpgrade(3), "store-needs-upgrade"),
            (
                CoreError::UndoRefused {
                    reason: UndoRefusedReason::Changed,
                    memory_ids: vec![typed.clone()],
                },
                "undo-refused",
            ),
            (
                CoreError::VersionConflict(typed.clone()),
                "version-conflict",
            ),
        ];
        let mut names = vec!["invalid-value", "invalid-name"];
        for (error, name) in named {
            assert_eq!(kind(&error), name, "{error:?}");
            names.push(name);
            // what a command is stopped by is that name, from a read and from a write
            let stops: [Stop; 2] = [error.clone().into(), WriteError::Core(error.clone()).into()];
            for stop in stops {
                match (stop, &error) {
                    (Stop::NeedsUpgrade(3), CoreError::StoreNeedsUpgrade(3)) => {}
                    (Stop::Unexpected(stopped), _) => assert_eq!(stopped, name),
                    (other, _) => panic!("{other:?}"),
                }
            }
        }
        // no two kinds share a name, and a name is a word of the program's own
        let distinct: std::collections::BTreeSet<&str> = names.iter().copied().collect();
        assert_eq!((distinct.len(), names.len()), (16, 16));
        let plain = |name: &&str| name.chars().all(|c| c.is_ascii_lowercase() || c == '-');
        assert!(names.iter().all(plain), "{names:?}");
        let value: Stop = WriteError::Value(ValueError::new(typed)).into();
        assert!(matches!(value, Stop::Unexpected("invalid-value")));
    }

    #[test]
    fn the_form_without_a_command_is_rewritten_to_serve() {
        let rewritten = |args: &[&str]| {
            let argv = args.iter().map(|arg| arg.to_string()).collect();
            normalize_legacy_serve(argv).join(" ")
        };
        assert_eq!(rewritten(&[]), "memriver serve");
        assert_eq!(rewritten(&["memriver"]), "memriver serve");
        assert_eq!(
            rewritten(&["memriver", "--root=R"]),
            "memriver serve --root=R"
        );
        assert_eq!(
            rewritten(&["memriver", "--project-dir", "D"]),
            "memriver serve --project-dir D"
        );
        assert_eq!(rewritten(&["memriver", "--version"]), "memriver --version");
        assert_eq!(rewritten(&["memriver", "--rooted"]), "memriver --rooted");
        assert_eq!(rewritten(&["memriver", "list"]), "memriver list");
    }
}
