//! Every default and limit of this crate, and how settings are loaded.
//!
//! Settings are the top level of `<root>/settings.toml` and the `MEMRIVER_*` environment.
//! Precedence: an override of the root > environment > file > defaults. Other crates read
//! their own table of the same file; this one ignores every key and table it does not own.

use std::collections::HashMap;
use std::fmt;
use std::path::{Path, PathBuf};

use toml::de::{DeTable, DeValue};

use crate::content_policy::rule_cache::CachePolicy;

/// A process environment, handed down from the binary's entry point.
pub type Env = HashMap<String, String>;

pub const ENV_PREFIX: &str = "MEMRIVER_";
pub const SETTINGS_FILENAME: &str = "settings.toml";

// The defaults of the settings a user can change, each behind a field of `Settings`.
pub const DEFAULT_MAX_BODY_CHARS: usize = 8000;
pub const DEFAULT_SEARCH_LIMIT_MAX: usize = 50;
pub const DEFAULT_SEARCH_LIMIT: usize = 5;
pub const DEFAULT_BUDGET_LINES: usize = 100;
// the metadata limits every write keeps
pub const DEFAULT_STANDING_MAX_COUNT: usize = 10; // level-1 entries of one project
pub const DEFAULT_STANDING_MAX_CHARS: usize = 200; // the body of one level-1 entry
pub const DEFAULT_TAGS_MAX: usize = 12; // distinct tag names of one project
pub const DEFAULT_TAGS_PER_MEMORY_MAX: usize = 3; // tag names of one memory

// Fixed values a user cannot change. They live here with every other default of this
// crate, never in the modules that use them.
pub const INDEX_CUE_CHARS: usize = 60; // one index line's cue
pub const SEARCH_SNIPPET_CHARS: usize = 60; // one search hit's body snippet
pub const HEADER_FIELD_CHARS: usize = 120; // one field of the project header
pub const PROJECT_NAME_MAX_CHARS: usize = 120; // a project name
pub const BUSY_TIMEOUT_MS: u64 = 5000; // SQLite's bounded wait for the write lock
// memories one DELETE of a hard delete names: far below the 32,766 values a statement takes
pub const HARD_DELETE_CHUNK: usize = 500;
// harness sessions
pub const SESSION_PROMPT_CHARS: usize = 512; // one recorded prompt's text
pub const SESSION_RECENT_PROMPTS: usize = 5; // prompts kept per session, newest last
pub const SESSION_PROMPT_SCAN_MAX_BYTES: usize = 65536; // a larger prompt is omitted unscanned
pub const STOP_NUDGE_MIN_PROMPTS: i64 = 5; // unsaved prompts before the first Stop nudge
pub const STOP_NUDGE_INTERVAL_PROMPTS: i64 = 5; // prompts between two Stop nudges
pub const GIT_QUERY_TIMEOUT_S: u64 = 2; // one git call mapping a worktree
// a larger file of a repository (config, packed-refs, a ref) is not read here: git is asked
pub const GIT_FILE_MAX_BYTES: usize = 4 * 1024 * 1024;
pub const SESSION_SEARCH_LIMIT_DEFAULT: usize = 10;
pub const SESSION_SEARCH_LIMIT_MAX: usize = 50;
pub const TOOL_CALL_RETENTION_S: i64 = 3600; // how long a call -> session mapping is kept
pub const SESSION_SUMMARY_MAX_CHARS: usize = 1200; // one published session summary
// compiled content-policy rules kept between checks; chosen by measurement
pub const POLICY_RULE_CACHE: CachePolicy = CachePolicy::None;

/// The settings file or a `MEMRIVER_*` variable cannot be used; nothing was loaded.
///
/// Fields only: `fields` names the fields of the file that are invalid, `env_vars` the
/// variables that are, and `unreadable` says the file could not be read at all. Its text is
/// one line safe to print: never a path and never the rejected value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SettingsError {
    pub unreadable: bool,
    pub env_vars: Vec<String>,
    pub fields: Vec<String>,
}

