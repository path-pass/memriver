//! SessionStart, and the notices that lead an injected index.

use memriver_core::Services;
use memriver_core::application::sessions::StartOutcome;
use memriver_core::models::context::{ProjectContext, ProjectContextState};
use memriver_core::models::errors::CoreError;
use memriver_core::models::helpers::cut;
use memriver_core::models::session::{Harness, MoveOutcome};
use memriver_core::settings::HEADER_FIELD_CHARS;

use super::compose::compose;
use super::{
    HookResult, INVALID_INPUT, Place, STORE_UNAVAILABLE, additional_context, object, session_key,
    text,
};
use crate::cli::views::visible;
use crate::protocol_text::{MOVE_SOURCE_NONE, Moved, move_notice, pending_notice, project_phrase};

/// A stored name or directory as a notice shows it: neutralised, cut like a header field.
pub(super) fn field(text: &str) -> String {
    cut(&visible(text), HEADER_FIELD_CHARS).to_string()
}

/// The pending notice for `context`, naming the stored entry and its candidate.
pub(super) fn pending(services: &Services, context: &ProjectContext) -> Result<String, CoreError> {
    let candidate = services.session.pending_candidate(context)?;
    let entry = field(&services.session.entry_of(context)?.unwrap_or_default());
    let target = candidate.map(|project| project_phrase(&field(&project.name), &project.id));
    Ok(pending_notice(&entry, target.as_deref()))
}

/// The project's name; its id when the project is gone or cannot be read.
fn project_name(services: &Services, project_id: &str) -> String {
    match services.project.read_project(project_id) {
        Ok(project) => field(&project.name),
        Err(_) => project_id.to_string(),
    }
}

/// What a `/cd` did to the session, worded for the state it left.
fn moved(services: &Services, outcome: &MoveOutcome) -> String {
    let source = |project_id: &Option<String>| match project_id {
        None => MOVE_SOURCE_NONE.to_string(),
        Some(id) => project_phrase(&project_name(services, id), id),
    };
    let entry = field(&outcome.to_entry_cwd);
    if outcome.pending {
        let (old, new) = (
            source(&outcome.from_candidate_id),
            source(&outcome.to_candidate_id),
        );
        return move_notice(
            "",
            &entry,
            Moved::Pending {
                old: &old,
                new: &new,
            },
        );
    }
    let from = source(&outcome.from_project_id);
    match &outcome.to_project_id {
        None => move_notice(
            &from,
            &entry,
            Moved::Nowhere {
                unresolved: outcome.degraded,
            },
        ),
        Some(id) => {
            let name = project_name(services, id);
            move_notice(&from, &entry, Moved::Project { name: &name, id })
        }
    }
}

/// What leads the index: the move a `/cd` made, then the pending notice.
pub(super) fn notice(
    services: &Services,
    context: &ProjectContext,
    outcome: Option<&MoveOutcome>,
) -> Result<String, CoreError> {
    let mut parts = Vec::new();
    if let Some(outcome) = outcome.filter(|outcome| outcome.changed()) {
        parts.push(moved(services, outcome));
    }
    if context.state == ProjectContextState::Pending {
        parts.push(pending(services, context)?);
    }
    Ok(parts.join("\n"))
}

/// The header and the index body the context grants, as the MCP server shows them.
pub(super) fn index(services: &Services, context: &ProjectContext) -> Result<String, CoreError> {
    Ok(format!(
        "{}\n{}",
        context.header,
        services.memory.index(context, None)?
    ))
}

pub(super) fn session_start(harness: Harness, payload: &str, place: &Place) -> HookResult {
    let Some(payload) = object(payload) else {
        return HookResult::stderr(INVALID_INPUT);
    };
    let Some(key) = session_key(harness, &payload) else {
        return HookResult::default();
    };
    let services = match place.services() {
        Ok(services) => services,
        Err(error) => return HookResult::settings(&error),
    };
    // no store, nothing to route: silent, and nothing is created
    if !services.session.store_exists() {
        return HookResult::default();
    }
    let source = text(&payload, "source").unwrap_or("");
    let composed = || -> Result<String, CoreError> {
        let StartOutcome { context, moved } = services.session.start_session(
            &key,
            source,
            &place.entry_dir(harness, &payload),
            text(&payload, "transcript_path"),
            text(&payload, "cwd"),
        )?;
        let notice = notice(&services, &context, moved.as_ref())?;
        let index = index(&services, &context)?;
        Ok(compose(&index, Some(source), harness, Some(&notice)))
    };
    match composed() {
        Ok(text) => HookResult::stdout(&additional_context("SessionStart", text)),
        // refused: nothing is read or written
        Err(CoreError::StoreNeedsUpgrade(_)) => HookResult::default(),
        Err(_) => HookResult::stderr(STORE_UNAVAILABLE),
    }
}
