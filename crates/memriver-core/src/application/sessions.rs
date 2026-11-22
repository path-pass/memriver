//! The session service: harness sessions -- start, prompts, end, Stop, tool calls.
//!
//! The session store owns the session rows; this service registers sessions from their
//! entry directory, builds the context a stored row grants, answers the save-watermark
//! and pending questions a memory write asks, and hands the human commands their
//! session listing.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::Path;

use serde_json::Value;

use super::contexts::{
    NONE_HEADER, PENDING_HEADER, PENDING_NO_CANDIDATE_HEADER, SESSION_NONE_HEADER,
    SESSION_PROJECT_GONE_HEADER, UNIDENTIFIED_HEADER, degraded_context, no_project_context,
    registered_context, unavailable_context,
};
use super::memory::PolicySource;
use crate::models::context::{ProjectContext, ProjectContextState, ReadWriteSet, ResolutionState};
use crate::models::errors::{CoreError, WriteError};
use crate::models::helpers::{cut, now, single_line};
use crate::models::project::Project;
use crate::models::session::{
    CallRecord, Harness, MoveOutcome, PromptEntry, PromptOmission, Session, SessionKey,
    SessionMove, SessionOrigin, SessionStatus,
};
use crate::repository::directories::{
    RootState, UNRESOLVABLE_START, canonical, root_state, transcript_moved, usable_directory,
};
use crate::repository::sqlite::project_store::ProjectStore;
use crate::repository::sqlite::session_store::{MoveTo, SessionStore, core};
use crate::repository::worktree::git_facts;
use crate::settings::{
    Env, HEADER_FIELD_CHARS, SESSION_PROMPT_CHARS, SESSION_PROMPT_SCAN_MAX_BYTES,
    SESSION_RECENT_PROMPTS, SESSION_SEARCH_LIMIT_DEFAULT, SESSION_SEARCH_LIMIT_MAX,
    SESSION_SUMMARY_MAX_CHARS, STOP_NUDGE_INTERVAL_PROMPTS, STOP_NUDGE_MIN_PROMPTS,
    TOOL_CALL_RETENTION_S,
};

// a new session with no row: these sources start one afresh, so it registers at once;
// resume and compact continue one memriver never saw, so it waits for the user
const FRESH_SOURCES: [&str; 3] = ["startup", "clear", "fork"];
const WORKTREE_UNMAPPED: &str = "the git worktree could not be mapped to its main working tree";
const SESSION_PROJECT_MISSING: &str = "session project missing";

/// What SessionStart answers: the stored row's context, and the move a `compact`
/// applied first.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StartOutcome {
    pub context: ProjectContext,
    pub moved: Option<MoveOutcome>,
}

/// What UserPromptSubmit answers: the context, whether this call created the row, and
/// the move it applied first.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromptOutcome {
    pub context: ProjectContext,
    pub created_pending: bool,
    pub moved: Option<MoveOutcome>,
}

/// A registration directory as resolved: its canonical spelling, project and branch.
struct Entry {
    cwd: String,
    project: Option<Project>,
    branch: Option<String>,
}

pub struct SessionService {
    session_store: SessionStore,
    project_store: ProjectStore,
    /// Loaded by the first prompt or summary that is checked: a hook that records no
    /// prompt never pays for the rules.
    policy: PolicySource,
    /// What git is run with, when a registration or a move has to ask it.
    env: Env,
}

fn unavailable(reason: &str) -> CoreError {
    CoreError::ProjectUnavailable {
        reason: reason.to_string(),
    }
}

fn usable(directory: Option<&str>) -> Option<&str> {
    directory.filter(|directory| usable_directory(directory))
}

/// The integrity rule resolution applies: an offline root is fine, a re-pointed or
/// uncheckable one is not.
fn root_is_intact(root: &str) -> bool {
    matches!(root_state(root), RootState::Ok | RootState::Missing)
}

