//! Human management of memories: `history`, `restore`, `undo`, `delete`.
//!
//! None of this is reachable over MCP. Every rule and every write is the core's; this
//! module shows plans, asks, and words refusals. When a write is refused because the
//! store moved on while the prompt was open, a person at the prompt is shown the new
//! state and asked again; under `--yes` or `--confirm` the command fails instead (exit
//! 2), because nobody looked at the new state.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use memriver_core::Services;
use memriver_core::models::changes::{
    Change, Confirmation, HardDeletePlan, MemoryVersion, Op, OpName, Step,
};
use memriver_core::models::errors::{
    BatchConflictReason, CoreError, UndoRefusedReason, WriteError,
};
use memriver_core::models::helpers::single_line;
use memriver_core::models::memory::Memory;

use super::args::{Delete, History, PositiveInt, Restore, Undo};
use super::views::{STORE_UNREADABLE, body, cue, echoed, shell_quote, visible};
use super::{Invocation, Outcome, confirm, services};

const STORE_UNWRITABLE: &str = "refused: the memory store could not be written";
const STORE_MOVED: &str =
    "refused: the memory store changed while it was read; run the command again";
const DECLINED: &str = "aborted; nothing was changed";
const CHANGED_BY: &str = "human";

fn say_line(line: &str, code: i32, inv: &mut Invocation) -> Outcome {
    inv.say(format_args!("{line}\n"));
    Ok(code)
}

fn no_such_memory(memory_id: &str, inv: &mut Invocation) -> Outcome {
    say_line(&format!("no such memory: {}", echoed(memory_id)), 2, inv)
}

fn place(project_id: &str, global_id: Option<&str>) -> String {
    match global_id {
        Some(global_id) if global_id == project_id => "global".to_string(),
        _ => format!("project {project_id}"),
    }
}

/// A restore or an undo whose result would break a metadata limit of settings.toml.
fn cap_refusal(cap: impl std::fmt::Display) -> String {
    format!("refused ({cap}): the result would exceed the {cap} setting; nothing was changed")
}

// --- history ---

/// Who made a version: its change, or "imported" for one with no recorded change.
fn origin(version: &MemoryVersion) -> String {
    let Some(change) = &version.change else {
        return "imported".to_string();
    };
    format!(
        "{}  {}{}  change {}",
        visible(&change.at),
        visible(&change.changed_by),
        via(change),
        change.change_id
    )
}

fn via(change: &Change) -> String {
    match change.changed_via.as_deref() {
        Some(via) if !via.is_empty() => format!(" ({})", visible(via)),
        _ => String::new(),
    }
}

fn sources(version: &MemoryVersion) -> String {
    let cited: Vec<String> = version
        .sources
        .iter()
        .map(|source| format!("{} v{}", visible(&source.memory_id), source.version))
        .collect();
    cited.join(", ")
}

fn deleted_mark(version: &MemoryVersion) -> &'static str {
    if version.deleted { "  [deleted]" } else { "" }
}

fn version_line(version: &MemoryVersion) -> String {
    let mut line = format!(
        "v{}  {}{}  {}\n",
        version.version,
        origin(version),
        deleted_mark(version),
        visible(&single_line(&version.description))
    );
    let sources = sources(version);
    if !sources.is_empty() {
        line.push_str(&format!("    sources: {sources}\n"));
    }
    line
}

fn version_in_full(version: &MemoryVersion) -> String {
    let sources = sources(version);
    let lines = [
        format!("id: {}", version.memory_id),
        format!("version: {}", version.version),
        format!("type: {}", version.type_),
        format!("trust: {}", version.trust),
        format!("sync: {}", version.sync),
        format!("deleted: {}", version.deleted),
        format!("change: {}", origin(version)),
        format!(
            "sources: {}",
            if sources.is_empty() { "-" } else { &sources }
        ),
        format!(
            "description: {}",
            visible(&single_line(&version.description))
        ),
    ];
    format!("{}\n---\n{}\n", lines.join("\n"), body(&version.body))
}

/// Every version of a memory, oldest first; none for an unknown id.
fn versions(services: &Services, memory_id: &str) -> Result<Vec<MemoryVersion>, CoreError> {
    let mut versions = match services.memory.versions(memory_id) {
        Err(CoreError::MemoryNotFound(_)) => Vec::new(),
        versions => versions?,
    };
    versions.sort_by_key(|version| version.version);
    Ok(versions)
}

fn chosen<'v>(versions: &'v [MemoryVersion], number: &PositiveInt) -> Option<&'v MemoryVersion> {
    // a number no version can have is larger than every stored one, never equal to it
    let wanted: i64 = number.to_string().parse().ok()?;
    versions.iter().find(|version| version.version == wanted)
}