impl SettingsError {
    fn unreadable() -> SettingsError {
        SettingsError {
            unreadable: true,
            env_vars: Vec::new(),
            fields: Vec::new(),
        }
    }
}

impl fmt::Display for SettingsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut parts = Vec::new();
        if self.unreadable {
            parts.push(format!("{SETTINGS_FILENAME} could not be read"));
        }
        if !self.env_vars.is_empty() {
            parts.push(format!(
                "environment variable {} is invalid",
                self.env_vars.join(", ")
            ));
        }
        if !self.fields.is_empty() {
            parts.push(format!(
                "{SETTINGS_FILENAME} is invalid: field {}",
                self.fields.join(", ")
            ));
        }
        if parts.is_empty() {
            // an error that names no field still is one, never "could not be read"
            parts.push("the settings are invalid".to_string());
        }
        f.write_str(&parts.join("; "))
    }
}

impl std::error::Error for SettingsError {}

/// `path` made absolute from the working directory and normalized by its text alone, as
/// Python's `os.path.abspath` does: no link is read, `..` drops the name before it.
pub fn lexical_absolute(path: &Path) -> std::io::Result<PathBuf> {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let joined = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let normal = crate::models::helpers::py_normpath(joined.as_os_str().as_bytes());
    Ok(PathBuf::from(std::ffi::OsString::from_vec(normal)))
}

/// The one place a root names, however it is written. The settings file and the
/// database are both found from it, so a root that reaches a directory by its text only
/// (`missing/../store`) cannot have its database used and its settings passed by.
///
/// When the working directory cannot be determined (it was removed), a relative root
/// cannot be made absolute and stays as written. The two then part: the settings file is
/// still looked for at that relative path, and the file system may find it (`../store`
/// does); every database operation on the same root is a storage failure. No store is
/// read or written under settings that are not its own, and the binary refuses to start
/// without a working directory before either is reached.
pub fn located_root(root: &Path) -> PathBuf {
    lexical_absolute(root).unwrap_or_else(|_| root.to_path_buf())
}

/// The store's root: `MEMRIVER_ROOT` when it is set and not empty, or
/// `<home>/agent-memory`; made absolute and normalized by its text (`located_root`).
pub fn storage_root(env: &Env, home: &Path) -> PathBuf {
    match env.get("MEMRIVER_ROOT") {
        Some(root) if !root.is_empty() => located_root(Path::new(root)),
        _ => located_root(&home.join("agent-memory")),
    }
}

/// The text of `<root>/settings.toml`, the root taken as `located_root` gives it, or None
/// only when the path is really absent.
/// Anything else that is not a readable regular file of UTF-8 text — a directory, a FIFO,
/// a link that dangles or loops, a root that cannot be searched — is an error: an
/// unreadable file never means "no settings". The type is checked before the file is
/// opened, so a FIFO never blocks.
pub fn settings_file(root: &Path) -> Result<Option<String>, SettingsError> {
    use std::io::ErrorKind::NotFound;
    let path = located_root(root).join(SETTINGS_FILENAME);
    match std::fs::metadata(&path) {
        Ok(found) if found.is_file() => {}
        // a link whose target is missing is broken, not absent
        Err(error) if error.kind() == NotFound => {
            return match std::fs::symlink_metadata(&path) {
                Err(error) if error.kind() == NotFound => Ok(None),
                _ => Err(SettingsError::unreadable()),
            };
        }
        _ => return Err(SettingsError::unreadable()),
    }
    let bytes = std::fs::read(&path).map_err(|_| SettingsError::unreadable())?;
    String::from_utf8(bytes)
        .map(Some)
        .map_err(|_| SettingsError::unreadable())
}

/// The behaviour a user can set. Every default reproduces what memriver does unconfigured.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settings {
    pub root: PathBuf,
    pub max_body_chars: usize,
    pub search_limit_max: usize,
    pub search_limit_default: usize,
    pub index_budget_lines: usize,
    /// None keeps every recorded read; a number of days lets older ones be pruned.
    pub memory_reads_retention_days: Option<u32>,
    pub standing_max_count: usize,
    pub standing_max_chars: usize,
    pub tags_max: usize,
    pub tags_per_memory_max: usize,
}

