//! Memories over the SQLite database: current state, permanent history, the change log.
//!
//! A `memories` row is a memory's current state; every state it ever had is a
//! `memory_versions` row with its complete `memory_sources` set; every write is one
//! `changes` row with one `change_steps` row per memory it touched. All of it is
//! written by `apply_ops`, inside one write transaction, so a refused change leaves
//! nothing behind.

use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use rusqlite::types::Value;
use rusqlite::{Connection, ErrorCode, OptionalExtension, params, params_from_iter};

use super::database::{Database, MEMORY_COLUMNS, memory_from_row, memory_to_row, values};
use crate::models::changes::{Change, MetadataCaps, Op, OpName, SourceRef, Step};
use crate::models::context::ReadWriteSet;
use crate::models::errors::{BatchConflictReason, Cap, CoreError, ValueError, WriteError};
use crate::models::helpers::{
    is_id, is_timestamp, new_id, now, now_strictly_after, py_repr, py_strip,
};
use crate::models::memory::{
    Memory, MemoryType, Trust, checked_level, checked_tags, tags_from_json, tags_to_json,
};
use crate::settings::BUSY_TIMEOUT_MS;

/// The content policy on one resulting state: `check(description, body)` is the id of
/// the rule that refuses it, or None.
pub type Check<'a> = &'a dyn Fn(&str, &str) -> Option<String>;

type Written<T> = Result<T, WriteError>;

impl From<rusqlite::Error> for WriteError {
    fn from(err: rusqlite::Error) -> WriteError {
        WriteError::Core(err.into())
    }
}

fn value_error<T>(message: impl Into<String>) -> Written<T> {
    Err(WriteError::Value(ValueError::new(message)))
}

fn conflict<T>(index: usize, memory_id: Option<&str>, reason: BatchConflictReason) -> Written<T> {
    Err(WriteError::Core(CoreError::BatchConflict {
        index,
        memory_id: memory_id.map(str::to_string),
        reason,
    }))
}

fn rejected(rule_id: String, memory_id: Option<&str>) -> WriteError {
    WriteError::Core(CoreError::ContentRejected {
        message: String::new(),
        rule_id,
        memory_id: memory_id.map(str::to_string),
        detail: String::new(),
    })
}

/// The memory columns, each named through the alias `m`.
fn joined_columns() -> String {
    MEMORY_COLUMNS
        .split(',')
        .map(|column| format!("m.{}", column.trim()))
        .collect::<Vec<_>>()
        .join(", ")
}

fn placeholders(count: usize) -> String {
    vec!["?"; count].join(", ")
}

/// The row, decoded; None when absent or outside `readable` (None: any project).
///
/// It is joined to its project, so an orphan (a writer ignored foreign keys) answers
/// like any other hidden id. Authorization and the deleted state are decided in SQL,
/// before a single column is decoded: a damaged row of a project the caller may not
/// read, and a damaged deleted row, answer like an absent id, never as damage.
fn joined(
    conn: &Connection,
    memory_id: &str,
    include_deleted: bool,
    readable: Option<&BTreeSet<&str>>,
) -> Result<Option<Memory>, CoreError> {
    if readable.is_some_and(BTreeSet::is_empty) {
        return Ok(None);
    }
    let mut sql = format!(
        "SELECT {} FROM memories m JOIN projects p ON p.id = m.project_id WHERE m.id = ?",
        joined_columns()
    );
    if !include_deleted {
        sql.push_str(" AND m.deleted_at IS NULL");
    }
    let mut bound = vec![memory_id];
    if let Some(readable) = readable {
        sql.push_str(&format!(
            " AND m.project_id IN ({})",
            placeholders(readable.len())
        ));
        bound.extend(readable.iter().copied());
    }
    let row = conn
        .query_row(&sql, params_from_iter(bound), values)
        .optional()?;
    // damage is reported as damage, not absence
    row.map(|row| memory_from_row(&row).map_err(|_| CoreError::StorageFailure))
        .transpose()
}

/// Whether the project row is the global one; None without a row.
fn is_global(conn: &Connection, project_id: &str) -> Result<Option<bool>, CoreError> {
    Ok(conn
        .query_row(
            "SELECT is_global FROM projects WHERE id = ?1",
            [project_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()?
        .map(|flag| flag != 0))
}

/// Locate and authorize one agent target inside the write transaction.
fn writable(
    conn: &Connection,
    memory_id: &str,
    read_write_set: &ReadWriteSet,
) -> Result<Memory, CoreError> {
    let not_found = || CoreError::MemoryNotFound(memory_id.to_string());
    let memory =
        joined(conn, memory_id, false, Some(&read_write_set.readable()))?.ok_or_else(not_found)?;
    // global is readable, so naming the rule reveals nothing; this is only an early
    // refusal from the cached id -- the database can be replaced or restored under a
    // running server, so the row's own flag, read just below, is the one that decides
    if Some(memory.project_id.as_str()) == read_write_set.global_project_id.as_deref() {
        return Err(CoreError::GlobalReadOnly);
    }
    // `joined` already required a project row to exist (it is a join)
    if is_global(conn, &memory.project_id)?.unwrap_or(true) {
        return Err(CoreError::GlobalReadOnly);
    }
    if !read_write_set
        .writable()
        .contains(memory.project_id.as_str())
    {
        return Err(not_found());
    }
    Ok(memory)
}

/// Lowest first.
const TRUST_ORDER: [Trust; 3] = [Trust::UntrustedDerived, Trust::Agent, Trust::User];

/// Does a walk along source rows of any version, from (memory_id, version), reach the id?
const REACHES: &str = "WITH RECURSIVE reach(id) AS (
  SELECT source_id FROM memory_sources WHERE memory_id = ? AND version = ?
  UNION
  SELECT s.source_id FROM memory_sources s JOIN reach r ON s.memory_id = r.id)
SELECT 1 FROM reach WHERE id = ? LIMIT 1";

/// Everything one version records.
#[derive(Debug, Clone, PartialEq, Eq)]
struct State {
    type_: MemoryType,
    trust: Trust,
    sync: bool,
    description: String,
    body: String,
    deleted: bool,
    sources: Vec<SourceRef>,
    level: Option<i64>,
    tags: Vec<String>,
}

/// One memory a change wrote: its project, its state before (None for one the change
/// created) and after.
struct Wrote {
    memory_id: String,
    project_id: String,
    before: Option<State>,
    after: State,
}

/// One change being written, inside its write transaction.
struct Batch<'a> {
    conn: &'a Connection,
    /// None: the management path.
    restriction: Option<&'a ReadWriteSet>,
    check: Check<'a>,
    change_id: String,
    /// (version, deleted) before this change of every memory it has written so far,
    /// None for one it created: what a newly cited source is admitted against.
    before: HashMap<String, Option<(i64, bool)>>,
    /// (index, memory id, version) of every new version with sources: the cycle check.
    cited: Vec<(usize, String, i64)>,
    /// In the order of the operations: the limits.
    written: Vec<Wrote>,
}

