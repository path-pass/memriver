//! The grammar of the command line: nothing but parsing.

use std::fmt;

use clap::{Args, Parser, Subcommand, ValueEnum};
use memriver_core::models::helpers::py_strip;

const ROOT_HELP: &str = "storage root (default: $MEMRIVER_ROOT or ~/agent-memory)";
/// The digits a number may be written with, as the Python implementation's `int()`.
const DIGITS_MAX: usize = 4300;

/// A whole number above zero, kept as it was written: a version or a limit may be
/// larger than any number of the store, and is still shown as the caller wrote it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PositiveInt(String);

impl PositiveInt {
    /// The number, or the largest one when it is larger.
    pub fn as_i64(&self) -> i64 {
        self.0.parse().unwrap_or(i64::MAX)
    }

    pub fn as_usize(&self) -> usize {
        self.0.parse().unwrap_or(usize::MAX)
    }
}

impl fmt::Display for PositiveInt {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// What Python's `int()` takes off both ends of a number: its own white space, without
/// the four separators U+001C to U+001F that `str.strip()` also takes off.
fn int_strip(written: &str) -> &str {
    let kept = |c: char| ('\u{1c}'..='\u{1f}').contains(&c);
    written.trim_matches(|c: char| !kept(c) && py_strip(c.encode_utf8(&mut [0; 4])).is_empty())
}

/// White space around it, a sign, digits with single underscores between them.
fn positive_int(written: &str) -> Result<PositiveInt, String> {
    let text = int_strip(written);
    let (negative, unsigned) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    let group = |digits: &str| !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit());
    let digits = unsigned.replace('_', "");
    if !unsigned.split('_').all(group) || digits.len() > DIGITS_MAX {
        return Err("not a whole number".to_string());
    }
    let significant = digits.trim_start_matches('0');
    if negative || significant.is_empty() {
        return Err(format!("'{written}' must be a positive integer"));
    }
    Ok(PositiveInt(significant.to_string()))
}

#[derive(Parser, Debug)]
#[command(
    name = "memriver",
    about = "Shared memory MCP server for coding agents",
    disable_version_flag = true,
    infer_long_args = true,
    args_override_self = true
)]
pub struct Cli {
    /// Print the version
    #[arg(long)]
    pub version: bool,
    #[command(subcommand)]
    pub command: Option<Command>,
}

