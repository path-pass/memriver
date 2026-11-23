//! Read-only views of the store for people: `list`, `show`, `search`, `sessions`,
//! `export`.
//!
//! Every read is the core's management view (all projects); every rule is the core's.
//! This module renders and words refusals, nothing else.

use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::fs::File;
use std::io::{self, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

use memriver_core::models::errors::CoreError;
use memriver_core::models::helpers::{LINE_BREAKS, cut, now, parse_timestamp, single_line};
use memriver_core::models::memory::Memory;
use memriver_core::models::project::Project;
use memriver_core::models::session::{
    Harness, PromptBody, PromptEntry, Session, SessionMove, SessionStatus,
};
use memriver_core::repository::directories::{strerror, tidied};
use memriver_core::settings::INDEX_CUE_CHARS;
use serde_json::{Map, Value, json};
use unicode_properties::{GeneralCategory, UnicodeGeneralCategory};

use super::args::{Export, List, Search, Sessions, Show};
use super::{Invocation, Outcome, services};

pub const STORE_UNREADABLE: &str = "refused: the memory store could not be read";

/// The one line every command prints for a store below the schema this version reads.
/// The MCP server words its own, for the agent.
pub fn unsupported_store(version: i64) -> String {
    format!(
        "the memory store is at schema version {version}; this memriver needs schema version \
         6. Upgrade the store to version 6 with an earlier memriver release first"
    )
}

// What is replaced before a store- or path-derived string is printed: the controls, the
// format controls (a right-to-left override reorders the line a terminal draws), and the
// line and paragraph separators.
fn invisible(c: char) -> bool {
    matches!(
        c.general_category(),
        GeneralCategory::Control
            | GeneralCategory::Format
            | GeneralCategory::Surrogate
            | GeneralCategory::LineSeparator
            | GeneralCategory::ParagraphSeparator
    )
}

/// `text` with every invisible character replaced by one space, one for one. Ordinary
/// spaces are never collapsed or trimmed: a path is shown as it is spelled.
pub fn visible(text: &str) -> String {
    text.chars()
        .map(|c| if invisible(c) { ' ' } else { c })
        .collect()
}

/// The body for a terminal: newlines and tabs kept, every other invisible character a
/// space.
pub fn body(text: &str) -> String {
    text.chars()
        .map(|c| match c {
            '\n' | '\t' => c,
            c if invisible(c) => ' ',
            c => c,
        })
        .collect()
}

/// A memory in a few words: its description, or the first line of its body.
pub fn cue(description: &str, body: &str) -> String {
    let raw = match description {
        "" => body.split(LINE_BREAKS).next().unwrap_or_default(),
        description => description,
    };
    cut(&visible(&single_line(raw)), INDEX_CUE_CHARS).to_string()
}

pub fn memory_line(memory: &Memory) -> String {
    // updated is stored text like any other field: neutralised before it is shown
    format!(
        "  {}  [{}]  {}  {}\n",
        memory.id,
        memory.type_,
        cut(&visible(&memory.updated), 10),
        cue(&memory.description, &memory.body)
    )
}

/// The first 255 characters of an id a caller typed, safe to print.
pub(crate) fn echoed(raw: &str) -> String {
    visible(cut(raw, 255))
}

fn place(project: &Project, global_id: Option<&str>) -> String {
    match &project.root {
        _ if Some(project.id.as_str()) == global_id => "global".to_string(),
        Some(root) if !root.is_empty() => visible(root),
        _ => "no directory".to_string(),
    }
}

fn no_such_project(project_id: Option<&str>, inv: &mut Invocation) -> Outcome {
    let shown = echoed(project_id.unwrap_or_default());
    inv.say(format_args!("no such project: {shown}\n"));
    Ok(2)
}

fn unreadable(inv: &mut Invocation) -> Outcome {
    inv.say(format_args!("{STORE_UNREADABLE}\n"));
    Ok(2)
}

pub(crate) fn list(args: &List, inv: &mut Invocation) -> Outcome {
    let (services, _) = services(args.root.as_deref(), inv)?;
    let read = || -> Result<_, CoreError> {
        let listed = services.memory.list_memories(args.project.as_deref())?;
        Ok((listed, services.project.global_project_id()?))
    };
    let (listed, global_id) = match read() {
        Ok(read) => read,
        Err(CoreError::ProjectNotFound(_)) => return no_such_project(args.project.as_deref(), inv),
        Err(CoreError::StorageFailure) => return unreadable(inv),
        Err(other) => return Err(other.into()),
    };
    for (project, memories) in listed {
        inv.say(format_args!(
            "{}  {}  ({})\n",
            project.id,
            visible(&project.name),
            place(&project, global_id.as_deref())
        ));
        if memories.is_empty() {
            inv.say(format_args!("  (no memories)\n"));
        }
        for memory in &memories {
            inv.say(format_args!("{}", memory_line(memory)));
        }
    }
    Ok(0)
}

pub(crate) fn show(args: &Show, inv: &mut Invocation) -> Outcome {
    let (services, _) = services(args.root.as_deref(), inv)?;
    let memory = match services.memory.show(&args.memory_id, args.deleted) {
        Ok(memory) => memory,
        Err(CoreError::MemoryNotFound(_)) => {
            inv.say(format_args!(
                "no such memory: {}\n",
                echoed(&args.memory_id)
            ));
            return Ok(2);
        }
        Err(CoreError::StorageFailure) => return unreadable(inv),
        Err(other) => return Err(other.into()),
    };
    let mut lines = vec![
        format!("id: {}", memory.id),
        format!("project: {}", memory.project_id),
        format!("type: {}", memory.type_),
        format!(
            "source: {}/{}",
            visible(&memory.source_harness),
            visible(&memory.source_method)
        ),
        format!("trust: {}", memory.trust),
        format!("sync: {}", memory.sync),
        format!("created: {}", visible(&memory.created)),
        format!("updated: {}", visible(&memory.updated)),
        format!("version: {}", memory.version),
        format!(
            "last_read_at: {}",
            match memory.last_read_at.as_deref() {
                Some(at) if !at.is_empty() => visible(at),
                _ => "never".to_string(),
            }
        ),
        format!(
            "description: {}",
            visible(&single_line(&memory.description))
        ),
    ];
    if let Some(deleted_at) = &memory.deleted_at {
        lines.push(format!("deleted: {}", visible(deleted_at)));
    }
    inv.say(format_args!(
        "{}\n---\n{}\n",
        lines.join("\n"),
        body(&memory.body)
    ));
    Ok(0)
}

pub(crate) fn search(args: &Search, inv: &mut Invocation) -> Outcome {
    let (services, _) = services(args.root.as_deref(), inv)?;
    let limit = args.limit.as_ref().map(|limit| limit.as_usize());
    let hits = match services
        .memory
        .search_all(&args.query, args.project.as_deref(), limit)
    {
        Ok(hits) => hits,
        Err(CoreError::ProjectNotFound(_)) => return no_such_project(args.project.as_deref(), inv),
        Err(CoreError::StorageFailure) => return unreadable(inv),
        Err(other) => return Err(other.into()),
    };
    if hits.is_empty() {
        inv.say(format_args!("(no matches)\n"));
    }
    for memory in &hits {
        inv.say(format_args!("{}", memory_line(memory)));
    }
    Ok(0)
}

// --- sessions ---

/// Python's `shlex.quote`.
pub fn shell_quote(text: &str) -> String {
    let safe = |c: char| c.is_ascii_alphanumeric() || "@%+=:,./-_".contains(c);
    if !text.is_empty() && text.chars().all(safe) {
        return text.to_string();
    }
    format!("'{}'", text.replace('\'', "'\"'\"'"))
}

fn prompt_payload(entry: Option<&PromptEntry>) -> Value {
    match entry.map(|entry| (entry.at(), entry.body())) {
        None => Value::Null,
        Some((at, PromptBody::Text(text))) => json!({"at": at, "text": text}),
        Some((at, PromptBody::Omitted(why))) => json!({"at": at, "omitted": why.as_str()}),
    }
}

fn resume_command(session: &Session) -> String {
    let command = match session.key.harness() {
        Harness::ClaudeCode => "claude --resume",
        Harness::Codex => "codex resume",
    };
    format!("{command} {}", shell_quote(session.key.session_id()))
}

/// The item `session_search` and `memriver sessions --json` share: an agent and a
/// person's script read the same keys off the same session.
pub fn session_item(session: &Session) -> Map<String, Value> {
    let recent: Vec<Value> = session
        .recent_prompts
        .iter()
        .map(|entry| prompt_payload(Some(entry)))
        .collect();
    let item = json!({
        "harness": session.key.harness().as_str(),
        "session_id": session.key.session_id(),
        "project": session.project_id,
        "branch": session.branch,
        "entry_cwd": session.entry_cwd,
        "first_recorded": session.started_at,
        "last_active_at": session.last_active_at,
        "last_end_event_at": session.ended_at,
        "first_prompt": prompt_payload(session.first_prompt.as_ref()),
        "recent_prompts": recent,
        "resume_command": resume_command(session),
        "summary": session.summary,
        "summary_at": session.summary_at,
    });
    match item {
        Value::Object(item) => item,
        _ => unreachable!("the item is written as an object"),
    }
}

/// A string as Python's `json.dumps` writes it: everything outside ASCII escaped.
fn json_string(text: &str, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            c => {
                for unit in c.encode_utf16(&mut [0; 2]) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn json_value(value: &Value, indent: Option<usize>, depth: usize, out: &mut String) {
    let line = |depth: usize, out: &mut String| {
        if let Some(indent) = indent {
            out.push('\n');
            out.push_str(&" ".repeat(indent * depth));
        }
    };
    let between = if indent.is_some() { "," } else { ", " };
    match value {
        Value::String(text) => json_string(text, out),
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Object(items) if items.is_empty() => out.push_str("{}"),
        Value::Array(items) => {
            out.push('[');
            for (at, item) in items.iter().enumerate() {
                out.push_str(if at > 0 { between } else { "" });
                line(depth + 1, out);
                json_value(item, indent, depth + 1, out);
            }
            line(depth, out);
            out.push(']');
        }
        Value::Object(items) => {
            out.push('{');
            for (at, (key, item)) in items.iter().enumerate() {
                out.push_str(if at > 0 { between } else { "" });
                line(depth + 1, out);
                json_string(key, out);
                out.push_str(": ");
                json_value(item, indent, depth + 1, out);
            }
            line(depth, out);
            out.push('}');
        }
        // null, true, false and whole numbers are written alike by both
        scalar => out.push_str(&scalar.to_string()),
    }
}

/// `json.dumps(value, indent=indent)` of the Python implementation: ASCII only.
pub fn python_json(value: &Value, indent: Option<usize>) -> String {
    let mut out = String::new();
    json_value(value, indent, 0, &mut out);
    out
}

const MINUTE_US: i64 = 60_000_000;
const HOUR_US: i64 = 60 * MINUTE_US;
const DAY_US: i64 = 24 * HOUR_US;

/// How long before `reference` a time lies, in words. A time after it (a clock that was
/// set back) is "just now", never a negative age.
pub fn relative_age(timestamp: &str, reference: &str) -> Option<String> {
    let delta = parse_timestamp(reference)?.checked_sub(parse_timestamp(timestamp)?)?;
    let ago = |count: i64, unit: &str| {
        let plural = if count == 1 { "" } else { "s" };
        format!("{count} {unit}{plural} ago")
    };
    Some(match delta {
        delta if delta < MINUTE_US => "just now".to_string(),
        delta if delta < HOUR_US => ago(delta / MINUTE_US, "minute"),
        delta if delta < DAY_US => ago(delta / HOUR_US, "hour"),
        delta => ago(delta / DAY_US, "day"),
    })
}

fn named(project_id: Option<&str>, names: &HashMap<String, String>) -> String {
    let Some(project_id) = project_id else {
        return "-".to_string();
    };
    let name = names
        .get(project_id)
        .map_or("unknown project", String::as_str);
    format!("{name} ({project_id})")
}

fn project_cell(session: &Session, names: &HashMap<String, String>) -> String {
    match (session.status, &session.candidate_id, &session.project_id) {
        (SessionStatus::Pending, None, _) => "pending".to_string(),
        (SessionStatus::Pending, Some(candidate), _) => {
            format!("pending -> {}", named(Some(candidate), names))
        }
        (_, _, None) => "(unbound)".to_string(),
        (_, _, Some(project_id)) => named(Some(project_id), names),
    }
}

fn prompt_cell(entry: Option<&PromptEntry>) -> String {
    match entry.map(PromptEntry::body) {
        None => "-".to_string(),
        Some(PromptBody::Text(text)) => text.clone(),
        Some(PromptBody::Omitted(why)) => format!("(omitted: {why})"),
    }
}

/// One session as the text view shows it; no line break at its end.
pub fn session_block(
    session: &Session,
    names: &HashMap<String, String>,
    reference: &str,
    moves: &[SessionMove],
) -> String {
    let or_dash = |value: &Option<String>| match value.as_deref() {
        Some(value) if !value.is_empty() => visible(value),
        _ => "-".to_string(),
    };
    let age = relative_age(&session.last_active_at, reference).unwrap_or_default();
    let mut lines = vec![
        format!(
            "{} {}",
            session.key.harness(),
            visible(session.key.session_id())
        ),
        format!(
            "  last active: {} ({age})",
            visible(&session.last_active_at)
        ),
        format!("  project: {}", visible(&project_cell(session, names))),
        format!("  branch: {}", or_dash(&session.branch)),
    ];
    lines.extend(moves.iter().map(|moved| {
        visible(&format!(
            "  moved: {} from {} ({}) to {} ({})",
            moved.moved_at,
            moved.from_entry_cwd,
            named(moved.from_project_id.as_deref(), names),
            moved.to_entry_cwd,
            named(moved.to_project_id.as_deref(), names)
        ))
    }));
    lines.extend([
        format!("  first recorded: {}", visible(&session.started_at)),
        format!("  last end event: {}", or_dash(&session.ended_at)),
        format!(
            "  first prompt: {}",
            visible(&prompt_cell(session.first_prompt.as_ref()))
        ),
        format!(
            "  latest prompt: {}",
            visible(&prompt_cell(session.recent_prompts.last()))
        ),
        format!(
            "  summary: {}",
            match session.summary.as_deref() {
                Some(summary) if !summary.is_empty() => visible(&single_line(summary)),
                _ => "-".to_string(),
            }
        ),
        format!("  resume: {}", visible(&resume_command(session))),
    ]);
    lines.join("\n")
}

pub(crate) fn sessions(args: &Sessions, inv: &mut Invocation) -> Outcome {
    let (services, _) = services(args.root.as_deref(), inv)?;
    let limit = args.limit.as_ref().map(|limit| limit.as_usize());
    let read = || -> Result<_, CoreError> {
        let sessions =
            services
                .session
                .list_sessions(args.project.as_deref(), &args.query, limit)?;
        let mut moves = Vec::new();
        for session in &sessions {
            moves.push(services.session.moves(&session.key)?);
        }
        Ok((sessions, moves))
    };
    let (sessions, moves) = match read() {
        Ok(read) => read,
        Err(CoreError::ProjectNotFound(_)) => return no_such_project(args.project.as_deref(), inv),
        Err(CoreError::StorageFailure) => return unreadable(inv),
        Err(other) => return Err(other.into()),
    };
    if args.json {
        let items = sessions.iter().zip(&moves).map(|(session, moves)| {
            let mut item = session_item(session);
            let moves = moves.iter().map(|moved| {
                json!({
                    "moved_at": moved.moved_at,
                    "from_entry_cwd": moved.from_entry_cwd,
                    "from_project_id": moved.from_project_id,
                    "to_entry_cwd": moved.to_entry_cwd,
                    "to_project_id": moved.to_project_id,
                })
            });
            item.insert("moves".to_string(), moves.collect());
            Value::Object(item)
        });
        inv.say(format_args!("{}\n", python_json(&items.collect(), Some(2))));
        return Ok(0);
    }
    if sessions.is_empty() {
        inv.say(format_args!("(no sessions)\n"));
        return Ok(0);
    }
    let names: HashMap<String, String> = match services.project.list_projects() {
        Ok(projects) => projects
            .into_iter()
            .map(|project| (project.id, visible(&project.name)))
            .collect(),
        Err(CoreError::StorageFailure) => HashMap::new(),
        Err(other) => return Err(other.into()),
    };
    let reference = now();
    let blocks: Vec<String> = sessions
        .iter()
        .zip(&moves)
        .map(|(session, moves)| session_block(session, &names, &reference, moves))
        .collect();
    inv.say(format_args!("{}\n", blocks.join("\n")));
    Ok(0)
}

// --- export ---

/// A name that is one path component and nothing else. Ids never hold these characters,
/// so this never refuses real data: it is there for whatever a later caller passes.
fn export_name(name: &str) -> io::Result<CString> {
    let unsafe_name = name.is_empty() || name == "." || name == ".." || name.contains('/');
    match CString::new(name) {
        Ok(name) if !unsafe_name => Ok(name),
        _ => Err(io::Error::other("unsafe export name")),
    }
}

fn opened(descriptor: libc::c_int) -> io::Result<OwnedFd> {
    if descriptor < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: the descriptor was just returned by the system and nothing else holds it
    Ok(unsafe { OwnedFd::from_raw_fd(descriptor) })
}

const DIRECTORY: libc::c_int =
    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;

/// A directory opened to be searched and nothing more: what making and opening a name
/// inside it need, and what a directory that may not be listed still allows.
#[cfg(target_os = "linux")]
const SEARCHED: libc::c_int = libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC;
#[cfg(not(target_os = "linux"))]
const SEARCHED: libc::c_int = libc::O_SEARCH | libc::O_CLOEXEC;

/// The directory `target` is to be made in, opened and held, and the name it is to have
/// there. Everything of an export is made and opened from this descriptor on: a path
/// looked up a second time can lead elsewhere, when a directory above it was swapped for
/// a link in between.
fn above_of(target: &Path) -> io::Result<(OwnedFd, CString)> {
    let (Some(above), Some(name)) = (target.parent(), target.components().next_back()) else {
        return Err(io::Error::from_raw_os_error(libc::EEXIST));
    };
    let above = CString::new(above.as_os_str().as_bytes())?;
    // SAFETY: `above` is a valid C string for the length of the call
    let above = opened(unsafe { libc::open(above.as_ptr(), SEARCHED) })?;
    Ok((above, CString::new(name.as_os_str().as_bytes())?))
}

/// Makes the directory `name` inside the held directory.
fn make_in(directory: &OwnedFd, name: &CStr) -> io::Result<()> {
    // SAFETY: the descriptor is open and `name` is a valid C string for the call
    match unsafe { libc::mkdirat(directory.as_raw_fd(), name.as_ptr(), 0o700) } {
        0 => Ok(()),
        _ => Err(io::Error::last_os_error()),
    }
}

/// Opens the directory `name` inside the held directory, refusing a link in its place.
fn open_in(directory: &OwnedFd, name: &CStr) -> io::Result<OwnedFd> {
    // SAFETY: the descriptor is open and `name` is a valid C string for the call
    opened(unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), DIRECTORY) })
}