fn chars_between(text: &str, least: usize, most: usize) -> bool {
    (least..=most).contains(&text.chars().count())
}

/// Check and write `ops` as one change, inside the caller's write transaction.
///
/// Every refusal comes back before the caller commits, so a refused change leaves no
/// change row, step, version or source behind.
#[allow(clippy::too_many_arguments)]
pub(crate) fn apply_ops(
    conn: &Connection,
    ops: &[Op],
    restriction: Option<&ReadWriteSet>,
    changed_by: &str,
    changed_via: Option<&str>,
    check: Check<'_>,
    caps: &MetadataCaps,
    undoes: Option<&str>,
) -> Written<Change> {
    if ops.is_empty() {
        return value_error("a change needs at least one operation");
    }
    if !chars_between(changed_by, 1, 32) {
        return value_error("changed_by is not 1 to 32 characters");
    }
    if changed_via.is_some_and(|via| !chars_between(via, 1, 64)) {
        return value_error("changed_via is not 1 to 64 characters");
    }
    let named: Vec<&str> = ops.iter().filter_map(target_id).collect();
    if named.iter().collect::<BTreeSet<_>>().len() != named.len() {
        return value_error("a change touches a memory at most once");
    }
    let change_id = new_id();
    let taken = conn
        .query_row(
            "SELECT 1 FROM changes WHERE change_id = ?1",
            [&change_id],
            |_| Ok(()),
        )
        .optional()?;
    if taken.is_some() {
        // an id is drawn once: a taken one is a failure, never a second draw
        return Err(CoreError::StorageFailure.into());
    }
    let at = now();
    let step_count = i64::try_from(ops.len()).map_err(|_| CoreError::StorageFailure)?;
    // first: every version and step below references the change row
    conn.execute(
        "INSERT INTO changes (change_id, at, changed_by, changed_via, step_count, undoes) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![change_id, at, changed_by, changed_via, step_count, undoes],
    )?;
    let mut batch = Batch {
        conn,
        restriction,
        check,
        change_id,
        before: HashMap::new(),
        cited: Vec::new(),
        written: Vec::new(),
    };
    let mut steps = Vec::with_capacity(ops.len());
    for (index, op) in ops.iter().enumerate() {
        steps.push(apply_one(&mut batch, index, op, changed_by, changed_via)?);
    }
    refuse_cycles(&batch)?;
    refuse_caps(&batch, caps)?;
    for step in &steps {
        conn.execute(
            "INSERT INTO change_steps (change_id, step, memory_id, op, before_version, \
             after_version) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                batch.change_id,
                step.step,
                step.memory_id,
                step.op.as_str(),
                step.before_version,
                step.after_version
            ],
        )?;
    }
    Ok(Change {
        change_id: batch.change_id,
        at,
        changed_by: changed_by.to_string(),
        changed_via: changed_via.map(str::to_string),
        step_count,
        undoes: undoes.map(str::to_string),
        steps,
    })
}

/// The memory an operation names; None for a create.
fn target_id(op: &Op) -> Option<&str> {
    match op {
        Op::Create { .. } => None,
        Op::Update { memory_id, .. }
        | Op::SoftDelete { memory_id, .. }
        | Op::Restore { memory_id, .. } => Some(memory_id),
    }
}

fn step_of(index: usize, memory_id: &str, op: OpName, before: Option<i64>, after: i64) -> Step {
    Step {
        // a batch is far smaller than the number can hold
        step: i64::try_from(index + 1).unwrap_or(i64::MAX),
        memory_id: memory_id.to_string(),
        op,
        before_version: before,
        after_version: after,
    }
}