pub(crate) fn history(args: &History, inv: &mut Invocation) -> Outcome {
    let (services, _) = services(args.root.as_deref(), inv)?;
    let versions = match versions(&services, &args.memory_id) {
        Ok(versions) => versions,
        Err(CoreError::StorageFailure) => return say_line(STORE_UNREADABLE, 2, inv),
        Err(other) => return Err(other.into()),
    };
    let Some(first) = versions.first() else {
        return no_such_memory(&args.memory_id, inv);
    };
    let Some(show) = &args.show else {
        let lines: String = versions.iter().map(version_line).collect();
        inv.say(format_args!("{lines}"));
        return Ok(0);
    };
    match chosen(&versions, show) {
        Some(version) => inv.say(format_args!("{}", version_in_full(version))),
        None => {
            return say_line(
                &format!("no such version: {} v{show}", first.memory_id),
                2,
                inv,
            );
        }
    }
    Ok(0)
}

// --- restore ---

fn state(version: &MemoryVersion) -> String {
    format!(
        "{}{}",
        cue(&version.description, &version.body),
        deleted_mark(version)
    )
}

fn restore_plan(current: &MemoryVersion, target: &MemoryVersion) -> String {
    let cited = |version: &MemoryVersion| version.sources.iter().cloned().collect::<BTreeSet<_>>();
    let differing = [
        (
            "content",
            (&current.description, &current.body) != (&target.description, &target.body),
        ),
        ("sources", cited(current) != cited(target)),
        (
            "level and tags",
            (current.level, &current.tags) != (target.level, &target.tags),
        ),
        ("deleted state", current.deleted != target.deleted),
    ];
    let changes: Vec<&str> = differing
        .iter()
        .filter(|(_, differs)| *differs)
        .map(|(name, _)| *name)
        .collect();
    format!(
        "memriver restore: {} from v{} to the state of v{}\n  now (v{}): {}\n  target (v{}): \
         {}\n  changes: {}\n",
        current.memory_id,
        current.version,
        target.version,
        current.version,
        state(current),
        target.version,
        state(target),
        if changes.is_empty() {
            "nothing".to_string()
        } else {
            changes.join(", ")
        }
    )
}

pub(crate) fn restore(args: &Restore, inv: &mut Invocation) -> Outcome {
    let to = &args.to_version;
    loop {
        let (services, _) = services(args.root.as_deref(), inv)?;
        let versions = match versions(&services, &args.memory_id) {
            Ok(versions) => versions,
            Err(CoreError::StorageFailure) => return say_line(STORE_UNREADABLE, 2, inv),
            Err(other) => return Err(other.into()),
        };
        let Some(current) = versions.last() else {
            return no_such_memory(&args.memory_id, inv);
        };
        let id = &current.memory_id;
        let Some(target) = chosen(&versions, to) else {
            return say_line(&format!("no such version: {id} v{to}"), 2, inv);
        };
        inv.say(format_args!("{}", restore_plan(current, target)));
        if let Some(code) = confirm(inv.io, args.yes, DECLINED).refused() {
            return Ok(code);
        }
        let restored =
            services
                .memory
                .restore(id, current.version, target.version, CHANGED_BY, None);
        let line = match restored {
            Ok(change) => {
                let new = change.steps.first().map_or(0, |step| step.after_version);
                let done = format!(
                    "restored {id} to the state of v{to} as v{new} (change {})",
                    change.change_id
                );
                return say_line(&done, 0, inv);
            }
            Err(WriteError::Core(CoreError::BatchConflict { reason, .. })) => match reason {
                BatchConflictReason::Version if !args.yes => {
                    inv.say(format_args!(
                        "{id} changed while waiting; nothing was changed yet\n"
                    ));
                    continue;
                }
                BatchConflictReason::Version => format!(
                    "refused (version): {id} changed while waiting; nothing was changed; run \
                     the command again"
                ),
                BatchConflictReason::SameState => format!(
                    "refused (same-state): the state of v{to} is already the current one; \
                     nothing was changed"
                ),
                reason => format!("refused ({reason}): nothing was changed"),
            },
            Err(WriteError::Core(CoreError::ContentRejected { rule_id, .. })) => format!(
                "refused (content policy): the state of v{to} fails rule {rule_id}; nothing was \
                 changed"
            ),
            Err(WriteError::Core(CoreError::CapExceeded { cap, .. })) => cap_refusal(cap),
            Err(WriteError::Core(CoreError::MemoryNotFound(_))) => {
                return no_such_memory(&args.memory_id, inv);
            }
            Err(WriteError::Core(CoreError::StorageFailure)) => STORE_UNWRITABLE.to_string(),
            Err(other) => return Err(other.into()),
        };
        return say_line(&line, 2, inv);
    }
}