impl SessionService {
    pub fn new(
        session_store: SessionStore,
        project_store: ProjectStore,
        policy: PolicySource,
        env: Env,
    ) -> SessionService {
        SessionService {
            session_store,
            project_store,
            policy,
            env,
        }
    }

    /// False only for a store known to be absent (nothing to route, nothing to write).
    /// One that cannot be checked counts as present: its operation then reports it as
    /// unavailable.
    pub fn store_exists(&self) -> bool {
        self.session_store.store_exists().unwrap_or(true)
    }

    /// SessionStart: the stored row's context, registering the session when it has
    /// none. A store that cannot be read is the unavailable context; a store of
    /// another schema version is the error.
    pub fn start_session(
        &self,
        key: &SessionKey,
        source: &str,
        entry_dir: &str,
        transcript_path: Option<&str>,
        current_dir: Option<&str>,
    ) -> Result<StartOutcome, CoreError> {
        let answer = |context, moved| Ok(StartOutcome { context, moved });
        // first: without a store no directory question (git included) is asked
        if !self.store_exists() {
            return answer(self.storeless_context(key), None);
        }
        let started = || -> Result<StartOutcome, CoreError> {
            if let Some(stored) = self.session_store.get(key)? {
                return self.continue_session(key, stored, source, transcript_path, current_dir);
            }
            let fresh = FRESH_SOURCES.contains(&source);
            let seed = match self.new_session(key, entry_dir, transcript_path, fresh)? {
                Ok(seed) => seed,
                Err(context) => return answer(context, None),
            };
            // the stored winner answers, never this call's own computation
            match self.session_store.register(&seed).map_err(core)? {
                Some(winner) => answer(self.context_of(&winner)?, None),
                None => answer(self.storeless_context(key), None),
            }
        };
        match started() {
            Err(CoreError::StorageFailure) => answer(unavailable_context(Some(key.clone())), None),
            started => started,
        }
    }

    /// SessionStart on an existing row: touch it, after a `compact`'s move.
    fn continue_session(
        &self,
        key: &SessionKey,
        stored: Session,
        source: &str,
        transcript_path: Option<&str>,
        current_dir: Option<&str>,
    ) -> Result<StartOutcome, CoreError> {
        let store = &self.session_store;
        if source == "resume" {
            // a session keeps its row however and wherever it is resumed: the new
            // transcript path is adopted, the session never moves
            let touched = store.touch(key, &now(), transcript_path).map_err(core)?;
            return Ok(StartOutcome {
                context: self.context_of(&touched.unwrap_or(stored))?,
                moved: None,
            });
        }
        let mut moved = None;
        if source == "compact" {
            // before the touch: a /cd then /compact before any prompt must not have
            // its new transcript path absorbed
            match self.apply_move(key, &stored, current_dir, transcript_path)? {
                Ok(outcome) => moved = outcome,
                Err(context) => {
                    return Ok(StartOutcome {
                        context,
                        moved: None,
                    });
                }
            }
        }
        let mut touched = store.touch(key, &now(), None).map_err(core)?;
        // a signal needs a stored path, so a NULL one means none was seen
        if touched.is_some()
            && stored.transcript_path.is_none()
            && let Some(path) = usable(transcript_path)
        {
            touched = store.adopt_transcript(key, path).map_err(core)?.or(touched);
        }
        Ok(StartOutcome {
            context: self.context_of(&touched.unwrap_or(stored))?,
            moved,
        })
    }