fn apply_one(
    batch: &mut Batch<'_>,
    index: usize,
    op: &Op,
    changed_by: &str,
    changed_via: Option<&str>,
) -> Written<Step> {
    match op {
        Op::Create { .. } => create(batch, index, op, changed_by, changed_via),
        Op::Update {
            memory_id,
            expected_version,
            description,
            body,
            sources,
            level,
            tags,
        } => {
            let current = target(batch, index, memory_id, *expected_version, false)?;
            let before = state_of(batch.conn, &current)?;
            let sources = admit(
                batch,
                index,
                Some(&current.id),
                &before.sources,
                sources.as_deref(),
            )?;
            let (trust, sync) = if sources.is_empty() {
                (before.trust, before.sync)
            } else {
                derived(
                    batch,
                    index,
                    Some(&current.id),
                    before.trust,
                    before.sync,
                    &sources,
                )?
            };
            let text = |new: &Option<String>, old: &str| match new {
                Some(new) => py_strip(new).to_string(),
                None => old.to_string(),
            };
            let after = State {
                trust,
                sync,
                sources,
                description: text(description, &before.description),
                body: text(body, &before.body),
                // not mentioned: carried forward, like the text
                level: match level {
                    Some(_) => checked_level(*level)?,
                    None => before.level,
                },
                tags: match tags {
                    Some(tags) => checked_tags(tags)?,
                    None => before.tags.clone(),
                },
                ..before.clone()
            };
            write_version(batch, index, OpName::Update, &current, before, after)
        }
        Op::SoftDelete {
            memory_id,
            expected_version,
            unread_since,
        } => {
            let current = target(batch, index, memory_id, *expected_version, false)?;
            if let Some(since) = unread_since {
                if !is_timestamp(since) {
                    return value_error("unread_since is not a timestamp");
                }
                // both are the fixed-width form, so text order is time order
                if current
                    .last_read_at
                    .as_ref()
                    .is_some_and(|read| read >= since)
                {
                    return conflict(index, Some(&current.id), BatchConflictReason::ReadSince);
                }
            }
            let before = state_of(batch.conn, &current)?;
            let after = State {
                deleted: true,
                ..before.clone()
            };
            write_version(batch, index, OpName::SoftDelete, &current, before, after)
        }
        Op::Restore {
            memory_id,
            expected_version,
            to_version,
        } => {
            // `to_version`'s recorded state -- its own trust and sync included -- as
            // the next version
            let current = target(batch, index, memory_id, *expected_version, true)?;
            let row = batch
                .conn
                .query_row(
                    "SELECT type, trust, sync, description, body, deleted, level, tags \
                     FROM memory_versions WHERE memory_id = ?1 AND version = ?2",
                    params![current.id, to_version],
                    values,
                )
                .optional()?;
            let Some(row) = row else {
                return conflict(index, Some(&current.id), BatchConflictReason::Missing);
            };
            let mut after = history_state(&row)?;
            after.sources = sources_of(batch.conn, &current.id, *to_version)?;
            // restored references go back as they were; each cited version must still
            // exist
            for cited in &after.sources {
                if !version_exists(batch.conn, cited)? {
                    return conflict(index, Some(&current.id), BatchConflictReason::Source);
                }
            }
            let before = state_of(batch.conn, &current)?;
            write_version(batch, index, OpName::Restore, &current, before, after)
        }
    }
}

fn version_exists(conn: &Connection, cited: &SourceRef) -> Result<bool, CoreError> {
    Ok(conn
        .query_row(
            "SELECT 1 FROM memory_versions WHERE memory_id = ?1 AND version = ?2",
            params![cited.memory_id, cited.version],
            |_| Ok(()),
        )
        .optional()?
        .is_some())
}

fn create(
    batch: &mut Batch<'_>,
    index: usize,
    op: &Op,
    changed_by: &str,
    changed_via: Option<&str>,
) -> Written<Step> {
    let Op::Create {
        project_id,
        type_,
        description,
        body,
        sources,
        trust,
        sync,
        level,
        tags,
    } = op
    else {
        unreachable!("only a create is handed here");
    };
    authorize_create(batch, project_id)?;
    let sources = admit(batch, index, None, &[], Some(sources))?;
    // the type, then the trust, then the project's id: the order Python's `Memory.new`
    // looks at them in, and its words for the first two
    let named =
        |what: &str, value: &str| ValueError::new(format!("invalid {what}: {}", py_repr(value)));
    // with sources, trust and sync come from them alone
    let derived_from = if sources.is_empty() {
        None
    } else {
        Some(derived(batch, index, None, Trust::User, true, &sources)?)
    };
    let type_ = MemoryType::parse(type_).ok_or_else(|| named("memory type", type_))?;
    let (trust, sync) = match derived_from {
        Some(derived) => derived,
        None => (
            Trust::parse(trust).ok_or_else(|| named("trust", trust))?,
            *sync,
        ),
    };
    let memory = Memory::new(
        body,
        type_,
        project_id,
        changed_via.unwrap_or("unknown"),
        changed_by,
        trust,
        sync,
        description,
    )?;
    let memory = Memory {
        level: checked_level(*level)?,
        tags: checked_tags(tags)?,
        ..memory
    };
    let row = memory_to_row(&memory);
    // a row the read path would reject is never committed
    memory_from_row(&row)?;
    if let Some(rule) = (batch.check)(&memory.description, &memory.body) {
        return Err(rejected(rule, None));
    }
    // deleted rows keep their id: an id is never reused
    let taken = batch
        .conn
        .query_row("SELECT 1 FROM memories WHERE id = ?1", [&memory.id], |_| {
            Ok(())
        })
        .optional()?;
    if taken.is_some() {
        return Err(CoreError::StorageFailure.into());
    }
    batch.conn.execute(
        &format!(
            "INSERT INTO memories ({MEMORY_COLUMNS}) VALUES ({})",
            placeholders(row.len())
        ),
        params_from_iter(row),
    )?;
    batch.before.insert(memory.id.clone(), None);
    let state = State {
        type_: memory.type_,
        trust: memory.trust,
        sync: memory.sync,
        description: memory.description,
        body: memory.body,
        deleted: false,
        sources,
        level: memory.level,
        tags: memory.tags,
    };
    insert_version(batch, index, &memory.id, 1, &state)?;
    batch.written.push(Wrote {
        memory_id: memory.id.clone(),
        project_id: memory.project_id,
        before: None,
        after: state,
    });
    Ok(step_of(index, &memory.id, OpName::Create, None, 1))
}