#[derive(Subcommand, Debug)]
pub enum Command {
    /// Run the MCP server over stdio (the default command)
    Serve(Serve),
    /// Run a harness hook over stdin/stdout
    Hook(Hook),
    /// Configure a harness to use memriver
    Install(Install),
    /// Remove a harness's memriver configuration
    Uninstall(Uninstall),
    /// Check the memory store for problems
    Doctor(Doctor),
    /// Register directories as memory projects
    // without a command this is an error like any other, not the help text
    #[command(arg_required_else_help = false)]
    Project {
        #[command(subcommand)]
        command: ProjectCommand,
    },
    /// List every project's memories, or one project's
    List(List),
    /// Show one memory in full
    Show(Show),
    /// Search memories across projects
    Search(Search),
    /// Write a read-only markdown snapshot of every memory
    Export(Export),
    /// List every recorded session, or one project's
    Sessions(Sessions),
    /// Every version of one memory, global included
    History(History),
    /// Make an earlier version's state the current one
    Restore(Restore),
    /// Undo one change, while none of its memories changed since
    Undo(Undo),
    /// Delete one memory: soft (a global one by id, any other from its project's
    /// directory), or --hard with every memory citing it
    Delete(Delete),
    /// Summarize sessions, consolidate memories and retire unused ones, offline and on a
    /// schedule
    // without a command this is an error like any other, not the help text
    #[command(arg_required_else_help = false)]
    Dream {
        #[command(subcommand)]
        command: DreamCommand,
    },
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum AnyHarness {
    ClaudeCode,
    Codex,
    Cursor,
    Kiro,
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionHarness {
    ClaudeCode,
    Codex,
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum HookEvent {
    SessionStart,
    UserPromptSubmit,
    Stop,
    SessionEnd,
    PreToolUse,
}

#[derive(Args, Debug)]
pub struct Serve {
    /// Print the version
    #[arg(long)]
    pub version: bool,
    /// Storage root, which also holds the optional settings.toml (default:
    /// $MEMRIVER_ROOT or ~/agent-memory)
    #[arg(long, allow_negative_numbers = true)]
    pub root: Option<String>,
    /// Directory where project discovery starts (the bound directories decide the
    /// project; default: the current working directory)
    #[arg(long, allow_negative_numbers = true)]
    pub project_dir: Option<String>,
    /// The harness this server is registered with (default: none; the project is decided
    /// by --project-dir)
    #[arg(long, value_enum)]
    pub harness: Option<AnyHarness>,
}

#[derive(Args, Debug)]
pub struct Hook {
    #[arg(value_enum)]
    pub event: HookEvent,
    #[arg(long, value_enum)]
    pub harness: SessionHarness,
    /// Storage root, which also holds the optional settings.toml (default:
    /// $MEMRIVER_ROOT or ~/agent-memory)
    #[arg(long, allow_negative_numbers = true)]
    pub root: Option<String>,
    /// Entry directory registered for a session memriver has not seen yet; an existing
    /// session keeps its project
    #[arg(long, allow_negative_numbers = true)]
    pub project_dir: Option<String>,
}

#[derive(Args, Debug)]
pub struct Install {
    /// Install one harness (default: all of them)
    #[arg(long, value_enum, conflicts_with = "all")]
    pub harness: Option<AnyHarness>,
    /// Install every supported harness (the default)
    #[arg(long)]
    pub all: bool,
    /// Accept every change shown, without prompting
    #[arg(long)]
    pub yes: bool,
    /// Show the plan and write nothing
    #[arg(long)]
    pub dry_run: bool,
}

#[derive(Args, Debug)]
pub struct Uninstall {
    /// Uninstall one harness (default: all of them)
    #[arg(long, value_enum, conflicts_with = "all")]
    pub harness: Option<AnyHarness>,
    /// Uninstall every supported harness (the default)
    #[arg(long)]
    pub all: bool,
    /// Accept every change shown, without prompting
    #[arg(long)]
    pub yes: bool,
    /// Show the plan and write nothing
    #[arg(long)]
    pub dry_run: bool,
    /// Also delete the memory storage root (default: $MEMRIVER_ROOT or ~/agent-memory)
    #[arg(long)]
    pub purge_data: bool,
    /// Storage root to purge with --purge-data (default: $MEMRIVER_ROOT or
    /// ~/agent-memory)
    #[arg(long, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct Doctor {
    /// Storage root to check (default: $MEMRIVER_ROOT or ~/agent-memory)
    #[arg(long, allow_negative_numbers = true)]
    pub root: Option<String>,
    /// Emit a machine-readable JSON report
    #[arg(long)]
    pub json: bool,
    /// Days since a memory was last updated before it is flagged stale (default: 90)
    #[arg(long, value_parser = positive_int)]
    pub stale_days: Option<PositiveInt>,
}

#[derive(Subcommand, Debug)]
pub enum ProjectCommand {
    /// Register a new project rooted at a directory
    Init {
        /// Project root (default: the current working directory)
        #[arg(allow_negative_numbers = true)]
        directory: Option<String>,
        /// Readable project name (default: the directory's name)
        #[arg(long, allow_negative_numbers = true)]
        name: Option<String>,
        /// Memory store root (default: $MEMRIVER_ROOT or ~/agent-memory)
        #[arg(long, allow_negative_numbers = true)]
        root: Option<String>,
        /// Accept the plan shown, without prompting
        #[arg(long)]
        yes: bool,
    },
    /// Bind a directory to an existing project id
    Adopt {
        /// Id of the project to bind to
        #[arg(allow_negative_numbers = true)]
        project_id: String,
        /// Project root (default: the current working directory)
        #[arg(allow_negative_numbers = true)]
        directory: Option<String>,
        /// Memory store root (default: $MEMRIVER_ROOT or ~/agent-memory)
        #[arg(long, allow_negative_numbers = true)]
        root: Option<String>,
        /// Accept the plan shown, without prompting
        #[arg(long)]
        yes: bool,
    },
    /// Remove a root from a project
    Unbind {
        /// Id of the project to edit
        #[arg(allow_negative_numbers = true)]
        project_id: String,
        /// Root to remove, as registered
        #[arg(allow_negative_numbers = true)]
        directory: String,
        /// Memory store root (default: $MEMRIVER_ROOT or ~/agent-memory)
        #[arg(long, allow_negative_numbers = true)]
        root: Option<String>,
        /// Accept the plan shown, without prompting
        #[arg(long)]
        yes: bool,
    },
    /// Show which project a directory resolves to
    Explain {
        /// Directory to explain (default: the current working directory)
        #[arg(long, allow_negative_numbers = true)]
        project_dir: Option<String>,
        /// Memory store root (default: $MEMRIVER_ROOT or ~/agent-memory)
        #[arg(long, allow_negative_numbers = true)]
        root: Option<String>,
    },
}

#[derive(Args, Debug)]
pub struct List {
    /// Only this project id
    #[arg(long, allow_negative_numbers = true)]
    pub project: Option<String>,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct Show {
    #[arg(allow_negative_numbers = true)]
    pub memory_id: String,
    /// Also show a deleted memory
    #[arg(long)]
    pub deleted: bool,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct Search {
    /// Space-separated keywords; an entry matching any keyword is returned, entries
    /// matching more keywords first
    #[arg(allow_negative_numbers = true)]
    pub query: String,
    /// Only this project id
    #[arg(long, allow_negative_numbers = true)]
    pub project: Option<String>,
    #[arg(long, value_parser = positive_int)]
    pub limit: Option<PositiveInt>,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct Export {
    /// A directory that does not exist yet
    #[arg(allow_negative_numbers = true)]
    pub directory: String,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct Sessions {
    /// Space-separated keywords matched against a prompt, summary, branch or entry
    /// directory; an entry matching any keyword is returned, entries matching more
    /// keywords first (none: every session)
    #[arg(default_value = "", allow_negative_numbers = true)]
    pub query: String,
    /// Only this project id
    #[arg(long, allow_negative_numbers = true)]
    pub project: Option<String>,
    #[arg(long, value_parser = positive_int)]
    pub limit: Option<PositiveInt>,
    /// Emit the session_search item shape as JSON
    #[arg(long)]
    pub json: bool,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct History {
    #[arg(allow_negative_numbers = true)]
    pub memory_id: String,
    /// Print version N in full
    #[arg(long, value_name = "N", value_parser = positive_int)]
    pub show: Option<PositiveInt>,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct Restore {
    #[arg(allow_negative_numbers = true)]
    pub memory_id: String,
    /// The version memriver history printed
    #[arg(long = "to", value_name = "N", value_parser = positive_int)]
    pub to_version: PositiveInt,
    /// Confirm without prompting
    #[arg(long)]
    pub yes: bool,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct Undo {
    #[arg(allow_negative_numbers = true)]
    pub change_id: String,
    /// Confirm without prompting
    #[arg(long)]
    pub yes: bool,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(Args, Debug)]
pub struct Delete {
    #[arg(allow_negative_numbers = true)]
    pub memory_id: String,
    /// The version memriver show printed (soft delete only)
    #[arg(long, value_parser = positive_int)]
    pub version: Option<PositiveInt>,
    /// Remove the memory, every memory citing it and all their versions, sources and
    /// reads
    #[arg(long)]
    pub hard: bool,
    /// With --hard: print the plan and its --confirm command
    #[arg(long, conflicts_with = "confirm")]
    pub dry_run: bool,
    /// With --hard: delete the plan --dry-run printed, without prompting
    #[arg(long, value_name = "CODE", allow_negative_numbers = true)]
    pub confirm: Option<String>,
    /// Confirm without prompting
    #[arg(long)]
    pub yes: bool,
    #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
    pub root: Option<String>,
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum DreamExecutor {
    Claude,
    Codex,
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum DreamPhase {
    Summarize,
    Consolidate,
    Extract,
    Curate,
    Retire,
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum DreamTrigger {
    Manual,
    Schedule,
}

#[derive(Subcommand, Debug)]
pub enum DreamCommand {
    /// Configure dream
    Init {
        /// The harness that runs the model (default: the configured one, else claude,
        /// else codex, whichever is on PATH)
        #[arg(long, value_enum)]
        executor: Option<DreamExecutor>,
        /// Days unused before a memory is reviewed for retirement (default: 30; each
        /// recorded read lengthens it)
        #[arg(long, value_parser = positive_int)]
        ttl_days: Option<PositiveInt>,
        /// Confirm without prompting
        #[arg(long)]
        yes: bool,
        #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
        root: Option<String>,
    },
    /// Run dream once now (what the schedule runs)
    Run {
        /// Run only this step after the policy scan
        #[arg(long, value_enum)]
        phase: Option<DreamPhase>,
        #[arg(long, value_enum, default_value = "manual", hide = true)]
        trigger: DreamTrigger,
        #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
        root: Option<String>,
    },
    /// Print a dream run's report
    Report {
        /// The run to show (default: the latest)
        #[arg(allow_negative_numbers = true)]
        run_id: Option<String>,
        /// List the last N runs (10)
        #[arg(long = "list", value_name = "N", num_args = 0..=1, default_missing_value = "10",
              value_parser = positive_int)]
        list_count: Option<PositiveInt>,
        #[arg(long, help = ROOT_HELP, allow_negative_numbers = true)]
        root: Option<String>,
    },
    /// Say how the daily schedule is stopped; settings and data stay
    Uninstall,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(written: &str) -> Option<String> {
        positive_int(written).ok().map(|number| number.to_string())
    }

    #[test]
    fn a_positive_number_is_read_as_pythons_int_reads_it() {
        for (written, number) in [
            ("7", "7"),
            (" 7\t", "7"),
            ("+7", "7"),
            ("007", "7"),
            ("1_000", "1000"),
            ("\u{a0}7\u{3000}", "7"),
            ("\u{b}7\u{c}\r\n", "7"),
            ("99999999999999999999", "99999999999999999999"),
        ] {
            assert_eq!(read(written).as_deref(), Some(number), "{written:?}");
        }
        for written in [
            "0",
            "-1",
            "-0",
            "",
            " ",
            "1.0",
            "1e3",
            "0x10",
            "1__0",
            "_1",
            "1_",
            "+-1",
            "one",
            "1 0",
            "\u{1f}12",
            "\u{1c}1",
            "1\u{1d}",
            "\u{1e}1",
            " \u{1f}1",
            "\u{200b}1",
        ] {
            assert_eq!(read(written), None, "{written:?}");
        }
        assert!(read(&"9".repeat(4300)).is_some());
        assert_eq!(read(&"9".repeat(4301)), None);
    }

    #[test]
    fn a_number_larger_than_any_stored_one_is_the_largest() {
        let huge = positive_int("99999999999999999999").unwrap();
        assert_eq!((huge.as_i64(), huge.as_usize()), (i64::MAX, usize::MAX));
        let small = positive_int("42").unwrap();
        assert_eq!((small.as_i64(), small.as_usize()), (42, 42));
    }

    #[test]
    fn the_grammar_is_consistent() {
        use clap::CommandFactory;
        Cli::command().debug_assert();
    }
}
