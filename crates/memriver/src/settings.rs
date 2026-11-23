//! The settings of the memriver crate: its fixed values and the `[classifier]` table.
//!
//! Every default and fixed value of this crate lives here, with the executor keys the
//! `[dream]` and `[classifier]` tables share -- the Codex provider-override whitelist among
//! them. A table is read on its own, straight from `settings.toml`, with no environment
//! layer; a bad value is core's `SettingsError` naming the field, never the value.
//!
//! Which values a field takes is not decided here: it is what the Python implementation's
//! model accepts, recorded case by case in `tests/fixtures/classifier_matrix.json`.

use std::path::Path;

use memriver_core::models::helpers::{py_isprintable, py_strip};
use memriver_core::settings::{SettingsError, from_file, settings_file};
use toml::de::{DeTable, DeValue};

// the executors
pub const KILL_GRACE_S: u64 = 2; // draining a timed-out run's pipes after the kill
pub const DREAM_SCRATCH_PREFIX: &str = "memriver-dream-"; // a dream run's temporary directories
pub const CLASSIFIER_SCRATCH_PREFIX: &str = "memriver-classifier-"; // a classification's
pub const JEV_BASE_URL: &str = "https://api.typesafe.ai"; // fixed: never read from the environment
pub const DEFAULT_JEV_MODEL: &str = "jev-latest";
pub const DEFAULT_API_KEY_ENV: &str = "TYPESAFE_API_KEY"; // jev: the variable the key is read from

// [classifier]; executor (and executor_path for claude/codex) has no default
pub const CLASSIFIER_TABLE: &str = "classifier";
pub const DEFAULT_HEADLESS_TIMEOUT_S: u64 = 60; // one claude/codex classification
pub const DEFAULT_JEV_TIMEOUT_S: u64 = 10; // one jev request
pub const DEFAULT_BLOCK_THRESHOLD: f64 = 0.7; // jev: block when P(plants instructions) >= this

/// What runs a dream or a classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Executor {
    Claude,
    Codex,
    Jev,
}

/// One Codex override: a text, or the one switch (`requires_openai_auth`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OverrideValue {
    Str(String),
    Bool(bool),
}

/// The Codex provider overrides, each key with its value, in the order of the file.
pub type CodexOverrides = Vec<(String, OverrideValue)>;

/// `[A-Za-z_][A-Za-z0-9_]{0,127}`, the whole text.
fn is_env_name(text: &str) -> bool {
    let rest = |b: u8| b.is_ascii_alphanumeric() || b == b'_';
    matches!(text.as_bytes(), [first, others @ ..]
        if !first.is_ascii_digit() && rest(*first) && others.len() <= 127 && others.iter().copied().all(rest))
}

/// The field of a provider key: `model_providers.<id>.<field>`, the id 1 to 64 characters
/// of `[A-Za-z0-9_-]`, the field one a user may set. With the id.
fn provider_key(key: &str) -> Option<(&str, &str)> {
    let (id, field) = key.strip_prefix("model_providers.")?.split_once('.')?;
    let plain = |b: u8| b.is_ascii_alphanumeric() || b == b'_' || b == b'-';
    let known = [
        "name",
        "base_url",
        "env_key",
        "wire_api",
        "requires_openai_auth",
    ];
    ((1..=64).contains(&id.len()) && id.bytes().all(plain) && known.contains(&field))
        .then_some((id, field))
}

/// Not empty, not only white space, and no character that is not printable: one line.
fn is_one_line(text: &str) -> bool {
    !py_strip(text).is_empty() && text.chars().all(py_isprintable)
}

// The characters whose compatibility form (NFKC, Unicode 16) holds one of `/?#@:`: Python's
// `urlsplit` refuses a host that has one.
const CUT_BY_COMPATIBILITY: [char; 19] = [
    '\u{2047}', '\u{2048}', '\u{2049}', '\u{2100}', '\u{2101}', '\u{2105}', '\u{2106}', '\u{2a74}',
    '\u{fe13}', '\u{fe16}', '\u{fe55}', '\u{fe56}', '\u{fe5f}', '\u{fe6b}', '\u{ff03}', '\u{ff0f}',
    '\u{ff1a}', '\u{ff1f}', '\u{ff20}',
];

/// What may stand between brackets as a host: an IPv6 address, with a zone or without, or
/// the address of a later version (`v<hex>.<anything>`).
fn is_bracketed_host(host: &str) -> bool {
    if let Some(future) = host.strip_prefix('v') {
        let version = future.bytes().take_while(u8::is_ascii_hexdigit).count();
        return version > 0 && future[version..].starts_with('.') && future.len() > version + 1;
    }
    let address = match host.split_once('%') {
        Some((_, zone)) if zone.is_empty() || zone.contains('%') => return false,
        Some((address, _)) => address,
        None => host,
    };
    address.parse::<std::net::Ipv6Addr>().is_ok()
}