    /// UserPromptSubmit: count and record one prompt. `prompt` is the payload's value,
    /// whatever it is: anything but text is recorded as left out.
    pub fn observe_prompt(
        &self,
        key: &SessionKey,
        prompt: Option<&Value>,
        entry_dir: &str,
        transcript_path: Option<&str>,
        current_dir: Option<&str>,
    ) -> Result<PromptOutcome, CoreError> {
        let answer = |context, created_pending, moved| {
            Ok(PromptOutcome {
                context,
                created_pending,
                moved,
            })
        };
        // first: without a store neither the policy nor git runs
        if !self.store_exists() {
            return answer(self.storeless_context(key), false, None);
        }
        let entry = self.prompt_entry(prompt, &now());
        let observed = || -> Result<PromptOutcome, CoreError> {
            let store = &self.session_store;
            let mut moved = None;
            let seed = match store.get(key)? {
                None => match self.new_session(key, entry_dir, transcript_path, false)? {
                    Ok(seed) => seed,
                    Err(context) => return answer(context, false, None),
                },
                Some(seed) => {
                    match self.apply_move(key, &seed, current_dir, transcript_path)? {
                        Ok(outcome) => moved = outcome,
                        Err(context) => return answer(context, false, None),
                    }
                    // a signal needs a stored path, so a NULL one means none was seen
                    if seed.transcript_path.is_none()
                        && let Some(path) = usable(transcript_path)
                    {
                        store.adopt_transcript(key, path).map_err(core)?;
                    }
                    seed
                }
            };
            let added = store.add_prompt(key, &entry, &seed, SESSION_RECENT_PROMPTS);
            match added.map_err(core)? {
                Some((session, created)) => answer(self.context_of(&session)?, created, moved),
                None => answer(self.storeless_context(key), false, None),
            }
        };
        match observed() {
            Err(CoreError::StorageFailure) => {
                answer(unavailable_context(Some(key.clone())), false, None)
            }
            observed => observed,
        }
    }

    pub fn end_session(&self, key: &SessionKey) -> Result<(), CoreError> {
        self.session_store.end(key, &now()).map_err(core)
    }

    /// Stop: true when the session is due a save nudge (the nudge is recorded).
    pub fn stop_decision(&self, key: &SessionKey) -> Result<bool, CoreError> {
        let due = self.session_store.nudge_if_due(
            key,
            &now(),
            STOP_NUDGE_MIN_PROMPTS,
            STOP_NUDGE_INTERVAL_PROMPTS,
        );
        match due.map_err(core) {
            Err(CoreError::StorageFailure) => Ok(false),
            due => due,
        }
    }

    /// Claude Code's PreToolUse: remember which session made this call. Best effort: a
    /// store failure only leaves the MCP server on its fallback. An id no call can
    /// have is the caller's error.
    pub fn record_tool_call(
        &self,
        key: &SessionKey,
        call_id: &str,
        cwd: Option<&str>,
    ) -> Result<(), WriteError> {
        let recorded =
            self.session_store
                .record_call(key, call_id, &now(), TOOL_CALL_RETENTION_S, cwd);
        match recorded {
            Err(WriteError::Core(CoreError::StorageFailure)) => Ok(()),
            recorded => recorded,
        }
    }

    /// The recorded tool call (its session and cwd); None when none is known.
    pub fn call_record(
        &self,
        harness: &str,
        call_id: &str,
    ) -> Result<Option<CallRecord>, CoreError> {
        let record =
            self.session_store
                .call_record(harness, call_id, &now(), TOOL_CALL_RETENTION_S);
        match record {
            Err(CoreError::StorageFailure) => Ok(None),
            record => record,
        }
    }

    /// The context a session's stored row grants. Never resolves a directory.
    pub fn session_context(&self, key: Option<&SessionKey>) -> Result<ProjectContext, CoreError> {
        let read = || {
            let stored = match key {
                Some(key) => self.session_store.get(key)?,
                None => None,
            };
            match stored {
                Some(stored) => self.context_of(&stored),
                None => Ok(no_project_context(
                    ProjectContextState::Unidentified,
                    UNIDENTIFIED_HEADER,
                    self.project_store.global_project_id()?,
                    key.cloned(),
                )),
            }
        };
        match read() {
            Err(CoreError::StorageFailure) => Ok(unavailable_context(key.cloned())),
            read => read,
        }
    }