impl Settings {
    /// The defaults for `root`: no file and no environment is read.
    pub fn light(root: PathBuf) -> Settings {
        Settings {
            root,
            max_body_chars: DEFAULT_MAX_BODY_CHARS,
            search_limit_max: DEFAULT_SEARCH_LIMIT_MAX,
            search_limit_default: DEFAULT_SEARCH_LIMIT,
            index_budget_lines: DEFAULT_BUDGET_LINES,
            memory_reads_retention_days: None,
            standing_max_count: DEFAULT_STANDING_MAX_COUNT,
            standing_max_chars: DEFAULT_STANDING_MAX_CHARS,
            tags_max: DEFAULT_TAGS_MAX,
            tags_per_memory_max: DEFAULT_TAGS_PER_MEMORY_MAX,
        }
    }
}

// The fields in the order they are judged and named in an error.
const FIELDS: [&str; 9] = [
    "max_body_chars",
    "search_limit_max",
    "search_limit_default",
    "index_budget_lines",
    "memory_reads_retention_days",
    "standing_max_count",
    "standing_max_chars",
    "tags_max",
    "tags_per_memory_max",
];
const MAX: usize = 1;
const DEFAULT: usize = 2;

// A number written as text may have this many digits, leading zeros aside.
const TEXT_DIGITS_MAX: usize = 4300;

// Every setting is a count or a budget: a whole number above zero. Which spellings are
// one is not decided here: it is what the Python implementation's settings model accepts,
// recorded cell by cell in tests/fixtures/settings_matrix.json. A number larger than a
// field holds is accepted, as there, and becomes the largest the field holds: each of them
// is an upper bound, so that is the same as "no bound".

/// A number written as text, in the file or in a variable: white space around it, a sign,
/// digits with single underscores between them, and at most a fraction of zeros. Text that
/// starts with a zero is read after the zeros and underscores at its head, any number of
/// them: `0__1` is 1, though `1__0` is nothing.
fn from_text(text: &str) -> Option<u64> {
    let text = text.trim();
    let (negative, unsigned) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    let (whole, fraction) = match unsigned.split_once('.') {
        Some((whole, fraction)) => (whole, Some(fraction)),
        None => (unsigned, None),
    };
    if fraction.is_some_and(|zeros| zeros.is_empty() || zeros.bytes().any(|b| b != b'0')) {
        return None;
    }
    let digits = |group: &str| !group.is_empty() && group.bytes().all(|b| b.is_ascii_digit());
    let significant = match whole.strip_prefix('0') {
        // nothing is left of a zero, and zero is no setting
        Some(rest) => rest.trim_start_matches(['0', '_']),
        None => whole,
    };
    if negative || !significant.split('_').all(digits) {
        return None;
    }
    let significant = significant.replace('_', "");
    if significant.len() > TEXT_DIGITS_MAX {
        return None;
    }
    Some(significant.parse().unwrap_or(u64::MAX))
}

/// A TOML integer as written: decimal with an optional sign, or hexadecimal, octal or
/// binary. Of any length.
fn from_integer(written: &str, radix: u32) -> Option<u64> {
    let unsigned = written.strip_prefix('+').unwrap_or(written);
    let mut value: u64 = 0;
    for symbol in unsigned.chars().filter(|symbol| *symbol != '_') {
        // a minus sign is no digit: nothing below zero is a setting, and neither is -0
        let digit = u64::from(symbol.to_digit(radix)?);
        value = value
            .checked_mul(u64::from(radix))
            .and_then(|shifted| shifted.checked_add(digit))
            .unwrap_or(u64::MAX);
    }
    (value > 0).then_some(value)
}

/// A TOML float as written: only one that is a whole number below 2^63.
fn from_float(written: &str) -> Option<u64> {
    let value: f64 = written.replace('_', "").parse().ok()?;
    let whole = value.is_finite() && value.fract() == 0.0;
    (whole && (1.0..9_223_372_036_854_775_808.0).contains(&value)).then_some(value as u64)
}