/// An http(s) URL with a host and nowhere to hide a credential: no user information, no
/// query, no fragment. The verdict of Python's `urlsplit`, which is cut the same way here:
/// spaces and control characters dropped at the head, the scheme in any case, the host up
/// to the first slash, and a host in brackets only when it is an address.
fn is_plain_url(text: &str) -> bool {
    if text.contains(['?', '#']) {
        return false;
    }
    let url: String = text
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let Some((scheme, rest)) = url.split_once(':') else {
        return false;
    };
    if !scheme.eq_ignore_ascii_case("http") && !scheme.eq_ignore_ascii_case("https") {
        return false;
    }
    let Some(rest) = rest.strip_prefix("//") else {
        return false;
    };
    let netloc = rest.split('/').next().unwrap_or_default();
    if netloc.contains('@') || netloc.contains(CUT_BY_COMPATIBILITY) {
        return false;
    }
    if !netloc.contains(['[', ']']) {
        return !netloc.starts_with(':') && !netloc.is_empty();
    }
    // nothing before the bracket, and after it nothing or a port
    let Some((host, port)) = netloc
        .strip_prefix('[')
        .and_then(|bracketed| bracketed.split_once(']'))
    else {
        return false;
    };
    (port.is_empty() || port.starts_with(':')) && is_bracketed_host(host)
}

/// The whitelisted Codex provider overrides of `value`, or a sentence naming the key and a
/// fixed reason -- never the value, which could be a pasted secret. The keys a user may
/// set are matched by their whole path; everything else -- tools, hooks, MCP servers,
/// whole tables, credential fields -- is refused.
pub fn check_codex_overrides(value: &DeValue<'_>) -> Result<CodexOverrides, String> {
    let DeValue::Table(table) = value else {
        return Err("codex_overrides must be a table".to_string());
    };
    let mut read = CodexOverrides::new();
    let mut provider_ids: Vec<&str> = Vec::new();
    let mut selected = None;
    for (key, item) in table {
        let (key, item) = (key.get_ref().as_ref(), item.get_ref());
        let plain = |b: u8| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-');
        let shown = match (1..=128).contains(&key.len()) && key.bytes().all(plain) {
            true => key,
            false => "a key",
        };
        let provider = provider_key(key);
        let field = match provider {
            Some((_, field)) => field,
            None if key == "model_provider" || key == "model" => key,
            None => return Err(format!("codex_overrides: {shown} is not an allowed key")),
        };
        let kept = match item {
            DeValue::Boolean(flag) if field == "requires_openai_auth" => OverrideValue::Bool(*flag),
            _ if field == "requires_openai_auth" => {
                return Err(format!("codex_overrides: {shown} must be true or false"));
            }
            DeValue::String(text) if is_one_line(text) => {
                if field == "base_url" && !is_plain_url(text) {
                    return Err(format!(
                        "codex_overrides: {shown} must be an http(s) URL without user \
                         information, query or fragment"
                    ));
                }
                if field == "env_key" && !is_env_name(text) {
                    return Err(format!(
                        "codex_overrides: {shown} must name an environment variable"
                    ));
                }
                if field == "wire_api" && text != "responses" {
                    return Err(format!("codex_overrides: {shown} must be \"responses\""));
                }
                if key == "model_provider" {
                    selected = Some(text.as_ref());
                }
                OverrideValue::Str(text.to_string())
            }
            _ => {
                return Err(format!(
                    "codex_overrides: {shown} must be a non-empty single-line string"
                ));
            }
        };
        if let Some((id, _)) = provider {
            provider_ids.push(id);
        }
        read.push((key.to_string(), kept));
    }
    if provider_ids.iter().any(|id| Some(*id) != selected) {
        return Err(
            "codex_overrides: provider keys must define the one provider model_provider selects"
                .to_string(),
        );
    }
    Ok(read)
}

/// The keys that pick and configure an executor, one set for `[dream]` and `[classifier]`:
/// each table adds its own keys to these, so one check names every bad key of the table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecutorSettings {
    pub executor: Executor,
    /// Absolute; always there for claude and codex.
    pub executor_path: Option<String>,
    /// claude --model, codex -c model=, the jev model (jev-latest when unset).
    pub model: Option<String>,
    /// Passed to claude as --settings: authentication only, never a hook. Absolute.
    pub claude_settings: Option<String>,
    /// Provider settings the Codex executor passes as -c overrides.
    pub codex_overrides: CodexOverrides,
    /// jev: the variable's name, never the key.
    pub api_key_env: String,
}