    /// The user confirmed a pending session: register it with its candidate (or none).
    pub fn confirm_session(&self, key: &SessionKey) -> Result<ProjectContext, CoreError> {
        let stored = self.session_store.get(key)?;
        let candidate = stored
            .as_ref()
            .filter(|stored| stored.status == SessionStatus::Pending)
            .and_then(|stored| Some((stored.candidate_id.as_ref()?, &stored.candidate_root)));
        // a candidate without a root would confirm to any project without one; the
        // root's integrity is a question for the file system, asked outside the
        // store's transaction
        if let Some((_, root)) = candidate
            && !root.as_deref().is_some_and(root_is_intact)
        {
            return Err(unavailable("candidate-changed"));
        }
        let expected = stored.as_ref();
        let confirmed = self.session_store.confirm(
            key,
            expected.and_then(|stored| stored.candidate_id.as_deref()),
            expected.and_then(|stored| stored.candidate_root.as_deref()),
        )?;
        self.context_of(&confirmed.ok_or_else(|| unavailable("unidentified"))?)
    }

    /// On request: give a session with no project the one covering its directory.
    ///
    /// The stored entry directory first, then the directory the call came from, each
    /// resolved exactly like a first registration; a row that already has a project,
    /// or nothing covering either, answers unchanged. A registration, never a move:
    /// the entry stays as stored.
    pub fn register_session(
        &self,
        key: &SessionKey,
        current_dir: Option<&str>,
    ) -> Result<ProjectContext, CoreError> {
        // first: without a store no directory question (git included) is asked
        if !self.store_exists() {
            return Ok(self.storeless_context(key));
        }
        let Some(stored) = self.session_store.get(key)? else {
            return Err(unavailable("unidentified"));
        };
        if stored.project_id.is_some() {
            return self.context_of(&stored);
        }
        if stored.status == SessionStatus::Pending && stored.candidate_id.is_some() {
            return Err(unavailable("pending")); // session_confirm's to decide
        }
        // a candidate left on a registered row (confirmed before moves) is ignored
        for directory in [Some(stored.entry_cwd.as_str()), usable(current_dir)]
            .into_iter()
            .flatten()
        {
            // outside any transaction: git may run here; a degraded answer is final
            // (fail closed), never a reason to try the next directory
            let entry = match self.resolve_entry(key, directory)? {
                Ok(entry) => entry,
                Err(context) => return Ok(context),
            };
            if let Some(project) = entry.project {
                let assigned = self.session_store.assign_project(
                    key,
                    &project.id,
                    stored.status,
                    &stored.entry_cwd,
                    stored.transcript_path.as_deref(),
                );
                return match assigned.map_err(core)? {
                    Some(assigned) => self.context_of(&assigned),
                    None => Ok(self.storeless_context(key)),
                };
            }
        }
        self.context_of(&stored)
    }

    /// A session's applied moves, oldest first.
    pub fn moves(&self, key: &SessionKey) -> Result<Vec<SessionMove>, CoreError> {
        self.session_store.moves(key)
    }

    /// The registered sessions of the context's project that answer `query`.
    pub fn search_sessions(
        &self,
        query: &str,
        context: &ProjectContext,
        limit: Option<i64>,
    ) -> Result<Vec<Session>, CoreError> {
        let Some(project_id) = &context.read_write_set.project_id else {
            return Ok(Vec::new());
        };
        let limit = match limit {
            None => SESSION_SEARCH_LIMIT_DEFAULT,
            Some(limit) => usize::try_from(limit.max(1)).unwrap_or(usize::MAX),
        };
        self.session_store
            .search(Some(project_id), query, limit.min(SESSION_SEARCH_LIMIT_MAX))
    }

    /// The human read: every project's sessions (or one's), pending ones included.
    /// `ProjectNotFound` for an unknown project id, like the other human reads.
    pub fn list_sessions(
        &self,
        project_id: Option<&str>,
        query: &str,
        limit: Option<usize>,
    ) -> Result<Vec<Session>, CoreError> {
        if let Some(project_id) = project_id {
            self.project_store.read(project_id)?;
        }
        self.session_store
            .search(project_id, query, limit.unwrap_or(usize::MAX))
    }

