//! Harness hooks: five events on the session's row in the store.
//!
//! SessionStart registers the session (or finds its row) and injects the index its row
//! grants; UserPromptSubmit counts and records the prompt, and after a Claude Code `/cd`
//! that moved the session injects the new project's index; Stop decides the save nudge;
//! SessionEnd records the end; Claude Code's PreToolUse records which session makes a
//! memriver tool call and from which directory. Every event names its session by the
//! payload's `session_id`: without a valid one, or without a store, a hook does nothing,
//! and no hook ever creates the store. A store below the schema this version reads is
//! refused the same way.
//!
//! *Never fail the harness.* Every path exits 0 but one: a settings error on the three
//! events that load the settings (one line on stderr, exit 1 -- a failing, non-blocking
//! hook). Only SessionStart writes either of its two fixed, path-free stderr lines; the
//! others fail silently. A panic is caught at the same boundary, and its line is not
//! printed while a hook runs.
//!
//! *Stop and PreToolUse stay light:* neither reads `settings.toml` or the content
//! policy. Both read the `MEMRIVER_*` settings, as the Python implementation does, and
//! do nothing when one is invalid.

pub mod compose;
mod others;
mod session_start;

use std::cell::Cell;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};
use std::sync::Once;

use memriver_core::models::session::{Harness, SessionKey};
use memriver_core::settings::{
    Env, Settings, SettingsError, env_settings, load_settings, located_root, storage_root,
};
use memriver_core::{Services, build_services};
use serde_json::{Map, Value};

pub use crate::cli::args::HookEvent as Event;

pub const INVALID_INPUT: &str = "memriver hook: invalid input\n";
pub const STORE_UNAVAILABLE: &str = "memriver hook: memory store is unavailable\n";

/// What the hook writes and exits with. The default is silent success.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HookResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

impl HookResult {
    fn stdout(value: &Value) -> HookResult {
        HookResult {
            stdout: emit(value),
            ..HookResult::default()
        }
    }

    fn stderr(line: &str) -> HookResult {
        HookResult {
            stderr: line.to_string(),
            ..HookResult::default()
        }
    }

    /// The settings error's one line: it names the file or the variable and the field,
    /// never a value or a path.
    fn settings(error: &SettingsError) -> HookResult {
        HookResult {
            stderr: format!("memriver: {error}\n"),
            exit_code: 1,
            ..HookResult::default()
        }
    }
}

/// Python's `json.dumps(value, ensure_ascii=False)` followed by a line break: `", "`
/// and `": "` between items, characters outside ASCII as they are.
pub fn emit(value: &Value) -> String {
    struct Spaced;
    impl serde_json::ser::Formatter for Spaced {
        fn begin_array_value<W: ?Sized + std::io::Write>(
            &mut self,
            writer: &mut W,
            first: bool,
        ) -> std::io::Result<()> {
            if first {
                Ok(())
            } else {
                writer.write_all(b", ")
            }
        }
        fn begin_object_key<W: ?Sized + std::io::Write>(
            &mut self,
            writer: &mut W,
            first: bool,
        ) -> std::io::Result<()> {
            if first {
                Ok(())
            } else {
                writer.write_all(b", ")
            }
        }
        fn begin_object_value<W: ?Sized + std::io::Write>(
            &mut self,
            writer: &mut W,
        ) -> std::io::Result<()> {
            writer.write_all(b": ")
        }
    }
    let mut out = Vec::new();
    let mut serializer = serde_json::Serializer::with_formatter(&mut out, Spaced);
    serde::Serialize::serialize(value, &mut serializer).expect("a JSON value serializes");
    let mut text = String::from_utf8(out).expect("JSON text is UTF-8");
    text.push('\n');
    text
}

/// The envelope that hands `text` to the agent on SessionStart and UserPromptSubmit;
/// both harnesses take the same object today.
fn additional_context(event_name: &str, text: String) -> Value {
    serde_json::json!({
        "hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}
    })
}

/// The payload as an object, or None: anything that is no JSON object names nothing.
fn object(payload: &str) -> Option<Map<String, Value>> {
    match serde_json::from_str(payload) {
        Ok(Value::Object(object)) => Some(object),
        _ => None,
    }
}

fn text<'a>(payload: &'a Map<String, Value>, name: &str) -> Option<&'a str> {
    payload.get(name).and_then(Value::as_str)
}