fn from_file(value: &DeValue<'_>) -> Option<u64> {
    match value {
        DeValue::String(text) => from_text(text),
        DeValue::Integer(integer) => from_integer(integer.as_str(), integer.radix()),
        DeValue::Float(float) => from_float(float.as_str()),
        // a boolean is not 0 or 1 here, and a date, an array or a table is no number
        _ => None,
    }
}

/// The variable that sets `field`, if any. Names are matched in any case; of two names
/// that differ only in case, the one in capitals counts, and without it the one that
/// sorts first.
fn from_env<'e>(env: &'e Env, field: &str) -> Option<&'e str> {
    let lower = format!("{ENV_PREFIX}{field}").to_lowercase();
    let named = |name: &&String| name.to_lowercase() == lower;
    let name = env
        .keys()
        .filter(named)
        .min_by_key(|name| (**name != lower.to_uppercase(), *name))?;
    Some(&env[name])
}

fn env_var(field: &str) -> String {
    format!("{ENV_PREFIX}{field}").to_uppercase()
}

/// Root override > environment > `<root>/settings.toml` > defaults, or an error.
///
/// The root is resolved first, so the settings file can live inside the store it
/// configures, and a `root` key in the file never moves it. The environment and the file
/// are judged together, once: a field valid only once both combine (a variable lowers the
/// max, the file lowers the default under it) is accepted. An unreadable file, bad TOML or
/// an invalid value is an error: nothing falls back to the defaults, so a typo is never
/// silently ignored.
pub fn load_settings(
    root_override: Option<&Path>,
    env: &Env,
    home: &Path,
) -> Result<Settings, SettingsError> {
    let root = root_override.map_or_else(|| storage_root(env, home), located_root);
    let text = settings_file(&root)?.unwrap_or_default();
    // a byte order mark is not TOML to the Python implementation's parser
    if text.starts_with('\u{feff}') {
        return Err(SettingsError::unreadable());
    }
    let table = DeTable::parse(&text)
        .map_err(|_| SettingsError::unreadable())?
        .into_inner();
    // keys are matched in any case; of two spellings of one key the first in the file counts
    let in_file = |field: &str| {
        table
            .iter()
            .find(|(key, _)| key.get_ref().to_lowercase() == field)
            .map(|(_, value)| value)
    };
    let env_set = |field: &str| env.keys().any(|name| name.to_uppercase() == env_var(field));

    let mut values: [Option<u64>; 9] = [None; 9];
    let mut invalid: Vec<&str> = Vec::new();
    for (at, field) in FIELDS.into_iter().enumerate() {
        let written = match (from_env(env, field), in_file(field)) {
            (Some(text), _) => from_text(text),
            (None, Some(value)) => from_file(value.get_ref()),
            (None, None) => continue,
        };
        match written {
            Some(value) => values[at] = Some(value),
            None => invalid.push(field),
        }
    }
    let wide = |at: usize, default: usize| match values[at] {
        Some(value) => usize::try_from(value).unwrap_or(usize::MAX),
        None => default,
    };
    let settings = Settings {
        root,
        max_body_chars: wide(0, DEFAULT_MAX_BODY_CHARS),
        search_limit_max: wide(MAX, DEFAULT_SEARCH_LIMIT_MAX),
        search_limit_default: wide(DEFAULT, DEFAULT_SEARCH_LIMIT),
        index_budget_lines: wide(3, DEFAULT_BUDGET_LINES),
        memory_reads_retention_days: values[4].map(|days| u32::try_from(days).unwrap_or(u32::MAX)),
        standing_max_count: wide(5, DEFAULT_STANDING_MAX_COUNT),
        standing_max_chars: wide(6, DEFAULT_STANDING_MAX_CHARS),
        tags_max: wide(7, DEFAULT_TAGS_MAX),
        tags_per_memory_max: wide(8, DEFAULT_TAGS_PER_MEMORY_MAX),
    };
    // a default above the max would make the common case, a search without a limit, ask
    // for more than the store ever returns; judged only when both are valid themselves
    if !invalid.contains(&FIELDS[MAX])
        && !invalid.contains(&FIELDS[DEFAULT])
        && settings.search_limit_default > settings.search_limit_max
    {
        invalid.push(FIELDS[DEFAULT]);
        invalid.sort_by_key(|field| FIELDS.iter().position(|known| known == field));
    }
    if invalid.is_empty() {
        return Ok(settings);
    }
    // Each invalid field is blamed on the source that set it: its variable, else the file
    // when the file holds it. A default nobody set fails only against the max, and is
    // blamed on whoever set the max, by that name.
    let mut error = SettingsError {
        unreadable: false,
        env_vars: Vec::new(),
        fields: Vec::new(),
    };
    for mut field in invalid {
        let held = |field: &str| in_file(field).is_some();
        if field == FIELDS[DEFAULT] && !env_set(field) && !held(field) {
            field = FIELDS[MAX];
        }
        let (names, name) = match env_set(field) || !held(field) {
            true => (&mut error.env_vars, env_var(field)),
            false => (&mut error.fields, field.to_string()),
        };
        if !names.contains(&name) {
            names.push(name);
        }
    }
    Err(error)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_catalogue_has_the_values_of_the_python_one() {
        assert_eq!(
            (ENV_PREFIX, SETTINGS_FILENAME),
            ("MEMRIVER_", "settings.toml")
        );
        assert_eq!(
            (
                DEFAULT_MAX_BODY_CHARS,
                DEFAULT_SEARCH_LIMIT_MAX,
                DEFAULT_SEARCH_LIMIT,
                DEFAULT_BUDGET_LINES
            ),
            (8000, 50, 5, 100)
        );
        assert_eq!(
            (
                DEFAULT_STANDING_MAX_COUNT,
                DEFAULT_STANDING_MAX_CHARS,
                DEFAULT_TAGS_MAX,
                DEFAULT_TAGS_PER_MEMORY_MAX
            ),
            (10, 200, 12, 3)
        );
        assert_eq!(
            (
                INDEX_CUE_CHARS,
                SEARCH_SNIPPET_CHARS,
                HEADER_FIELD_CHARS,
                PROJECT_NAME_MAX_CHARS,
                BUSY_TIMEOUT_MS
            ),
            (60, 60, 120, 120, 5000)
        );
        assert_eq!(
            (
                SESSION_PROMPT_CHARS,
                SESSION_RECENT_PROMPTS,
                SESSION_PROMPT_SCAN_MAX_BYTES
            ),
            (512, 5, 65536)
        );
        assert_eq!(
            (
                STOP_NUDGE_MIN_PROMPTS,
                STOP_NUDGE_INTERVAL_PROMPTS,
                GIT_QUERY_TIMEOUT_S
            ),
            (5, 5, 2)
        );
        assert_eq!(
            (
                SESSION_SEARCH_LIMIT_DEFAULT,
                SESSION_SEARCH_LIMIT_MAX,
                TOOL_CALL_RETENTION_S,
                SESSION_SUMMARY_MAX_CHARS
            ),
            (10, 50, 3600, 1200)
        );
        // the defaults of the fields are the catalogue's
        let light = Settings::light(std::path::PathBuf::from("/r"));
        assert_eq!(
            (
                light.max_body_chars,
                light.search_limit_max,
                light.search_limit_default,
                light.index_budget_lines
            ),
            (
                DEFAULT_MAX_BODY_CHARS,
                DEFAULT_SEARCH_LIMIT_MAX,
                DEFAULT_SEARCH_LIMIT,
                DEFAULT_BUDGET_LINES
            )
        );
        assert_eq!(
            (
                light.standing_max_count,
                light.standing_max_chars,
                light.tags_max,
                light.tags_per_memory_max
            ),
            (
                DEFAULT_STANDING_MAX_COUNT,
                DEFAULT_STANDING_MAX_CHARS,
                DEFAULT_TAGS_MAX,
                DEFAULT_TAGS_PER_MEMORY_MAX
            )
        );
        assert_eq!(light.memory_reads_retention_days, None);
    }
}