    /// Sessions bound to a project (the ones a summary is published for), newest first.
    pub fn bound_sessions(&self) -> Result<Vec<Session>, CoreError> {
        self.session_store.bound()
    }

    /// Publish a session's summary.
    ///
    /// The text passes the content policy and the summary length limit first
    /// (`ContentRejected`); the session must still be bound and still have
    /// `expected_last_active_at`, else `SessionMoved`. Nothing is written on any
    /// refusal.
    pub fn publish_summary(
        &self,
        key: &SessionKey,
        text: &str,
        expected_last_active_at: &str,
    ) -> Result<(), WriteError> {
        if let Some(refused) = self.policy.check(text, SESSION_SUMMARY_MAX_CHARS) {
            return Err(CoreError::ContentRejected {
                message: refused.message,
                rule_id: refused.rule_id,
                memory_id: None,
                detail: String::new(),
            }
            .into());
        }
        self.session_store
            .publish_summary(key, text, expected_last_active_at, &now())
    }

    /// The project a pending session would be confirmed to, if it has one.
    pub fn pending_candidate(
        &self,
        context: &ProjectContext,
    ) -> Result<Option<Project>, CoreError> {
        let candidate = self
            .stored_session(context)?
            .filter(|stored| stored.status == SessionStatus::Pending)
            .and_then(|stored| stored.candidate_id);
        let Some(candidate_id) = candidate else {
            return Ok(None);
        };
        match self.project_store.read(&candidate_id) {
            Ok(project) => Ok(Some(project)),
            Err(CoreError::ProjectNotFound(_)) => Ok(None),
            Err(err) => Err(err),
        }
    }

    /// The directory the session was first seen in, for the pending notice.
    pub fn entry_of(&self, context: &ProjectContext) -> Result<Option<String>, CoreError> {
        Ok(self.stored_session(context)?.map(|stored| stored.entry_cwd))
    }

    // --- what a memory write asks ---

    /// `ProjectUnavailable` when the context is a pending session's: nothing is saved
    /// before the user confirms it.
    pub fn refuse_pending(&self, context: &ProjectContext) -> Result<(), CoreError> {
        if context.state != ProjectContextState::Pending {
            return Ok(());
        }
        // no candidate: there is nothing to confirm, only a project to init
        let without_candidate = self
            .stored_session(context)?
            .is_some_and(|stored| stored.candidate_id.is_none());
        Err(unavailable(if without_candidate {
            "pending-no-candidate"
        } else {
            "pending"
        }))
    }

    /// The save watermark: best effort, never fails the save.
    pub fn mark_saved(&self, context: &ProjectContext) {
        if let Some(key) = &context.session_key {
            let _ = self.session_store.mark_saved(key);
        }
    }

    fn stored_session(&self, context: &ProjectContext) -> Result<Option<Session>, CoreError> {
        match &context.session_key {
            Some(key) => self.session_store.get(key),
            None => Ok(None),
        }
    }

    /// What a stored row grants: its project, none, or pending (read global only).
    fn context_of(&self, session: &Session) -> Result<ProjectContext, CoreError> {
        let global_project_id = self.project_store.global_project_id()?;
        let key = Some(session.key.clone());
        let without = |state, header| {
            Ok(no_project_context(
                state,
                header,
                global_project_id.clone(),
                key.clone(),
            ))
        };
        if session.status == SessionStatus::Pending {
            return without(
                ProjectContextState::Pending,
                match session.candidate_id {
                    Some(_) => PENDING_HEADER,
                    None => PENDING_NO_CANDIDATE_HEADER,
                },
            );
        }
        let Some(project_id) = &session.project_id else {
            return without(ProjectContextState::None, SESSION_NONE_HEADER);
        };
        let project = match self.project_store.read(project_id) {
            Ok(project) => Some(project),
            Err(CoreError::ProjectNotFound(_)) => None,
            Err(err) => return Err(err),
        };
        match project.filter(|project| Some(&project.id) != global_project_id.as_ref()) {
            Some(project) => Ok(registered_context(
                project,
                global_project_id,
                HEADER_FIELD_CHARS,
                key,
            )),
            None => Ok(ProjectContext {
                state: ProjectContextState::Degraded,
                header: SESSION_PROJECT_GONE_HEADER.to_string(),
                read_write_set: ReadWriteSet {
                    project_id: None,
                    global_project_id,
                },
                project: None,
                diagnostic: Some(SESSION_PROJECT_MISSING.to_string()),
                session_key: key,
            }),
        }
    }