/// The payload's session, or None for an id that names none.
fn session_key(harness: Harness, payload: &Map<String, Value>) -> Option<SessionKey> {
    SessionKey::new(harness, text(payload, "session_id")?).ok()
}

/// What one invocation is given besides its event and payload.
pub(crate) struct Place<'a> {
    pub root: Option<&'a Path>,
    pub project_dir: Option<&'a Path>,
    pub cwd: &'a Path,
    pub env: &'a Env,
    pub home: &'a Path,
}

impl Place<'_> {
    /// The services over the configured store, after `settings.toml` and the
    /// `MEMRIVER_*` settings were read.
    fn services(&self) -> Result<Services, SettingsError> {
        let root = self.root.map(|root| self.cwd.join(root));
        let settings = load_settings(root.as_deref(), self.env, self.home)?;
        Ok(self.build(&settings))
    }

    /// The services without `settings.toml`: the defaults and the `MEMRIVER_*`
    /// variables, as the Python implementation's light path reads them. None when a
    /// variable is invalid -- the hook then does nothing, as there.
    fn light_services(&self) -> Option<Services> {
        let root = match self.root {
            Some(root) => located_root(&self.cwd.join(root)),
            None => storage_root(self.env, self.home),
        };
        let settings = env_settings(root, self.env).ok()?;
        Some(self.build(&settings))
    }

    fn build(&self, settings: &Settings) -> Services {
        let services = build_services(settings, self.home, self.env, None);
        #[cfg(test)]
        let services = tests::with_test_seams(services, &settings.root, self);
        services
    }

    /// The registration entry: `--project-dir` > Claude Code's `CLAUDE_PROJECT_DIR` when
    /// absolute > the payload's `cwd` > the process's. Only a session with no row uses
    /// it. A relative one is read from the working directory.
    fn entry_dir(&self, harness: Harness, payload: &Map<String, Value>) -> String {
        let claude = match harness {
            Harness::ClaudeCode => self
                .env
                .get("CLAUDE_PROJECT_DIR")
                .filter(|dir| Path::new(dir).is_absolute())
                .map(PathBuf::from),
            Harness::Codex => None,
        };
        let entry = match (self.project_dir, claude, text(payload, "cwd")) {
            (Some(dir), _, _) => self.cwd.join(dir),
            (None, Some(dir), _) => dir,
            (None, None, Some(dir)) => self.cwd.join(dir),
            (None, None, None) => self.cwd.to_path_buf(),
        };
        entry.to_string_lossy().into_owned()
    }
}

thread_local! {
    /// Set while a hook runs on this thread: a panic then prints nothing.
    static QUIET: Cell<bool> = const { Cell::new(false) };
}

/// Runs `body` with the panic line silenced on this thread. A failed check of a prompt
/// is caught inside the session service and recorded as `scan-error`; the default
/// panic hook would still write its line to stderr first, which UserPromptSubmit must
/// never do. Other threads keep their panic lines.
fn quietly<T>(body: impl FnOnce() -> T) -> T {
    static INSTALL: Once = Once::new();
    INSTALL.call_once(|| {
        let previous = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            if !QUIET.with(Cell::get) {
                previous(info);
            }
        }));
    });
    let was = QUIET.with(|quiet| quiet.replace(true));
    let done = body();
    QUIET.with(|quiet| quiet.set(was));
    done
}

/// Runs one hook event. Returns what to write and the exit code; never panics.
#[allow(clippy::too_many_arguments)]
pub fn run_hook(
    event: Event,
    harness: Harness,
    payload: &str,
    root: Option<&Path>,
    project_dir: Option<&Path>,
    cwd: &Path,
    env: &Env,
    home: &Path,
) -> HookResult {
    let place = Place {
        root,
        project_dir,
        cwd,
        env,
        home,
    };
    let run = || match event {
        Event::SessionStart => session_start::session_start(harness, payload, &place),
        Event::UserPromptSubmit => others::user_prompt_submit(harness, payload, &place),
        Event::Stop => others::stop(harness, payload, &place),
        Event::SessionEnd => others::session_end(harness, payload, &place),
        Event::PreToolUse => others::pre_tool_use(harness, payload, &place),
    };
    match quietly(|| catch_unwind(AssertUnwindSafe(run))) {
        Ok(result) => result,
        // the reason belongs in `memriver doctor`, never in the session
        Err(_) if event == Event::SessionStart => HookResult::stderr(STORE_UNAVAILABLE),
        Err(_) => HookResult::default(),
    }
}

#[cfg(test)]
mod tests;