/// The `[classifier]` table: the executor keys, then the classifier's own.
#[derive(Debug, Clone, PartialEq)]
pub struct ClassifierSettings {
    pub executor: Executor,
    pub executor_path: Option<String>,
    pub model: Option<String>,
    pub claude_settings: Option<String>,
    pub codex_overrides: CodexOverrides,
    pub api_key_env: String,
    /// false: off, the table kept.
    pub enabled: bool,
    /// Check what an agent writes.
    pub agent_writes: bool,
    /// Check what a dream writes.
    pub dream_writes: bool,
    pub timeout_s: Option<u64>,
    pub block_threshold: f64,
}

impl ClassifierSettings {
    /// `timeout_s`, else the executor's default.
    pub fn timeout(&self) -> u64 {
        self.timeout_s.unwrap_or(match self.executor {
            Executor::Jev => DEFAULT_JEV_TIMEOUT_S,
            Executor::Claude | Executor::Codex => DEFAULT_HEADLESS_TIMEOUT_S,
        })
    }
}

/// The value of `field` in `table`: keys are matched in any case, and of two spellings of
/// one key the first in the file counts.
fn lookup<'t, 'i>(table: &'t DeTable<'i>, field: &str) -> Option<&'t DeValue<'i>> {
    table
        .iter()
        .find(|(key, _)| key.get_ref().to_lowercase() == field)
        .map(|(_, value)| value.get_ref())
}

/// A key that holds a text: the text, None when the key is absent, an error for any other
/// type -- nothing is turned into a text.
fn text_of<'t>(value: Option<&'t DeValue<'_>>) -> Result<Option<&'t str>, ()> {
    match value {
        None => Ok(None),
        Some(DeValue::String(text)) => Ok(Some(text)),
        Some(_) => Err(()),
    }
}

/// The executor keys of `table`, the invalid ones added to `invalid` in the order of the
/// fields; None when one is.
fn executor_keys(table: &DeTable<'_>, invalid: &mut Vec<String>) -> Option<ExecutorSettings> {
    let before = invalid.len();
    let mut refuse = |field: &str| invalid.push(field.to_string());
    let executor = match text_of(lookup(table, "executor")) {
        Ok(Some("claude")) => Some(Executor::Claude),
        Ok(Some("codex")) => Some(Executor::Codex),
        Ok(Some("jev")) => Some(Executor::Jev),
        _ => {
            refuse("executor");
            None
        }
    };
    let executor_path = match text_of(lookup(table, "executor_path")) {
        Ok(Some(path)) if path.starts_with('/') => Some(path.to_string()),
        // required only for an executor that is known to need it
        Ok(None) if !matches!(executor, Some(Executor::Claude | Executor::Codex)) => None,
        _ => {
            refuse("executor_path");
            None
        }
    };
    let model = match text_of(lookup(table, "model")) {
        Ok(None) => None,
        Ok(Some(model)) if is_one_line(model) => Some(model.to_string()),
        _ => {
            refuse("model");
            None
        }
    };
    let claude_settings = match text_of(lookup(table, "claude_settings")) {
        Ok(None) => None,
        Ok(Some(path)) if path.starts_with('/') => Some(path.to_string()),
        _ => {
            refuse("claude_settings");
            None
        }
    };
    let codex_overrides = match lookup(table, "codex_overrides").map(check_codex_overrides) {
        None => CodexOverrides::new(),
        Some(Ok(overrides)) => overrides,
        Some(Err(_)) => {
            refuse("codex_overrides");
            CodexOverrides::new()
        }
    };
    let api_key_env = match text_of(lookup(table, "api_key_env")) {
        Ok(None) => DEFAULT_API_KEY_ENV.to_string(),
        Ok(Some(name)) if is_env_name(name) => name.to_string(),
        _ => {
            refuse("api_key_env");
            String::new()
        }
    };
    let settings = ExecutorSettings {
        executor: executor?,
        executor_path,
        model,
        claude_settings,
        codex_overrides,
        api_key_env,
    };
    (invalid.len() == before).then_some(settings)
}

/// A raw table validated as the executor keys alone: the keys matched to fields in any
/// case, a key that is not one skipped. The error is the invalid fields, in field order.
pub fn check_executor_table(table: &DeTable<'_>) -> Result<ExecutorSettings, Vec<String>> {
    let mut invalid = Vec::new();
    executor_keys(table, &mut invalid).ok_or(invalid)
}