/// Makes the directory `name` inside the held directory and opens it, never by path.
fn folder(directory: &OwnedFd, name: &str) -> io::Result<OwnedFd> {
    let name = export_name(name)?;
    make_in(directory, &name)?;
    open_in(directory, &name)
}

/// Writes `name` inside the held directory, never by path: a link swapped in after the
/// directory was made cannot carry the write elsewhere, and a link at `name` is refused.
pub(crate) fn write_private(directory: &OwnedFd, name: &str, text: &str) -> io::Result<()> {
    let name = export_name(name)?;
    let flags = libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC;
    // SAFETY: the descriptor is open and `name` is a valid C string for the call
    let file = opened(unsafe {
        libc::openat(
            directory.as_raw_fd(),
            name.as_ptr(),
            flags,
            0o600 as libc::c_uint,
        )
    })?;
    File::from(file).write_all(text.as_bytes())
}

fn header(memory: &Memory) -> String {
    let fields: [(&str, Value); 12] = [
        ("id", json!(memory.id)),
        ("project_id", json!(memory.project_id)),
        ("type", json!(memory.type_.as_str())),
        ("source_harness", json!(memory.source_harness)),
        ("source_method", json!(memory.source_method)),
        ("trust", json!(memory.trust.as_str())),
        ("sync", json!(memory.sync)),
        ("created", json!(memory.created)),
        ("updated", json!(memory.updated)),
        ("version", json!(memory.version)),
        ("last_read_at", json!(memory.last_read_at)),
        ("description", json!(memory.description)),
    ];
    fields
        .iter()
        .map(|(field, value)| format!("{field}: {}\n", python_json(value, None)))
        .collect()
}