    // no store: nothing was written, and no project exists to grant
    fn storeless_context(&self, key: &SessionKey) -> ProjectContext {
        no_project_context(
            ProjectContextState::None,
            NONE_HEADER,
            None,
            Some(key.clone()),
        )
    }

    /// Move a Claude Code session whose transcript changed directory: what the move
    /// did, None when there was none to apply; the context instead when the row is
    /// gone. A signal with an unusable `current_dir` writes nothing, so the next event
    /// with a usable one applies the move.
    fn apply_move(
        &self,
        key: &SessionKey,
        stored: &Session,
        current_dir: Option<&str>,
        transcript_path: Option<&str>,
    ) -> Result<Result<Option<MoveOutcome>, ProjectContext>, CoreError> {
        // a Codex row is never checked, and without a directory to go to there is no
        // move to apply: the transcript's directory is not looked at for either
        let (Harness::ClaudeCode, Some(current_dir)) = (key.harness(), usable(current_dir)) else {
            return Ok(Ok(None));
        };
        let signal = match (&stored.transcript_path, transcript_path) {
            (Some(stored), Some(current)) if transcript_moved(stored, current) => current,
            _ => return Ok(Ok(None)),
        };
        // outside any transaction: git may run here
        let (entry, degraded) = match self.resolve_entry(key, current_dir)? {
            Ok(entry) => (entry, false),
            // moved to "no project" where /cd went: never left on the old one
            Err(_) => (
                Entry {
                    cwd: current_dir.to_string(),
                    project: None,
                    branch: None,
                },
                true,
            ),
        };
        let project = entry.project.as_ref();
        let to = MoveTo {
            expected_transcript_path: stored.transcript_path.as_deref(),
            entry_cwd: &entry.cwd,
            branch: entry.branch.as_deref(),
            transcript_path: signal,
            project_id: project.map(|project| project.id.as_str()),
            project_root: project.and_then(|project| project.root.as_deref()),
            at: &now(),
        };
        Ok(match self.session_store.move_(key, &to).map_err(core)? {
            None => Err(self.storeless_context(key)),
            // the row changed meanwhile: it answers
            Some((_, None)) => Ok(None),
            Some((row, Some(record))) => Ok(Some(MoveOutcome {
                from_project_id: record.from_project_id,
                to_project_id: row.project_id,
                from_candidate_id: stored.candidate_id.clone(),
                to_candidate_id: row.candidate_id,
                to_entry_cwd: row.entry_cwd,
                pending: row.status == SessionStatus::Pending,
                degraded,
            })),
        })
    }