/// The project a create lands in: exists, and on the agent path is writable, not global.
fn authorize_create(batch: &Batch<'_>, project_id: &str) -> Result<(), CoreError> {
    let unavailable = || CoreError::ProjectUnavailable {
        reason: String::new(),
    };
    if let Some(restriction) = batch.restriction {
        if Some(project_id) == restriction.global_project_id.as_deref() {
            return Err(CoreError::GlobalReadOnly);
        }
        if !restriction.writable().contains(project_id) {
            return Err(unavailable());
        }
    }
    match (is_global(batch.conn, project_id)?, batch.restriction) {
        (None, Some(_)) => Err(unavailable()),
        (None, None) => Err(CoreError::ProjectNotFound(project_id.to_string())),
        // the cached global id above is only an early refusal: the role that decides
        // is the one on the row, read just now
        (Some(true), Some(_)) => Err(CoreError::GlobalReadOnly),
        _ => Ok(()),
    }
}

/// The memory an operation names, located and authorized inside the transaction.
fn target(
    batch: &Batch<'_>,
    index: usize,
    memory_id: &str,
    expected_version: i64,
    allow_deleted: bool,
) -> Written<Memory> {
    if let Some(restriction) = batch.restriction {
        // the agent path answers as it always has: an agent learns nothing new
        let memory = writable(batch.conn, memory_id, restriction)?;
        if memory.version != expected_version {
            return Err(CoreError::VersionConflict(memory_id.to_string()).into());
        }
        return Ok(memory);
    }
    let found = if is_id(memory_id) {
        joined(batch.conn, memory_id, true, None)?
    } else {
        None
    };
    let Some(memory) = found else {
        return conflict(index, Some(memory_id), BatchConflictReason::Missing);
    };
    if memory.version != expected_version {
        return conflict(index, Some(memory_id), BatchConflictReason::Version);
    }
    if memory.deleted_at.is_some() && !allow_deleted {
        return conflict(index, Some(memory_id), BatchConflictReason::Deleted);
    }
    Ok(memory)
}

/// The resulting source set of a create (`current` empty) or an update.
///
/// None keeps the current set. Otherwise a reference already in the current set is
/// carried -- its version exists, whatever the source did since -- and any other is
/// newly cited: it must be the source's current, non-deleted version as it stood
/// before this change.
fn admit(
    batch: &Batch<'_>,
    index: usize,
    memory_id: Option<&str>,
    current: &[SourceRef],
    requested: Option<&[SourceRef]>,
) -> Written<Vec<SourceRef>> {
    let Some(requested) = requested else {
        return Ok(current.to_vec());
    };
    let mut cited = requested.to_vec();
    // stable, as Python's sort: of two references to one memory the order given stays
    cited.sort_by(|a, b| a.memory_id.cmp(&b.memory_id));
    if cited
        .windows(2)
        .any(|pair| pair[0].memory_id == pair[1].memory_id)
    {
        return value_error("a source set cites a memory at most once");
    }
    for source in &cited {
        if Some(source.memory_id.as_str()) == memory_id {
            return conflict(index, memory_id, BatchConflictReason::Cycle);
        }
        if !current.contains(source)
            && pre_batch(batch, &source.memory_id)? != Some((source.version, false))
        {
            return conflict(index, memory_id, BatchConflictReason::Source);
        }
    }
    Ok(cited)
}