/// Writes the snapshot into the directory `name` that was just made inside `above`;
/// counts the memories as they are written, so a failure half-way can say how far it came.
fn snapshot(
    above: &OwnedFd,
    name: &CStr,
    listed: &[(Project, Vec<Memory>)],
    global_id: Option<&str>,
    count: &mut usize,
) -> io::Result<()> {
    // Held descriptors, not path checks: a name looked up again after its directory was
    // made can be swapped for a link. Everything below is created relative to a
    // descriptor opened right after its directory's own creation, refusing links.
    let directory = open_in(above, name)?;
    let projects: String = listed
        .iter()
        .map(|(project, _)| {
            format!(
                "- {} {} ({})\n",
                project.id,
                single_line(&project.name),
                place(project, global_id)
            )
        })
        .collect();
    write_private(&directory, "projects.md", &projects)?;
    for (project, memories) in listed.iter().filter(|(_, memories)| !memories.is_empty()) {
        let folder = folder(&directory, &project.id)?;
        for memory in memories {
            let text = format!("---\n{}---\n{}", header(memory), memory.body);
            write_private(&folder, &format!("{}.md", memory.id), &text)?;
            *count += 1;
        }
    }
    Ok(())
}

#[cfg(test)]
thread_local! {
    static MEANWHILE: std::cell::RefCell<Option<Box<dyn FnOnce()>>> =
        const { std::cell::RefCell::new(None) };
}