// --- undo ---

fn step_line(step: &Step, memory: &Memory, global_id: Option<&str>) -> String {
    let before = step
        .before_version
        .map(|before| format!("v{before}->"))
        .unwrap_or_default();
    format!(
        "  {}  {}  {} {before}v{}  {}\n",
        step.memory_id,
        place(&memory.project_id, global_id),
        step.op,
        step.after_version,
        cue(&memory.description, &memory.body)
    )
}

fn inverse_line(step: &Step) -> String {
    match (step.op, step.before_version) {
        (OpName::Create, _) => format!("  {}: soft delete\n", step.memory_id),
        (_, Some(before)) => format!("  {}: restore the state of v{before}\n", step.memory_id),
        (_, None) => format!("  {}: restore the state of vNone\n", step.memory_id),
    }
}

fn change_plan(
    change: &Change,
    memories: &HashMap<&str, Memory>,
    global_id: Option<&str>,
) -> String {
    let undoes = match change.undoes.as_deref() {
        Some(undone) if !undone.is_empty() => format!(", an undo of {undone}"),
        _ => String::new(),
    };
    let mut text = format!(
        "memriver undo: change {} at {} by {}{}{undoes}\n",
        change.change_id,
        visible(&change.at),
        visible(&change.changed_by),
        via(change)
    );
    for step in &change.steps {
        text.push_str(&step_line(
            step,
            &memories[step.memory_id.as_str()],
            global_id,
        ));
    }
    let stored = i64::try_from(change.steps.len()).unwrap_or(i64::MAX);
    let missing = change.step_count.saturating_sub(stored);
    if missing != 0 {
        let steps = if missing == 1 { "step" } else { "steps" };
        text.push_str(&format!(
            "  ({missing} more {steps} removed by a hard delete)\n"
        ));
    }
    text.push_str("the undo:\n");
    text.extend(change.steps.iter().map(inverse_line));
    text
}

pub(crate) fn undo(args: &Undo, inv: &mut Invocation) -> Outcome {
    let (services, _) = services(args.root.as_deref(), inv)?;
    let read = || -> Result<_, CoreError> {
        let Some(change) = services.memory.change(&args.change_id)? else {
            return Ok(None);
        };
        let global_id = services.project.global_project_id()?;
        let mut memories = Vec::new();
        for step in &change.steps {
            memories.push(services.memory.show(&step.memory_id, true)?);
        }
        Ok(Some((change, global_id, memories)))
    };
    let (change, global_id, memories) = match read() {
        Ok(Some(read)) => read,
        Ok(None) => {
            let line = format!("refused (not-found): no change {}", echoed(&args.change_id));
            return say_line(&line, 2, inv);
        }
        Err(CoreError::MemoryNotFound(_)) => return say_line(STORE_MOVED, 2, inv),
        Err(CoreError::StorageFailure) => return say_line(STORE_UNREADABLE, 2, inv),
        Err(other) => return Err(other.into()),
    };
    let id = &change.change_id;
    let by_id: HashMap<&str, Memory> = change
        .steps
        .iter()
        .map(|step| step.memory_id.as_str())
        .zip(memories)
        .collect();
    inv.say(format_args!(
        "{}",
        change_plan(&change, &by_id, global_id.as_deref())
    ));
    if let Some(code) = confirm(inv.io, args.yes, DECLINED).refused() {
        return Ok(code);
    }
    let line = match services.memory.undo(id, CHANGED_BY, None) {
        Ok(undo) => {
            let done = format!("undone change {id} by change {}", undo.change_id);
            return say_line(&done, 0, inv);
        }
        Err(WriteError::Core(CoreError::UndoRefused { reason, memory_ids })) => match reason {
            UndoRefusedReason::NotFound => format!("refused (not-found): no change {id}"),
            UndoRefusedReason::HardDeleted => format!(
                "refused (hard-deleted): a hard delete removed part of change {id}; it cannot \
                 be undone"
            ),
            UndoRefusedReason::Changed => format!(
                "refused (changed): {} changed after change {id}; nothing was changed; use \
                 memriver history and memriver restore",
                memory_ids.join(", ")
            ),
        },
        Err(WriteError::Core(CoreError::ContentRejected { rule_id, .. })) => format!(
            "refused (content policy): the undo would restore content failing rule {rule_id}; \
             nothing was changed"
        ),
        Err(WriteError::Core(CoreError::BatchConflict { reason, .. })) => format!(
            "refused ({reason}): the undo conflicts with the current state; nothing was changed"
        ),
        Err(WriteError::Core(CoreError::CapExceeded { cap, .. })) => cap_refusal(cap),
        Err(WriteError::Core(CoreError::StorageFailure)) => STORE_UNWRITABLE.to_string(),
        Err(other) => return Err(other.into()),
    };
    say_line(&line, 2, inv)
}