/// (version, deleted) of a memory before this change; None when it did not exist.
fn pre_batch(batch: &Batch<'_>, memory_id: &str) -> Result<Option<(i64, bool)>, CoreError> {
    if let Some(before) = batch.before.get(memory_id) {
        return Ok(*before);
    }
    Ok(batch
        .conn
        .query_row(
            "SELECT version, deleted_at IS NOT NULL FROM memories WHERE id = ?1",
            [memory_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?)
}

/// The lowest of `trust` and every cited version's trust; sync only if all sync.
fn derived(
    batch: &Batch<'_>,
    index: usize,
    memory_id: Option<&str>,
    mut trust: Trust,
    mut sync: bool,
    sources: &[SourceRef],
) -> Written<(Trust, bool)> {
    let rank = |trust: Trust| TRUST_ORDER.iter().position(|known| *known == trust);
    for source in sources {
        let row = batch
            .conn
            .query_row(
                "SELECT trust, sync FROM memory_versions WHERE memory_id = ?1 AND version = ?2",
                params![source.memory_id, source.version],
                values,
            )
            .optional()?;
        let Some(row) = row else {
            return conflict(index, memory_id, BatchConflictReason::Source);
        };
        // a trust no version of memriver writes: Python looks it up in its list of
        // the three and fails with the list's own words
        let cited = match &row[0] {
            Value::Text(text) => Trust::parse(text),
            _ => None,
        };
        let Some(cited) = cited else {
            return value_error("tuple.index(x): x not in tuple");
        };
        if rank(cited) < rank(trust) {
            trust = cited;
        }
        sync = sync && !matches!(row[1], Value::Integer(0));
    }
    Ok((trust, sync))
}

/// No walk along the source rows of any version leads a memory back to itself.
fn refuse_cycles(batch: &Batch<'_>) -> Written<()> {
    for (index, memory_id, version) in &batch.cited {
        let reached = batch
            .conn
            .query_row(REACHES, params![memory_id, version, memory_id], |_| Ok(()))
            .optional()?;
        if reached.is_some() {
            return conflict(*index, Some(memory_id), BatchConflictReason::Cycle);
        }
    }
    Ok(())
}

fn standing(state: Option<&State>) -> bool {
    state.is_some_and(|state| !state.deleted && state.level == Some(1))
}

fn exceeded<T>(cap: Cap, memory_id: Option<&str>) -> Written<T> {
    Err(WriteError::Core(CoreError::CapExceeded {
        cap,
        memory_id: memory_id.map(str::to_string),
    }))
}

/// The metadata limits, per project the change wrote, on the states it leaves. A
/// change never breaks a limit, and a breach already there -- the limit was lowered
/// since -- may stay but never grow: a memory it wrote may keep its own breach while
/// the new value is no larger than the old (its tag count, its level-1 body), and a
/// project's count may stay above its limit but never rise.
fn refuse_caps(batch: &Batch<'_>, caps: &MetadataCaps) -> Written<()> {
    let length = |state: &State| state.body.chars().count();
    for wrote in &batch.written {
        let (before, after) = (wrote.before.as_ref(), &wrote.after);
        if after.deleted {
            continue;
        }
        let held = before.filter(|before| !before.deleted);
        if after.tags.len() > caps.tags_per_memory_max
            && !held.is_some_and(|before| after.tags.len() <= before.tags.len())
        {
            return exceeded(Cap::TagsPerMemoryMax, Some(&wrote.memory_id));
        }
        if standing(Some(after))
            && length(after) > caps.standing_max_chars
            && !(standing(before) && before.is_some_and(|before| length(after) <= length(before)))
        {
            return exceeded(Cap::StandingMaxChars, Some(&wrote.memory_id));
        }
    }
    let projects: BTreeSet<&str> = batch
        .written
        .iter()
        .map(|wrote| wrote.project_id.as_str())
        .collect();
    for project_id in projects {
        let written: Vec<&Wrote> = batch
            .written
            .iter()
            .filter(|wrote| wrote.project_id == project_id)
            .collect();
        // the project's other current memories: the same before and after this change
        let sql = format!(
            "SELECT level, tags FROM memories WHERE project_id = ? AND deleted_at IS NULL \
             AND id NOT IN ({})",
            placeholders(written.len())
        );
        let bound = std::iter::once(project_id).chain(written.iter().map(|w| w.memory_id.as_str()));
        let mut statement = batch.conn.prepare(&sql)?;
        let others = statement
            .query_map(params_from_iter(bound), values)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut others_standing = 0usize;
        let mut names: BTreeSet<String> = BTreeSet::new();
        for row in others {
            let Value::Text(tags) = &row[1] else {
                return Err(CoreError::StorageFailure.into());
            };
            names.extend(tags_from_json(tags).map_err(|_| CoreError::StorageFailure)?);
            others_standing += usize::from(row[0] == Value::Integer(1));
        }
        let count = |states: &[Option<&State>]| {
            others_standing + states.iter().filter(|state| standing(**state)).count()
        };
        let carried = |states: &[Option<&State>]| {
            let mut carried = names.clone();
            for state in states.iter().flatten().filter(|state| !state.deleted) {
                carried.extend(state.tags.iter().cloned());
            }
            carried.len()
        };
        let before: Vec<Option<&State>> = written.iter().map(|w| w.before.as_ref()).collect();
        let after: Vec<Option<&State>> = written.iter().map(|w| Some(&w.after)).collect();
        if count(&after) > caps.standing_max_count && count(&after) > count(&before) {
            return exceeded(Cap::StandingMaxCount, None);
        }
        if carried(&after) > caps.tags_max && carried(&after) > carried(&before) {
            return exceeded(Cap::TagsMax, None);
        }
    }
    Ok(())
}

fn state_of(conn: &Connection, memory: &Memory) -> Result<State, CoreError> {
    Ok(State {
        type_: memory.type_,
        trust: memory.trust,
        sync: memory.sync,
        description: memory.description.clone(),
        body: memory.body.clone(),
        deleted: memory.deleted_at.is_some(),
        sources: sources_of(conn, &memory.id, memory.version)?,
        level: memory.level,
        tags: memory.tags.clone(),
    })
}

/// A version's sources. `memory_sources` has no CHECK on `source_id`, so a damaged one
/// is caught here: a storage failure for a row memriver could not have written.
fn sources_of(
    conn: &Connection,
    memory_id: &str,
    version: i64,
) -> Result<Vec<SourceRef>, CoreError> {
    let mut statement = conn.prepare(
        "SELECT source_id, source_version FROM memory_sources \
         WHERE memory_id = ?1 AND version = ?2 ORDER BY source_id",
    )?;
    let rows = statement
        .query_map(params![memory_id, version], values)?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    rows.iter()
        .map(|row| match (&row[0], &row[1]) {
            (Value::Text(source_id), Value::Integer(version)) if is_id(source_id) => {
                Ok(SourceRef {
                    memory_id: source_id.clone(),
                    version: *version,
                })
            }
            _ => Err(CoreError::StorageFailure),
        })
        .collect()
}

/// A `memory_versions` row's state (type, trust, sync, description, body, deleted,
/// level, tags), validated like a memory row; its sources are read apart.
///
/// `memory_versions` has no CHECK on type and trust, so a damaged history row is caught
/// here: a storage failure for a row memriver could not have written.
fn history_state(row: &[Value]) -> Result<State, CoreError> {
    let damaged = CoreError::StorageFailure;
    let flag = |value: &Value| match value {
        Value::Integer(0) => Some(false),
        Value::Integer(1) => Some(true),
        _ => None,
    };
    let [type_, trust, sync, description, body, deleted, level, tags] = row else {
        return Err(damaged);
    };
    let (
        Value::Text(type_),
        Value::Text(trust),
        Value::Text(description),
        Value::Text(body),
        Value::Text(tags),
    ) = (type_, trust, description, body, tags)
    else {
        return Err(damaged);
    };
    let level = match level {
        Value::Null => None,
        Value::Integer(level) => Some(*level),
        _ => return Err(damaged),
    };
    let state = || {
        Some(State {
            type_: MemoryType::parse(type_)?,
            trust: Trust::parse(trust)?,
            sync: flag(sync)?,
            description: description.clone(),
            body: body.clone(),
            deleted: flag(deleted)?,
            sources: Vec::new(),
            level: checked_level(level).ok()?,
            tags: tags_from_json(tags).ok()?,
        })
    };
    state().ok_or(damaged)
}

/// The next version of `current` in state `after`: the row, the version and its sources.
fn write_version(
    batch: &mut Batch<'_>,
    index: usize,
    op: OpName,
    current: &Memory,
    before: State,
    after: State,
) -> Written<Step> {
    if after == before {
        return conflict(index, Some(&current.id), BatchConflictReason::SameState);
    }
    // every resulting state passes today's policy, a deleted one included
    // ponytail: every op's resulting text is scanned again here, while BEGIN IMMEDIATE
    // holds the write lock, on top of the service's own scan just before the call --
    // one write pays for the policy check twice. Upgrade path: scan before taking the
    // lock, and inside re-check only the text a concurrent writer could have changed
    // since.
    if let Some(rule) = (batch.check)(&after.description, &after.body) {
        return Err(rejected(rule, Some(&current.id)));
    }
    let updated = now_strictly_after(&current.updated);
    let memory = Memory {
        type_: after.type_,
        trust: after.trust,
        sync: after.sync,
        description: after.description.clone(),
        body: after.body.clone(),
        updated: updated.clone(),
        version: current.version + 1,
        deleted_at: after.deleted.then_some(updated),
        level: after.level,
        tags: after.tags.clone(),
        ..current.clone()
    };
    // validate the row as it will stand, before writing it
    memory_from_row(&memory_to_row(&memory))?;
    batch
        .before
        .entry(current.id.clone())
        .or_insert(Some((current.version, before.deleted)));
    batch.conn.execute(
        "UPDATE memories SET type = ?1, trust = ?2, sync = ?3, description = ?4, body = ?5, \
         updated = ?6, version = ?7, deleted_at = ?8, level = ?9, tags = ?10 WHERE id = ?11",
        params![
            memory.type_.as_str(),
            memory.trust.as_str(),
            i64::from(memory.sync),
            memory.description,
            memory.body,
            memory.updated,
            memory.version,
            memory.deleted_at,
            memory.level,
            tags_to_json(&memory.tags),
            memory.id
        ],
    )?;
    insert_version(batch, index, &memory.id, memory.version, &after)?;
    batch.written.push(Wrote {
        memory_id: current.id.clone(),
        project_id: current.project_id.clone(),
        before: Some(before),
        after,
    });
    Ok(step_of(
        index,
        &current.id,
        op,
        Some(current.version),
        memory.version,
    ))
}

fn insert_version(
    batch: &mut Batch<'_>,
    index: usize,
    memory_id: &str,
    version: i64,
    state: &State,
) -> Result<(), CoreError> {
    batch.conn.execute(
        "INSERT INTO memory_versions (memory_id, version, type, trust, sync, description, \
         body, deleted, change_id, level, tags) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        params![
            memory_id,
            version,
            state.type_.as_str(),
            state.trust.as_str(),
            i64::from(state.sync),
            state.description,
            state.body,
            i64::from(state.deleted),
            batch.change_id,
            state.level,
            tags_to_json(&state.tags)
        ],
    )?;
    for source in &state.sources {
        batch.conn.execute(
            "INSERT INTO memory_sources (memory_id, version, source_id, source_version) \
             VALUES (?1, ?2, ?3, ?4)",
            params![memory_id, version, source.memory_id, source.version],
        )?;
    }
    if !state.sources.is_empty() {
        batch.cited.push((index, memory_id.to_string(), version));
    }
    Ok(())
}

pub struct MemoryStore {
    root: PathBuf,
    busy_timeout_ms: u64,
}

impl MemoryStore {
    pub fn new(root: &Path) -> MemoryStore {
        MemoryStore::with_busy_timeout(root, BUSY_TIMEOUT_MS)
    }

    /// For a test that must not wait the whole `BUSY_TIMEOUT_MS` for a lock.
    pub fn with_busy_timeout(root: &Path, busy_timeout_ms: u64) -> MemoryStore {
        MemoryStore {
            root: root.to_path_buf(),
            busy_timeout_ms,
        }
    }

    fn database(&self) -> Database {
        Database::with_busy_timeout(&self.root, self.busy_timeout_ms)
    }

    /// One write transaction around `body`, which may refuse with either kind of error.
    fn written<T>(&self, body: impl FnOnce(&Connection) -> Written<T>) -> Written<T> {
        // the database hands back its own kind of error only: a caller's error is
        // carried past it, and the transaction is rolled back for either
        let mut callers = None;
        let done = self.database().write(false, |conn| {
            body(conn).map_err(|err| match err {
                WriteError::Core(err) => err,
                WriteError::Value(err) => {
                    callers = Some(err);
                    CoreError::StorageFailure
                }
            })
        });
        match callers {
            Some(err) => Err(WriteError::Value(err)),
            None => Ok(done?),
        }
    }

    /// The management write: every operation, or none, as one change.
    pub fn apply(
        &self,
        ops: &[Op],
        changed_by: &str,
        changed_via: Option<&str>,
        check: Check<'_>,
        caps: &MetadataCaps,
    ) -> Written<Change> {
        self.written(|conn| apply_ops(conn, ops, None, changed_by, changed_via, check, caps, None))
    }

    /// One agent operation; the resulting memory, read inside the write transaction.
    ///
    /// An unchanged result writes nothing and returns the memory as checked.
    pub fn write(
        &self,
        op: &Op,
        restriction: &ReadWriteSet,
        changed_by: &str,
        changed_via: Option<&str>,
        check: Check<'_>,
        caps: &MetadataCaps,
    ) -> Written<Memory> {
        // Restore and an explicit source set are management-only: admitting a newly
        // cited source looks a memory up by id alone, with no `restriction` check, so
        // an agent operation that reached it could probe another project's memories
        // through "source" and "cycle" conflicts. An agent never sets sources, a level
        // or tags: a create has none, an update keeps them
        let plain = match op {
            Op::Restore { .. } => return value_error("restore is not an agent operation"),
            Op::Create {
                sources,
                level,
                tags,
                ..
            } => sources.is_empty() && level.is_none() && tags.is_empty(),
            Op::Update {
                sources,
                level,
                tags,
                ..
            } => sources.is_none() && level.is_none() && tags.is_none(),
            Op::SoftDelete { .. } => true,
        };
        if !plain {
            return value_error("citing sources or setting metadata is not an agent operation");
        }
        let named = target_id(op);
        let not_found = |memory_id: &str| CoreError::MemoryNotFound(memory_id.to_string());
        // a malformed target id answers exactly like a missing one, never as storage
        // damage: SQLite never sees it
        if let Some(memory_id) = named.filter(|memory_id| !is_id(memory_id)) {
            return Err(not_found(memory_id).into());
        }
        // as on every agent write: a removed store is never recreated, and it answers
        // as the missing project or memory it hides
        if !self.database().exists()? {
            return Err(match named {
                Some(memory_id) => not_found(memory_id),
                None => CoreError::ProjectUnavailable {
                    reason: String::new(),
                },
            }
            .into());
        }
        let memory = self.written(|conn| {
            // the change row is written first, so a checked no-op rolls back to here
            conn.execute_batch("SAVEPOINT agent_write")?;
            let applied = apply_ops(
                conn,
                std::slice::from_ref(op),
                Some(restriction),
                changed_by,
                changed_via,
                check,
                caps,
                None,
            );
            let memory_id = match (applied, named) {
                (Ok(change), _) => change.steps[0].memory_id.clone(),
                // on this path only an unchanged result, after every other check passed
                (
                    Err(WriteError::Core(CoreError::BatchConflict {
                        reason: BatchConflictReason::SameState,
                        ..
                    })),
                    Some(memory_id),
                ) => {
                    conn.execute_batch("ROLLBACK TO agent_write")?;
                    memory_id.to_string()
                }
                (Err(err), _) => return Err(err),
            };
            conn.execute_batch("RELEASE agent_write")?;
            Ok(joined(conn, &memory_id, true, None)?)
        })?;
        // cannot be None: the row was written or checked just now
        Ok(memory.ok_or(CoreError::StorageFailure)?)
    }

    pub fn read(
        &self,
        memory_id: &str,
        read_write_set: &ReadWriteSet,
    ) -> Result<Memory, CoreError> {
        let not_found = || CoreError::MemoryNotFound(memory_id.to_string());
        if !is_id(memory_id) {
            return Err(not_found());
        }
        self.database()
            .read(|conn| match conn {
                Some(conn) => joined(conn, memory_id, false, Some(&read_write_set.readable())),
                None => Ok(None),
            })?
            .ok_or_else(not_found)
    }

    /// Records that `memory_version` of a memory was read at `at`: the memory's
    /// high-water mark, and one read fact. Best effort: a store that is missing or
    /// cannot be written never fails the read it follows.
    pub fn touch_read(
        &self,
        memory_id: &str,
        at: &str,
        memory_version: i64,
        harness: &str,
        session_id: Option<&str>,
    ) -> Result<(), CoreError> {
        if !is_id(memory_id) || !is_timestamp(at) {
            return Ok(());
        }
        let touched = self.database().write(false, |conn| {
            conn.execute(
                "UPDATE memories SET last_read_at = max(coalesce(last_read_at, ''), ?1) \
                 WHERE id = ?2 AND deleted_at IS NULL",
                params![at, memory_id],
            )?;
            // the high-water mark above must land even when the read fact below cannot
            // (an oversized harness or session id fails its CHECK): its own savepoint,
            // so only the insert -- never last_read_at -- rolls back
            conn.execute_batch("SAVEPOINT touch_read")?;
            // the version the reader was handed, not the row's current one: another
            // writer may have moved the row since the read
            let inserted = conn.execute(
                "INSERT INTO memory_reads (memory_id, memory_version, read_at, harness, \
                 session_id) SELECT id, ?1, ?2, ?3, ?4 FROM memories \
                 WHERE id = ?5 AND deleted_at IS NULL",
                params![memory_version, at, harness, session_id, memory_id],
            );
            match inserted {
                Err(rusqlite::Error::SqliteFailure(failure, _))
                    if failure.code == ErrorCode::ConstraintViolation =>
                {
                    conn.execute_batch("ROLLBACK TO touch_read")?;
                }
                other => {
                    other?;
                }
            }
            conn.execute_batch("RELEASE touch_read")?;
            Ok(())
        });
        match touched {
            Err(CoreError::StorageFailure) => Ok(()),
            other => other,
        }
    }

    /// Every described tag name of `project_id` with its description; a row memriver
    /// could not have written is passed over.
    pub fn tag_descriptions(&self, project_id: &str) -> Result<HashMap<String, String>, CoreError> {
        let rows = self.database().read(|conn| {
            let Some(conn) = conn else {
                return Ok(Vec::new());
            };
            let mut statement =
                conn.prepare("SELECT name, description FROM tags WHERE project_id = ?1")?;
            let rows = statement.query_map([project_id], values)?;
            Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
        })?;
        Ok(rows
            .into_iter()
            .filter_map(|row| match <[Value; 2]>::try_from(row) {
                Ok([Value::Text(name), Value::Text(description)])
                    if checked_tags(std::slice::from_ref(&name)).is_ok() =>
                {
                    Some((name, description))
                }
                _ => None,
            })
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use memriver_test_support::Sandbox;

    use super::*;
    use crate::models::helpers::NEXT_IDS;
    use crate::models::project::Project;
    use crate::repository::sqlite::project_store::ProjectStore;

    const CAPS: MetadataCaps = MetadataCaps {
        standing_max_count: 10,
        standing_max_chars: 200,
        tags_max: 12,
        tags_per_memory_max: 3,
    };

    /// A store with one bound project: the sandbox, the store and the project's id.
    fn store() -> (Sandbox, MemoryStore, String) {
        let sandbox = Sandbox::new();
        let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
        let (root, work) = (tmp.join("store"), tmp.join("work"));
        fs::create_dir(&work).unwrap();
        let projects = ProjectStore::with_busy_timeout(&root, &tmp.join("home"), 2000);
        let project = Project::new("work", 120).unwrap();
        let plan = projects.plan_root(&work, None).unwrap();
        projects.create(&project, &plan).unwrap();
        (
            sandbox,
            MemoryStore::with_busy_timeout(&root, 2000),
            project.id,
        )
    }

    fn plant(ids: &[&str]) {
        NEXT_IDS.with_borrow_mut(|next| next.extend(ids.iter().map(|id| id.to_string())));
    }

    fn count(store: &MemoryStore, table: &str) -> i64 {
        store
            .database()
            .read(|conn| {
                Ok(conn.unwrap().query_row(
                    &format!("SELECT count(*) FROM {table}"),
                    [],
                    |row| row.get(0),
                )?)
            })
            .unwrap()
    }

    #[test]
    fn an_id_that_is_taken_is_a_storage_failure_and_replaces_nothing() {
        let (_sandbox, store, project_id) = store();
        let apply = |ops: &[Op]| store.apply(ops, "cli", None, &|_, _| None, &CAPS);
        let create = |body: &str| Op::create(&project_id, "user", "", body);
        plant(&["cccccccccc", "mmmmmmmmmm"]);
        let first = apply(&[create("the first")]).unwrap();
        assert_eq!(
            (first.change_id.as_str(), first.steps[0].memory_id.as_str()),
            ("cccccccccc", "mmmmmmmmmm")
        );
        // the memory's id is drawn again: nothing is replaced, and no second id is tried
        plant(&["dddddddddd", "mmmmmmmmmm", "nnnnnnnnnn"]);
        assert_eq!(
            apply(&[create("another")]),
            Err(WriteError::Core(CoreError::StorageFailure))
        );
        assert_eq!(
            NEXT_IDS.with_borrow_mut(|next| next.pop_front()),
            Some("nnnnnnnnnn".to_string())
        );
        // the change's id, too
        plant(&["cccccccccc", "pppppppppp"]);
        assert_eq!(
            apply(&[create("another")]),
            Err(WriteError::Core(CoreError::StorageFailure))
        );
        NEXT_IDS.with_borrow_mut(|next| next.clear());
        // a deleted memory keeps its id: it is never drawn for another
        apply(&[Op::soft_delete("mmmmmmmmmm", 1)]).unwrap();
        plant(&["eeeeeeeeee", "mmmmmmmmmm"]);
        assert_eq!(
            apply(&[create("another")]),
            Err(WriteError::Core(CoreError::StorageFailure))
        );
        // on the agent path it is the same failure
        plant(&["ffffffffff", "mmmmmmmmmm"]);
        let agent = ReadWriteSet {
            project_id: Some(project_id.clone()),
            global_project_id: None,
        };
        assert_eq!(
            store.write(&create("another"), &agent, "mcp", None, &|_, _| None, &CAPS),
            Err(WriteError::Core(CoreError::StorageFailure))
        );
        assert_eq!(
            (count(&store, "memories"), count(&store, "changes")),
            (1, 2)
        );
        let body: String = store
            .database()
            .read(|conn| {
                Ok(conn
                    .unwrap()
                    .query_row("SELECT body FROM memories", [], |row| row.get(0))?)
            })
            .unwrap();
        assert_eq!(body, "the first");
    }
}