/// For a test, on its thread, once: `then` runs when the next export has made its
/// directory and before it opens it -- where somebody else can move what lies above it.
#[cfg(test)]
fn meanwhile(then: impl FnOnce() + 'static) {
    MEANWHILE.set(Some(Box::new(then)));
}

pub(crate) fn export(args: &Export, inv: &mut Invocation) -> Outcome {
    let target = tidied(&inv.cwd.join(&args.directory));
    let shown = visible(&target.to_string_lossy());
    if target.symlink_metadata().is_ok() {
        inv.say(format_args!("refused: {shown} already exists\n"));
        return Ok(2);
    }
    let (services, _) = services(args.root.as_deref(), inv)?;
    let read = || -> Result<_, CoreError> {
        let listed = services.memory.list_memories(None)?;
        Ok((listed, services.project.global_project_id()?))
    };
    let (listed, global_id) = match read() {
        Ok(read) => read,
        Err(CoreError::StorageFailure) => return unreadable(inv),
        Err(other) => return Err(other.into()),
    };
    let made = above_of(&target).and_then(|(above, name)| {
        make_in(&above, &name)?;
        Ok((above, name))
    });
    let (above, name) = match made {
        Ok(made) => made,
        Err(error) => {
            match error.raw_os_error() {
                Some(libc::EEXIST) => inv.say(format_args!("refused: {shown} already exists\n")),
                errno => {
                    let reason = errno.map_or_else(|| "error".to_string(), strerror);
                    inv.say(format_args!(
                        "refused: {shown} could not be created ({reason})\n"
                    ));
                }
            }
            return Ok(2);
        }
    };
    #[cfg(test)]
    if let Some(then) = MEANWHILE.take() {
        then();
    }
    let mut count = 0;
    if let Err(error) = snapshot(&above, &name, &listed, global_id.as_deref(), &mut count) {
        let reason = error
            .raw_os_error()
            .map_or_else(|| "write failed".to_string(), strerror);
        inv.say(format_args!(
            "refused: the export stopped after {count} memories ({reason}); {shown} holds a \
             partial snapshot -- delete it and run memriver export again\n"
        ));
        return Ok(2);
    }
    inv.say(format_args!("exported {count} memories to {shown}\n"));
    Ok(0)
}

#[cfg(test)]
mod tests;
