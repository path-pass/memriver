//! UserPromptSubmit, Stop, SessionEnd and PreToolUse.

use memriver_core::models::context::ProjectContextState;
use memriver_core::models::errors::CoreError;
use memriver_core::models::session::{Harness, is_call_id};
use serde_json::{Value, json};

use super::compose::compose;
use super::session_start::{index, notice, pending};
use super::{HookResult, Place, additional_context, object, session_key, text};
use crate::protocol_text::STOP_NUDGE;

/// Counts and records one prompt. Says something only after a `/cd` that changed the
/// session's project (the notice and the new index) or when this prompt created a
/// pending row (the pending notice alone). Never a message: the prompt must not leak.
pub(super) fn user_prompt_submit(harness: Harness, payload: &str, place: &Place) -> HookResult {
    let Some(payload) = object(payload) else {
        return HookResult::default();
    };
    // a Codex sub-agent's prompt is not the user's: never recorded or counted
    let marked = |name: &str| payload.get(name).is_some_and(|value| !value.is_null());
    if harness == Harness::Codex && (marked("agent_id") || marked("agent_type")) {
        return HookResult::default();
    }
    let Some(key) = session_key(harness, &payload) else {
        return HookResult::default();
    };
    let services = match place.services() {
        Ok(services) => services,
        Err(error) => return HookResult::settings(&error),
    };
    if !services.session.store_exists() {
        return HookResult::default();
    }
    let said = || -> Result<Option<String>, CoreError> {
        let observed = services.session.observe_prompt(
            &key,
            payload.get("prompt"),
            &place.entry_dir(harness, &payload),
            text(&payload, "transcript_path"),
            text(&payload, "cwd"),
        )?;
        let context = &observed.context;
        if let Some(moved) = observed.moved.as_ref().filter(|moved| moved.changed()) {
            // a failure from here on loses the notice, and the next prompt (no move
            // left to apply) stays silent
            let notice = notice(&services, context, Some(moved))?;
            let index = index(&services, context)?;
            return Ok(Some(compose(&index, None, harness, Some(&notice))));
        }
        // only the prompt that created a pending row says so: SessionStart already told
        // a session whose row it made
        if observed.created_pending && context.state == ProjectContextState::Pending {
            return Ok(Some(pending(&services, context)?));
        }
        Ok(None)
    };
    match said() {
        Ok(Some(text)) => HookResult::stdout(&additional_context("UserPromptSubmit", text)),
        _ => HookResult::default(),
    }
}

/// The save nudge: only a first Stop (the JSON literal `false`) of a session that is
/// due. The loop guard is the only thing between a nudge and a hook that blocks every
/// Stop, so a missing key, a string "false" or an unreadable payload ends the session.
pub(super) fn stop(harness: Harness, payload: &str, place: &Place) -> HookResult {
    let Some(payload) = object(payload) else {
        return HookResult::default();
    };
    if payload.get("stop_hook_active") != Some(&Value::Bool(false)) {
        return HookResult::default();
    }
    let Some(key) = session_key(harness, &payload) else {
        return HookResult::default();
    };
    let Some(services) = place.light_services() else {
        return HookResult::default();
    };
    let session = &services.session;
    if !(session.store_exists() && session.stop_decision(&key).unwrap_or(false)) {
        return HookResult::default();
    }
    HookResult::stdout(&json!({"decision": "block", "reason": STOP_NUDGE}))
}

/// Records the end; silent whatever happens, but for a settings error.
pub(super) fn session_end(harness: Harness, payload: &str, place: &Place) -> HookResult {
    let key = object(payload).and_then(|payload| session_key(harness, &payload));
    let Some(key) = key else {
        return HookResult::default();
    };
    let services = match place.services() {
        Ok(services) => services,
        Err(error) => return HookResult::settings(&error),
    };
    if services.session.store_exists() {
        let _ = services.session.end_session(&key);
    }
    HookResult::default()
}

/// Claude Code only: which session makes the memriver tool call about to run, and from
/// which directory. Its MCP server keeps its first session id across `/clear` and an
/// in-app `/resume`, and only the call names the current one. Never prints anything,
/// so it can neither block nor change the call.
pub(super) fn pre_tool_use(harness: Harness, payload: &str, place: &Place) -> HookResult {
    // not installed for Codex: its calls name their session
    if harness != Harness::ClaudeCode {
        return HookResult::default();
    }
    let Some(payload) = object(payload) else {
        return HookResult::default();
    };
    let call_id = text(&payload, "tool_use_id").filter(|id| is_call_id(id));
    if let (Some(key), Some(call_id)) = (session_key(harness, &payload), call_id) {
        // a missing store stays missing: the write is a no-op without one
        if let Some(services) = place.light_services() {
            let _ = services
                .session
                .record_tool_call(&key, call_id, text(&payload, "cwd"));
        }
    }
    HookResult::default()
}