/// A fraction written as text, as the Python implementation's model reads one: the text
/// without the white space around it, or -- when that is no number -- the text as it is
/// with single underscores inside it dropped. The white space is Rust's here (`trim`), not
/// Python's: that model is itself written in Rust.
fn fraction_from_text(text: &str) -> Option<f64> {
    if let Ok(value) = text.trim().parse() {
        return Some(value);
    }
    if text.starts_with('_') || text.ends_with('_') || text.contains("__") {
        return None;
    }
    text.replace('_', "").parse().ok()
}

/// A threshold: above 0 and at most 1, as a TOML float, the whole number 1, or text.
fn threshold(value: &DeValue<'_>) -> Option<f64> {
    let fraction = match value {
        DeValue::String(text) => fraction_from_text(text)?,
        DeValue::Integer(_) => from_file(value).filter(|whole| *whole == 1).map(|_| 1.0)?,
        DeValue::Float(float) => float.as_str().replace('_', "").parse().ok()?,
        // a boolean is not 0 or 1 here, and a date, an array or a table is no number
        _ => return None,
    };
    (fraction > 0.0 && fraction <= 1.0).then_some(fraction)
}

/// A raw `[classifier]` table validated: its keys matched to fields in any case, the first
/// spelling in the table winning, a key that is not one skipped. The error is the invalid
/// fields, once each: the executor keys first, then the classifier's own.
pub fn check_classifier_table(table: &DeTable<'_>) -> Result<ClassifierSettings, Vec<String>> {
    let mut invalid = Vec::new();
    let keys = executor_keys(table, &mut invalid);
    // strict: 1 or "no" is no boolean
    let mut switch = |field: &str| match lookup(table, field) {
        None => true,
        Some(DeValue::Boolean(flag)) => *flag,
        Some(_) => {
            invalid.push(field.to_string());
            true
        }
    };
    let (enabled, agent_writes, dream_writes) = (
        switch("enabled"),
        switch("agent_writes"),
        switch("dream_writes"),
    );
    let timeout_s = lookup(table, "timeout_s").map(from_file);
    if timeout_s == Some(None) {
        invalid.push("timeout_s".to_string());
    }
    let block_threshold = lookup(table, "block_threshold").map(threshold);
    if block_threshold == Some(None) {
        invalid.push("block_threshold".to_string());
    }
    match keys {
        Some(keys) if invalid.is_empty() => Ok(ClassifierSettings {
            executor: keys.executor,
            executor_path: keys.executor_path,
            model: keys.model,
            claude_settings: keys.claude_settings,
            codex_overrides: keys.codex_overrides,
            api_key_env: keys.api_key_env,
            enabled,
            agent_writes,
            dream_writes,
            timeout_s: timeout_s.flatten(),
            block_threshold: block_threshold.flatten().unwrap_or(DEFAULT_BLOCK_THRESHOLD),
        }),
        _ => Err(invalid),
    }
}

fn refused(unreadable: bool, fields: Vec<String>) -> SettingsError {
    SettingsError {
        unreadable,
        env_vars: Vec::new(),
        fields,
    }
}

/// The `[classifier]` table of `<root>/settings.toml`; None when there is none.
///
/// An unreadable file, bad TOML or an invalid table is core's `SettingsError`: its fields
/// carry the `classifier.` prefix, never a value or the path.
pub fn load_classifier_settings(root: &Path) -> Result<Option<ClassifierSettings>, SettingsError> {
    let Some(text) = settings_file(root)? else {
        return Ok(None);
    };
    // a byte order mark is not TOML to the Python implementation's parser
    if text.starts_with('\u{feff}') {
        return Err(refused(true, Vec::new()));
    }
    // that implementation reads this table from the file as text, where every line ending
    // is a line feed: a carriage return alone ends a line too
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let document = DeTable::parse(&text)
        .map_err(|_| refused(true, Vec::new()))?
        .into_inner();
    // the table's own name is matched as it is written
    let table = document
        .iter()
        .find(|(key, _)| key.get_ref() == CLASSIFIER_TABLE);
    match table.map(|(_, value)| value.get_ref()) {
        None => Ok(None),
        Some(DeValue::Table(table)) => check_classifier_table(table).map(Some).map_err(|fields| {
            let named = |field: &String| format!("{CLASSIFIER_TABLE}.{field}");
            refused(false, fields.iter().map(named).collect())
        }),
        // `classifier = 5`
        Some(_) => Err(refused(false, vec![CLASSIFIER_TABLE.to_string()])),
    }
}