// --- delete ---

/// `--version` (soft) and `--hard` [`--dry-run` | `--confirm`] exclude each other; the
/// caller has refused every other combination.
pub(crate) fn delete(args: &Delete, inv: &mut Invocation) -> Outcome {
    let (services, root) = services(args.root.as_deref(), inv)?;
    match &args.version {
        Some(version) if !args.hard => soft_delete(&services, args, version, inv),
        // the root is named in the command to run only when this command was given one
        _ => hard_delete(
            &services,
            args,
            args.root.as_ref().map(|_| root.as_path()),
            inv,
        ),
    }
}

fn soft_delete(
    services: &Services,
    args: &Delete,
    version: &PositiveInt,
    inv: &mut Invocation,
) -> Outcome {
    let read = || -> Result<_, CoreError> {
        let context = services.project.open_project_context(inv.cwd)?;
        let global_id = services.project.global_project_id()?;
        Ok((
            context,
            global_id,
            services.memory.show(&args.memory_id, false)?,
        ))
    };
    let (context, global_id, memory) = match read() {
        Ok(read) => read,
        Err(CoreError::MemoryNotFound(_)) => return no_such_memory(&args.memory_id, inv),
        Err(CoreError::StorageFailure) => return say_line(STORE_UNREADABLE, 2, inv),
        Err(other) => return Err(other.into()),
    };
    let (id, project_id) = (&memory.id, &memory.project_id);
    // a global entry is deleted by id as a management delete, wherever this runs; any
    // other entry only from its own project's directory. The checks inside the
    // transaction still decide when it is written.
    let is_global = global_id.as_ref() == Some(project_id);
    if !is_global
        && !context
            .read_write_set
            .writable()
            .contains(project_id.as_str())
    {
        // `memriver show` displays it, so "no such memory" would mislead
        let line = format!(
            "refused: {id} belongs to project {project_id}, not this directory's project; run \
             memriver delete from that project's directory"
        );
        return say_line(&line, 2, inv);
    }
    inv.say(format_args!(
        "memriver delete: {id} [{}] in {}: {}  (soft)\n",
        memory.type_,
        place(project_id, global_id.as_deref()),
        cue(&memory.description, &memory.body)
    ));
    if let Some(code) = confirm(inv.io, args.yes, DECLINED).refused() {
        return Ok(code);
    }
    let expected = version.as_i64();
    let deleted = if is_global {
        // a management delete, like restore and undo: the check above is this command's
        // own; the version check of the write still decides
        let ops = [Op::soft_delete(id, expected)];
        services.memory.apply(&ops, CHANGED_BY, None).map(drop)
    } else {
        let delete = services
            .memory
            .delete(id, expected, &context, CHANGED_BY, None);
        delete.map(drop)
    };
    let changed =
        format!("refused: {id} changed since version {version}; run memriver show {id} and retry");
    let line = match deleted {
        Ok(()) => return say_line(&format!("deleted {id}"), 0, inv),
        Err(WriteError::Core(error)) => match error {
            CoreError::MemoryNotFound(_) => return no_such_memory(&args.memory_id, inv),
            // an ordinary project's entry whose project changed role between the plan and
            // the write (the global path never answers these)
            CoreError::GlobalReadOnly | CoreError::ProjectUnavailable { .. } => {
                "refused: the memory's project changed while waiting; run the command again"
                    .to_string()
            }
            // hard-deleted meanwhile
            CoreError::BatchConflict {
                reason: BatchConflictReason::Missing,
                ..
            } => return no_such_memory(&args.memory_id, inv),
            CoreError::BatchConflict { .. } | CoreError::VersionConflict(_) => changed,
            CoreError::ContentRejected { rule_id, .. } => format!(
                "refused (content policy): {} fails rule {}; remove it with memriver delete {} \
                 --hard",
                visible(id),
                visible(&rule_id),
                visible(id)
            ),
            CoreError::StorageFailure => STORE_UNWRITABLE.to_string(),
            other => return Err(other.into()),
        },
        Err(other) => return Err(other.into()),
    };
    say_line(&line, 2, inv)
}