    /// The row a new session gets from its entry directory. A context instead when the
    /// directory cannot be resolved: nothing is registered, and the next event tries
    /// again.
    fn new_session(
        &self,
        key: &SessionKey,
        entry_dir: &str,
        transcript_path: Option<&str>,
        fresh: bool,
    ) -> Result<Result<Session, ProjectContext>, CoreError> {
        let entry = match self.resolve_entry(key, entry_dir)? {
            Ok(entry) => entry,
            Err(context) => return Ok(Err(context)),
        };
        let at = now();
        let (project, candidate) = match (fresh, entry.project) {
            (true, project) => (project, None),
            (false, candidate) => (None, candidate),
        };
        Ok(Ok(Session {
            key: key.clone(),
            status: if fresh {
                SessionStatus::Registered
            } else {
                SessionStatus::Pending
            },
            origin: if fresh {
                SessionOrigin::Start
            } else {
                SessionOrigin::FirstSeen
            },
            project_id: project.map(|project| project.id),
            candidate_id: candidate.as_ref().map(|project| project.id.clone()),
            candidate_root: candidate.and_then(|project| project.root),
            entry_cwd: entry.cwd,
            branch: entry.branch,
            transcript_path: transcript_path.map(str::to_string),
            started_at: at.clone(),
            last_active_at: at,
            ended_at: None,
            prompt_count: 0,
            last_write_prompt_count: 0,
            last_nudge_prompt_count: 0,
            first_prompt: None,
            recent_prompts: Vec::new(),
            summary: None,
            summary_at: None,
        }))
    }

    /// A registration directory's canonical spelling, project and branch; the degraded
    /// context instead when it cannot be resolved.
    fn resolve_entry(
        &self,
        key: &SessionKey,
        entry_dir: &str,
    ) -> Result<Result<Entry, ProjectContext>, CoreError> {
        let degraded = |diagnostic: &str| -> Result<Result<Entry, ProjectContext>, CoreError> {
            Ok(Err(degraded_context(
                diagnostic,
                self.project_store.global_project_id()?,
                HEADER_FIELD_CHARS,
                Some(key.clone()),
            )))
        };
        // only the canonical spelling is registered and walked: an alias (a symlink)
        // belongs where it points, never where it is written
        let Some(entry) = canonical(Path::new(entry_dir)) else {
            return degraded(UNRESOLVABLE_START);
        };
        // git is asked once, for the place and the branch together
        let Some(facts) = git_facts(&entry, &self.env) else {
            return degraded(WORKTREE_UNMAPPED);
        };
        let resolution = self
            .project_store
            .resolve(&entry, None, Some(&facts.main_tree))?;
        if resolution.state == ResolutionState::Degraded {
            return degraded(resolution.diagnostic.as_deref().unwrap_or_default());
        }
        // a row holds text: a directory whose real path is none cannot be registered
        let cwd = entry
            .into_os_string()
            .into_string()
            .map_err(|_| CoreError::StorageFailure)?;
        Ok(Ok(Entry {
            cwd,
            project: resolution.project,
            branch: facts.branch,
        }))
    }

    /// One prompt as it may be stored: its first line-folded characters, or why not.
    /// Built before any transaction; the text never reaches an error or a log.
    fn prompt_entry(&self, prompt: Option<&Value>, at: &str) -> PromptEntry {
        let omitted = |why| PromptEntry::omitted(at, why).expect("the time is the clock's");
        let Some(Value::String(prompt)) = prompt else {
            return omitted(PromptOmission::Invalid);
        };
        let line = single_line(prompt);
        if line.is_empty() {
            return omitted(PromptOmission::Invalid);
        }
        if prompt.len() > SESSION_PROMPT_SCAN_MAX_BYTES {
            return omitted(PromptOmission::TooLarge);
        }
        // what is stored is the line, cut: a credential the policy sees only there (a
        // control character where the line has a blank) must not be kept either, so
        // both the prompt and what would be written are checked
        let preview = cut(&line, SESSION_PROMPT_CHARS);
        // any fault of the policy omits the text, never fails the hook
        let checked = catch_unwind(AssertUnwindSafe(|| {
            let refused = |text: &str| self.policy.check(text, text.chars().count()).is_some();
            refused(prompt) || refused(preview)
        }));
        match checked {
            Err(_) => omitted(PromptOmission::ScanError),
            Ok(true) => omitted(PromptOmission::Secret),
            Ok(false) => PromptEntry::text(at, preview).expect("the time is the clock's"),
        }
    }
}

#[cfg(test)]
mod tests;