fn plan_text(services: &Services, plan: &HardDeletePlan, global_id: Option<&str>) -> String {
    let count = plan.items.len();
    let memories = if count == 1 { "memory" } else { "memories" };
    let mut lines = vec![format!(
        "memriver delete --hard: removes {count} {memories} with every version, source and read:"
    )];
    for item in &plan.items {
        // the plan stays exact when a member cannot be shown; only its cue is missing
        let cue = match services.memory.show(&item.memory_id, true) {
            Ok(memory) => cue(&memory.description, &memory.body),
            Err(_) => String::new(),
        };
        lines.push(format!(
            "  {}  {}  v{}{}  {cue}",
            visible(&item.memory_id),
            visible(&place(&item.project_id, global_id)),
            item.version,
            if item.deleted { "  (deleted)" } else { "" }
        ));
        lines.extend(item.citations.iter().map(|citation| {
            format!(
                "    because {} v{}{} cites {} v{}",
                visible(&citation.citing_id),
                citation.citing_version,
                if citation.citing_current {
                    ""
                } else {
                    " (history)"
                },
                visible(&citation.cited_id),
                citation.cited_version
            )
        }));
    }
    lines.push("  changes that touched them stay in the log but can no longer be undone".into());
    lines.join("\n") + "\n"
}

fn confirm_command(plan: &HardDeletePlan, root: Option<&Path>) -> String {
    let root = root
        .map(|root| format!(" --root {}", shell_quote(&root.to_string_lossy())))
        .unwrap_or_default();
    format!(
        "to delete exactly these, run: memriver delete {} --hard --confirm {}{root}\n",
        plan.target, plan.code
    )
}

fn purged(memory_ids: &[String], inv: &mut Invocation) -> Outcome {
    say_line(&format!("purged {}", memory_ids.join(", ")), 0, inv)
}

fn hard_delete(
    services: &Services,
    args: &Delete,
    root: Option<&Path>,
    inv: &mut Invocation,
) -> Outcome {
    match hard_delete_steps(services, args, root, inv) {
        Err(CoreError::MemoryNotFound(_)) => no_such_memory(&args.memory_id, inv),
        Err(CoreError::StorageFailure) => say_line(
            "refused: the memory store failed; nothing was deleted",
            2,
            inv,
        ),
        Err(other) => Err(other.into()),
        Ok(code) => Ok(code),
    }
}

fn hard_delete_steps(
    services: &Services,
    args: &Delete,
    root: Option<&Path>,
    inv: &mut Invocation,
) -> Result<i32, CoreError> {
    let id = &args.memory_id;
    let global_id = services.project.global_project_id()?;
    let global_id = global_id.as_deref();
    if let Some(code) = &args.confirm {
        // the second step: the code names the plan the dry run printed, and the core
        // makes the plan again and compares; nothing was stored in between
        return match services
            .maintenance
            .hard_delete(id, Confirmation::Code(code))
        {
            Ok(deleted) => Ok(purged(&deleted, inv).unwrap_or(0)),
            Err(CoreError::PlanChanged(plan)) => {
                inv.say(format_args!(
                    "refused: the plan changed since it was printed; nothing was deleted\n{}{}",
                    plan_text(services, &plan, global_id),
                    confirm_command(&plan, root)
                ));
                Ok(2)
            }
            Err(other) => Err(other),
        };
    }
    let mut plan = services.maintenance.plan_hard_delete(id)?;
    if args.dry_run {
        inv.say(format_args!(
            "{}{}",
            plan_text(services, &plan, global_id),
            confirm_command(&plan, root)
        ));
        return Ok(0);
    }
    loop {
        inv.say(format_args!("{}", plan_text(services, &plan, global_id)));
        if let Some(code) = confirm(inv.io, args.yes, DECLINED).refused() {
            return Ok(code);
        }
        let deleted = {
            let expected = plan.expected();
            let shown = Confirmation::Expected(&expected);
            services.maintenance.hard_delete(id, shown)
        };
        plan = match deleted {
            Ok(deleted) => return Ok(purged(&deleted, inv).unwrap_or(0)),
            Err(CoreError::PlanChanged(_)) if args.yes => {
                inv.say(format_args!(
                    "refused: the plan changed while waiting; nothing was deleted; run the \
                     command again\n"
                ));
                return Ok(2);
            }
            Err(CoreError::PlanChanged(plan)) => {
                inv.say(format_args!(
                    "the plan changed while waiting; nothing was deleted yet\n"
                ));
                plan
            }
            Err(other) => return Err(other),
        };
    }
}
