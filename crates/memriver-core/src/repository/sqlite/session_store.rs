//! The session store: one row per harness session, every change one short transaction.
//!
//! Nothing here creates a store: a write to a store that is not there is a no-op, and
//! so is one whose store went away between the check and the connection.

use std::path::{Path, PathBuf};

use rusqlite::types::Value;
use rusqlite::{Connection, OptionalExtension, params, params_from_iter};

use super::database::{Database, text, values};
use crate::models::errors::{CoreError, ValueError, WriteError};
use crate::models::helpers::{
    format_micros, is_id, is_timestamp, now_strictly_after, parse_timestamp, py_strip,
};
use crate::models::search::{Rank, rank_key, search_terms};
use crate::models::session::{
    CallRecord, Harness, PromptBody, PromptEntry, Session, SessionKey, SessionMove, SessionOrigin,
    SessionStatus, is_call_id, prompt_from_json, prompt_to_json, prompts_from_json,
    prompts_to_json,
};
use crate::repository::directories::usable_directory;
use crate::settings::BUSY_TIMEOUT_MS;

/// The column order of every session SELECT and INSERT, which the row codec follows.
pub const SESSION_COLUMNS: &str = "harness, session_id, status, origin, project_id, candidate_id, \
     candidate_root, entry_cwd, branch, transcript_path, started_at, last_active_at, ended_at, \
     prompt_count, last_write_prompt_count, last_nudge_prompt_count, first_prompt, \
     recent_prompts, summary, summary_at";
pub const SESSION_MOVE_COLUMNS: &str = "harness, session_id, seq, moved_at, from_entry_cwd, \
                                        from_project_id, to_entry_cwd, to_project_id";
const BY_KEY: &str = " WHERE harness = ?1 AND session_id = ?2";

type Written<T> = Result<T, WriteError>;

// --- rows ---

pub fn session_to_row(session: &Session) -> Vec<Value> {
    let text = |text: &str| Value::Text(text.to_string());
    let optional = |text: &Option<String>| text.clone().map_or(Value::Null, Value::Text);
    vec![
        text(session.key.harness().as_str()),
        text(session.key.session_id()),
        text(session.status.as_str()),
        text(session.origin.as_str()),
        optional(&session.project_id),
        optional(&session.candidate_id),
        optional(&session.candidate_root),
        text(&session.entry_cwd),
        optional(&session.branch),
        optional(&session.transcript_path),
        text(&session.started_at),
        text(&session.last_active_at),
        optional(&session.ended_at),
        Value::Integer(session.prompt_count),
        Value::Integer(session.last_write_prompt_count),
        Value::Integer(session.last_nudge_prompt_count),
        optional(&session.first_prompt.as_ref().map(prompt_to_json)),
        text(&prompts_to_json(&session.recent_prompts)),
        optional(&session.summary),
        optional(&session.summary_at),
    ]
}

/// A validated session; an error for a row memriver could not have written.
pub fn session_from_row(row: &[Value]) -> Result<Session, ValueError> {
    if row.len() != 20 {
        return Err(ValueError::new("a session row has twenty columns"));
    }
    let not_text = || ValueError::new("a text column holds something else");
    let need = |idx| text(row, idx)?.ok_or_else(not_text);
    let owned = |idx| Ok::<_, ValueError>(text(row, idx)?.map(str::to_string));
    let harness = Harness::parse(need(0)?).ok_or_else(|| ValueError::new("unknown harness"))?;
    let key = SessionKey::new(harness, need(1)?)?;
    let (Some(status), Some(origin)) = (
        SessionStatus::parse(need(2)?),
        SessionOrigin::parse(need(3)?),
    ) else {
        return Err(ValueError::new("unknown status or origin"));
    };
    let project = |idx| match text(row, idx) {
        Ok(None) => Ok(None),
        Ok(Some(id)) if is_id(id) => Ok(Some(id.to_string())),
        _ => Err(ValueError::new("a project id is not addressable")),
    };
    let (project_id, candidate_id) = (project(4)?, project(5)?);
    if status == SessionStatus::Pending
        && (project_id.is_some() || origin != SessionOrigin::FirstSeen)
    {
        return Err(ValueError::new("a pending row holds a project"));
    }
    let time = |idx| match text(row, idx) {
        Ok(Some(at)) if is_timestamp(at) => Ok(at.to_string()),
        _ => Err(ValueError::new("a time column is not a timestamp")),
    };
    let ended_at = match &row[12] {
        Value::Null => None,
        _ => Some(time(12)?),
    };
    let counter = |idx: usize| match row[idx] {
        Value::Integer(counter) if counter >= 0 => Ok(counter),
        _ => Err(ValueError::new("a counter is not a non-negative integer")),
    };
    let (prompt_count, last_write, last_nudge) = (counter(13)?, counter(14)?, counter(15)?);
    if last_write > prompt_count || last_nudge > prompt_count {
        return Err(ValueError::new("a watermark is above the prompt count"));
    }
    let first_prompt = match &row[16] {
        Value::Null => None,
        _ => Some(prompt_from_json(need(16)?)?),
    };
    let (summary, summary_at) = (owned(18)?, owned(19)?);
    if summary.is_none() != summary_at.is_none() {
        return Err(ValueError::new(
            "a summary is stored without its time, or a time without it",
        ));
    }
    if let (Some(summary), Some(at)) = (&summary, &summary_at)
        && (py_strip(summary).is_empty() || !is_timestamp(at))
    {
        return Err(ValueError::new(
            "the summary is not text with its timestamp",
        ));
    }
    Ok(Session {
        key,
        status,
        origin,
        project_id,
        candidate_id,
        candidate_root: owned(6)?,
        entry_cwd: need(7)?.to_string(),
        branch: owned(8)?,
        transcript_path: owned(9)?,
        started_at: time(10)?,
        last_active_at: time(11)?,
        ended_at,
        prompt_count,
        last_write_prompt_count: last_write,
        last_nudge_prompt_count: last_nudge,
        first_prompt,
        recent_prompts: prompts_from_json(need(17)?)?,
        summary,
        summary_at,
    })
}

/// A validated move; an error for a row memriver could not have written.
pub fn session_move_from_row(row: &[Value]) -> Result<SessionMove, ValueError> {
    let not_text = || ValueError::new("a move directory is not text");
    let need = |idx| text(row, idx).ok().flatten().ok_or_else(not_text);
    let project = |idx| match text(row, idx) {
        Ok(id) => Ok(id.map(str::to_string)),
        Err(_) => Err(ValueError::new("a move project id is not addressable")),
    };
    let harness = Harness::parse(need(0)?).ok_or_else(|| ValueError::new("unknown harness"))?;
    let Some(Value::Integer(seq)) = row.get(2) else {
        return Err(ValueError::new("move sequence is not a positive integer"));
    };
    let moved_at = text(row, 3).ok().flatten().unwrap_or_default();
    SessionMove::new(
        SessionKey::new(harness, need(1)?)?,
        *seq,
        moved_at,
        need(4)?,
        project(5)?,
        need(6)?,
        project(7)?,
    )
}

/// The row for `session`; a row the read path would reject is never written.
fn checked_row(session: &Session) -> Result<Vec<Value>, ValueError> {
    let row = session_to_row(session);
    session_from_row(&row)?;
    Ok(row)
}

fn stored(conn: &Connection, key: &SessionKey) -> Result<Option<Session>, CoreError> {
    let sql = format!("SELECT {SESSION_COLUMNS} FROM sessions{BY_KEY}");
    let row = conn
        .query_row(
            &sql,
            params![key.harness().as_str(), key.session_id()],
            values,
        )
        .optional()?;
    // damage: the session gets no project access
    row.map(|row| session_from_row(&row).map_err(|_| CoreError::StorageFailure))
        .transpose()
}

fn insert(conn: &Connection, row: &[Value]) -> Result<bool, CoreError> {
    let places = (1..=row.len())
        .map(|place| format!("?{place}"))
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!(
        "INSERT INTO sessions ({SESSION_COLUMNS}) VALUES ({places}) \
         ON CONFLICT(harness, session_id) DO NOTHING"
    );
    Ok(conn.execute(&sql, params_from_iter(row))? == 1)
}

/// Every column but the key, rewritten from the validated row.
fn save(conn: &Connection, session: Session) -> Written<Session> {
    let row = checked_row(&session)?;
    let sets = SESSION_COLUMNS
        .split(", ")
        .skip(2)
        .enumerate()
        .map(|(at, column)| format!("{} = ?{}", column.trim(), at + 3))
        .collect::<Vec<_>>()
        .join(", ");
    conn.execute(
        &format!("UPDATE sessions SET {sets}{BY_KEY}"),
        params_from_iter(&row),
    )?;
    Ok(session)
}

// checked up front: taking the later of it and the stored value would hide a bad one
fn require_timestamp(at: &str) -> Result<(), ValueError> {
    if is_timestamp(at) {
        Ok(())
    } else {
        Err(ValueError::new("at is not a timestamp"))
    }
}

/// A session's activity time after an event at `at`: always later than the stored one.
/// A clock that stood still or went back must not hide the event from whoever read the
/// session before it -- a summary made from that reading does not cover it.
fn later(stored: &str, at: &str) -> String {
    if at > stored {
        at.to_string()
    } else {
        now_strictly_after(stored)
    }
}

/// The time before which a tool call is too old at `at`, in the same fixed-width form:
/// the comparison of the texts is chronological.
fn expired(at: &str, retention_s: i64) -> Option<String> {
    let kept = retention_s.checked_mul(1_000_000)?;
    format_micros(parse_timestamp(at)?.checked_sub(kept)?)
}

/// What a session search reads: saved prompt texts, entry cwd, branch, summary.
fn texts(session: &Session) -> Vec<&str> {
    let mut texts: Vec<&str> = session
        .first_prompt
        .iter()
        .chain(&session.recent_prompts)
        .filter_map(|entry| match entry.body() {
            PromptBody::Text(text) => Some(text.as_str()),
            PromptBody::Omitted(_) => None,
        })
        .collect();
    texts.extend([
        session.entry_cwd.as_str(),
        session.branch.as_deref().unwrap_or_default(),
        session.summary.as_deref().unwrap_or_default(),
    ]);
    texts
}

/// Where a moved session now is, and the transcript path the move expects to replace.
pub struct MoveTo<'a> {
    pub expected_transcript_path: Option<&'a str>,
    pub entry_cwd: &'a str,
    pub branch: Option<&'a str>,
    pub transcript_path: &'a str,
    pub project_id: Option<&'a str>,
    pub project_root: Option<&'a str>,
    pub at: &'a str,
}

#[cfg(test)]
thread_local! {
    static MEANWHILE: std::cell::RefCell<Option<Box<dyn FnOnce()>>> =
        const { std::cell::RefCell::new(None) };
}

/// For a test, on its thread, once: `then` runs when the next write has found its store
/// and before its transaction opens -- where another process's write can land.
#[cfg(test)]
pub(crate) fn meanwhile(then: impl FnOnce() + 'static) {
    MEANWHILE.set(Some(Box::new(then)));
}

pub struct SessionStore {
    root: PathBuf,
    busy_timeout_ms: u64,
}

impl SessionStore {
    pub fn new(root: &Path) -> SessionStore {
        SessionStore::with_busy_timeout(root, BUSY_TIMEOUT_MS)
    }

    /// For a test that must not wait the whole `BUSY_TIMEOUT_MS` for a lock.
    pub fn with_busy_timeout(root: &Path, busy_timeout_ms: u64) -> SessionStore {
        SessionStore {
            root: root.to_path_buf(),
            busy_timeout_ms,
        }
    }

    fn database(&self) -> Database {
        Database::with_busy_timeout(&self.root, self.busy_timeout_ms)
    }

    /// `body` in one write transaction; None when the store is absent, one removed
    /// between the check and the connection included. Any other failure comes back.
    fn written<T>(&self, body: impl FnOnce(&Connection) -> Written<T>) -> Written<Option<T>> {
        let database = self.database();
        if !database.exists()? {
            return Ok(None);
        }
        #[cfg(test)]
        if let Some(then) = MEANWHILE.take() {
            then();
        }
        // the database hands back its own kind of error only: a caller's error is
        // carried past it, and the transaction is rolled back for either
        let mut callers = None;
        let done = database.write(false, |conn| {
            body(conn).map_err(|err| match err {
                WriteError::Core(err) => err,
                WriteError::Value(err) => {
                    callers = Some(err);
                    CoreError::StorageFailure
                }
            })
        });
        match (callers, done) {
            (Some(err), _) => Err(err.into()),
            (None, Ok(value)) => Ok(Some(value)),
            (None, Err(CoreError::StorageFailure)) if !database.exists()? => Ok(None),
            (None, Err(err)) => Err(err.into()),
        }
    }

    fn rows<T>(
        &self,
        sql: &str,
        key: Option<&str>,
        mut each: impl FnMut(Vec<Value>) -> Option<T>,
        limit: usize,
    ) -> Result<Vec<T>, CoreError> {
        self.database().read(|conn| {
            let Some(conn) = conn else {
                return Ok(Vec::new());
            };
            let mut statement = conn.prepare(sql)?;
            let mut rows = statement.query(params_from_iter(key))?;
            let mut found = Vec::new();
            while found.len() < limit
                && let Some(row) = rows.next()?
            {
                found.extend(each(values(row)?));
            }
            Ok(found)
        })
    }

    pub fn store_exists(&self) -> Result<bool, CoreError> {
        self.database().exists()
    }

    pub fn get(&self, key: &SessionKey) -> Result<Option<Session>, CoreError> {
        self.database().read(|conn| match conn {
            Some(conn) => stored(conn, key),
            None => Ok(None),
        })
    }

    /// Stores `session` unless its key has a row; the stored row either way.
    pub fn register(&self, session: &Session) -> Written<Option<Session>> {
        let row = checked_row(session)?;
        let registered = self.written(|conn| {
            insert(conn, &row)?;
            Ok(stored(conn, &session.key)?)
        })?;
        Ok(registered.flatten())
    }

    /// Moves `last_active_at` forward, always; a transcript path replaces the stored
    /// one only when one is given.
    pub fn touch(
        &self,
        key: &SessionKey,
        at: &str,
        transcript_path: Option<&str>,
    ) -> Written<Option<Session>> {
        require_timestamp(at)?;
        self.change(key, |mut session| {
            session.last_active_at = later(&session.last_active_at, at);
            if let Some(path) = transcript_path {
                session.transcript_path = Some(path.to_string());
            }
            Some(session)
        })
    }

    /// One read-modify-write of a row: `change` gives the new row, or None to leave it.
    /// The row as it stands afterwards; None for an unknown key or an absent store.
    fn change(
        &self,
        key: &SessionKey,
        change: impl FnOnce(Session) -> Option<Session>,
    ) -> Written<Option<Session>> {
        let changed = self.written(|conn| {
            let Some(session) = stored(conn, key)? else {
                return Ok(None);
            };
            match change(session.clone()) {
                Some(changed) => save(conn, changed).map(Some),
                None => Ok(Some(session)),
            }
        })?;
        Ok(changed.flatten())
    }

    /// Counts one prompt, inserting `seed` first when the key has no row. The row, and
    /// whether this call created it: of several first prompts at once exactly one did.
    pub fn add_prompt(
        &self,
        key: &SessionKey,
        entry: &PromptEntry,
        seed: &Session,
        keep_recent: usize,
    ) -> Written<Option<(Session, bool)>> {
        if seed.key != *key {
            return Err(ValueError::new("the seed is another session's").into());
        }
        if keep_recent < 1 {
            return Err(ValueError::new("keep_recent is not a positive integer").into());
        }
        let seed_row = checked_row(seed)?;
        self.written(|conn| {
            let created = insert(conn, &seed_row)?;
            let mut session = stored(conn, key)?.ok_or(CoreError::StorageFailure)?;
            // a count with no next one: nothing is written
            session.prompt_count = session
                .prompt_count
                .checked_add(1)
                .ok_or(CoreError::StorageFailure)?;
            session.first_prompt.get_or_insert_with(|| entry.clone());
            session.recent_prompts.push(entry.clone());
            let surplus = session.recent_prompts.len().saturating_sub(keep_recent);
            session.recent_prompts.drain(..surplus);
            session.last_active_at = later(&session.last_active_at, entry.at());
            Ok((save(conn, session)?, created))
        })
    }

    pub fn end(&self, key: &SessionKey, at: &str) -> Written<()> {
        require_timestamp(at)?;
        self.change(key, |mut session| {
            session.ended_at = Some(at.to_string());
            session.last_active_at = later(&session.last_active_at, at);
            Some(session)
        })?;
        Ok(())
    }

    /// Whether the session is due a save nudge, which is then recorded. Every check of
    /// a registered row moves `last_active_at`.
    pub fn nudge_if_due(
        &self,
        key: &SessionKey,
        at: &str,
        min_prompts: i64,
        interval: i64,
    ) -> Written<bool> {
        require_timestamp(at)?;
        let due = self.written(|conn| {
            let Some(mut session) = stored(conn, key)? else {
                return Ok(false);
            };
            if session.status != SessionStatus::Registered {
                return Ok(false);
            }
            let count = session.prompt_count;
            let due = session.project_id.is_some()
                && count - session.last_write_prompt_count >= min_prompts
                && (session.last_nudge_prompt_count == 0
                    || count - session.last_nudge_prompt_count >= interval);
            session.last_active_at = later(&session.last_active_at, at);
            if due {
                session.last_nudge_prompt_count = count;
            }
            save(conn, session)?;
            Ok(due)
        })?;
        Ok(due.unwrap_or(false))
    }

    /// Moves the save watermark to the prompt count.
    pub fn mark_saved(&self, key: &SessionKey) -> Result<(), CoreError> {
        let marked = self.written(|conn| {
            conn.execute(
                &format!("UPDATE sessions SET last_write_prompt_count = prompt_count{BY_KEY}"),
                params![key.harness().as_str(), key.session_id()],
            )?;
            Ok(())
        });
        marked.map(drop).map_err(core)
    }

    /// Registers a pending row with its candidate (or with none), when the candidate
    /// is still the expected one and its project still stands where it stood. A
    /// registered row answers unchanged.
    pub fn confirm(
        &self,
        key: &SessionKey,
        expected_candidate_id: Option<&str>,
        expected_candidate_root: Option<&str>,
    ) -> Result<Option<Session>, CoreError> {
        let refused = |reason: &str| {
            WriteError::Core(CoreError::ProjectUnavailable {
                reason: reason.to_string(),
            })
        };
        let confirmed = self.written(|conn| {
            let Some(mut session) = stored(conn, key)? else {
                return Ok(None);
            };
            if session.status == SessionStatus::Registered {
                return Ok(Some(session));
            }
            let candidate = (
                session.candidate_id.as_deref(),
                session.candidate_root.as_deref(),
            );
            if candidate != (expected_candidate_id, expected_candidate_root) {
                return Err(refused("candidate-moved"));
            }
            if let Some(candidate_id) = &session.candidate_id {
                // the file system's side was checked by the caller; the store's side
                // is decided here, under the write lock
                let project = conn
                    .query_row(
                        "SELECT root, is_global FROM projects WHERE id = ?1",
                        [candidate_id],
                        values,
                    )
                    .optional()?;
                // the values as they are stored: a root that is not text is no root the
                // candidate was seen with, and it is not "no root" either
                let root = session
                    .candidate_root
                    .clone()
                    .map_or(Value::Null, Value::Text);
                let stands = project
                    .is_some_and(|project| project[0] == root && project[1] == Value::Integer(0));
                if !stands {
                    return Err(refused("candidate-changed"));
                }
            }
            session.status = SessionStatus::Registered;
            session.project_id = session.candidate_id.clone();
            save(conn, session).map(Some)
        });
        confirmed.map(Option::flatten).map_err(core)
    }

    /// Gives a row without a project one, unless it changed since the caller read it:
    /// a project, a move or (on a pending row) a candidate that arrived meanwhile wins.
    pub fn assign_project(
        &self,
        key: &SessionKey,
        project_id: &str,
        expected_status: SessionStatus,
        expected_entry_cwd: &str,
        expected_transcript_path: Option<&str>,
    ) -> Written<Option<Session>> {
        self.change(key, |mut session| {
            if session.project_id.is_some()
                || session.status != expected_status
                || (session.status == SessionStatus::Pending && session.candidate_id.is_some())
                || session.entry_cwd != expected_entry_cwd
                || session.transcript_path.as_deref() != expected_transcript_path
            {
                return None;
            }
            session.status = SessionStatus::Registered;
            session.project_id = Some(project_id.to_string());
            Some(session)
        })
    }

    /// Stores a transcript path where none is stored.
    pub fn adopt_transcript(
        &self,
        key: &SessionKey,
        transcript_path: &str,
    ) -> Written<Option<Session>> {
        self.change(key, |mut session| {
            session.transcript_path.is_none().then(|| {
                session.transcript_path = Some(transcript_path.to_string());
                session
            })
        })
    }

    /// Moves a session to another directory and records the move, when its stored
    /// transcript path is still the expected one; otherwise the row as it stands and
    /// no move. None for an unknown key or an absent store.
    pub fn move_(
        &self,
        key: &SessionKey,
        to: &MoveTo<'_>,
    ) -> Written<Option<(Session, Option<SessionMove>)>> {
        require_timestamp(to.at)?;
        let own = |text: Option<&str>| text.map(str::to_string);
        let moved = self.written(|conn| {
            let Some(mut session) = stored(conn, key)? else {
                return Ok(None);
            };
            if session.transcript_path.as_deref() != to.expected_transcript_path {
                return Ok(Some((session, None)));
            }
            let active = later(&session.last_active_at, to.at);
            let (from_entry_cwd, from_project_id) =
                (session.entry_cwd.clone(), session.project_id.clone());
            if session.status == SessionStatus::Registered {
                session.project_id = own(to.project_id);
                (session.candidate_id, session.candidate_root) = (None, None);
            } else {
                session.candidate_id = own(to.project_id);
                session.candidate_root = own(to.project_root);
            }
            session.entry_cwd = to.entry_cwd.to_string();
            session.branch = own(to.branch);
            session.transcript_path = Some(to.transcript_path.to_string());
            session.last_active_at = active.clone();
            let session = save(conn, session)?;
            let by_key = params![key.harness().as_str(), key.session_id()];
            let seq: i64 = conn.query_row(
                &format!("SELECT coalesce(max(seq), 0) + 1 FROM session_moves{BY_KEY}"),
                by_key,
                |row| row.get(0),
            )?;
            let record = SessionMove::new(
                key.clone(),
                seq,
                &active,
                &from_entry_cwd,
                from_project_id,
                to.entry_cwd,
                session.project_id.clone(),
            )?;
            conn.execute(
                &format!(
                    "INSERT INTO session_moves ({SESSION_MOVE_COLUMNS}) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)"
                ),
                params![
                    key.harness().as_str(),
                    key.session_id(),
                    seq,
                    active,
                    record.from_entry_cwd,
                    record.from_project_id,
                    record.to_entry_cwd,
                    record.to_project_id
                ],
            )?;
            Ok(Some((session, Some(record))))
        })?;
        Ok(moved.flatten())
    }

    /// A session's applied moves, oldest first. A bad row is skipped here; doctor
    /// reports it.
    pub fn moves(&self, key: &SessionKey) -> Result<Vec<SessionMove>, CoreError> {
        let sql = format!(
            "SELECT {SESSION_MOVE_COLUMNS} FROM session_moves \
             WHERE harness = '{}' AND session_id = ?1 ORDER BY seq",
            key.harness().as_str()
        );
        self.rows(
            &sql,
            Some(key.session_id()),
            |row| session_move_from_row(&row).ok(),
            usize::MAX,
        )
    }

    /// Remembers which session made a tool call, and where; calls older than
    /// `retention_s` are dropped.
    pub fn record_call(
        &self,
        key: &SessionKey,
        call_id: &str,
        at: &str,
        retention_s: i64,
        cwd: Option<&str>,
    ) -> Written<()> {
        if !is_call_id(call_id) {
            return Err(ValueError::new("invalid call id").into());
        }
        require_timestamp(at)?;
        if retention_s < 1 {
            return Err(ValueError::new("retention_s is not a positive integer").into());
        }
        let cwd = cwd.filter(|cwd| usable_directory(cwd));
        let expired =
            expired(at, retention_s).ok_or_else(|| ValueError::new("date value out of range"))?;
        self.written(|conn| {
            conn.execute(
                "INSERT INTO tool_calls (harness, call_id, session_id, recorded_at, cwd) \
                 VALUES (?1, ?2, ?3, ?4, ?5) ON CONFLICT(harness, call_id) DO UPDATE SET \
                 session_id = excluded.session_id, recorded_at = excluded.recorded_at, \
                 cwd = excluded.cwd",
                params![key.harness().as_str(), call_id, key.session_id(), at, cwd],
            )?;
            conn.execute("DELETE FROM tool_calls WHERE recorded_at < ?1", [&expired])?;
            Ok(())
        })?;
        Ok(())
    }

    /// The tool call recorded within `retention_s` before `at`; None when none is known.
    /// An id no call can have names no session, and a bad row routes nothing.
    pub fn call_record(
        &self,
        harness: &str,
        call_id: &str,
        at: &str,
        retention_s: i64,
    ) -> Result<Option<CallRecord>, CoreError> {
        // a call recorded longer ago than the retention names no session, whether or
        // not a later record has dropped its row yet
        let (true, Some(expired)) = (
            is_call_id(call_id) && retention_s >= 1,
            expired(at, retention_s),
        ) else {
            return Ok(None);
        };
        let row = self.database().read(|conn| match conn {
            Some(conn) => Ok(conn
                .query_row(
                    "SELECT session_id, cwd FROM tool_calls \
                     WHERE harness = ?1 AND call_id = ?2 AND recorded_at >= ?3",
                    [harness, call_id, &expired],
                    values,
                )
                .optional()?),
            None => Ok(None),
        })?;
        Ok(row.and_then(|row| {
            let session_id = text(&row, 0).ok()??;
            Some(CallRecord {
                key: SessionKey::new(Harness::parse(harness)?, session_id).ok()?,
                // a directory that is not text is no directory to go by; the session still is
                cwd: text(&row, 1).ok().flatten().map(str::to_string),
            })
        }))
    }

    /// Sessions answering `query`, best first and then newest first: one project's
    /// registered rows, or with None every row. The empty query lists them all; one
    /// with no keyword in it matches nothing. A bad row is skipped here.
    pub fn search(
        &self,
        project_id: Option<&str>,
        query: &str,
        limit: usize,
    ) -> Result<Vec<Session>, CoreError> {
        let terms = search_terms(query);
        if !query.is_empty() && terms.is_empty() {
            return Ok(Vec::new());
        }
        let scope = match project_id {
            Some(_) => " WHERE project_id = ?1 AND status = 'registered'",
            None => "",
        };
        let sql = format!(
            "SELECT {SESSION_COLUMNS} FROM sessions{scope} \
             ORDER BY last_active_at DESC, harness, session_id"
        );
        let ranked = |row: Vec<Value>| -> Option<(Option<Rank>, Session)> {
            let session = session_from_row(&row).ok()?;
            if terms.is_empty() {
                return Some((None, session));
            }
            let rank = rank_key(&terms, &texts(&session), None);
            (rank.1 > 0).then_some((Some(rank), session))
        };
        // "" lists every session: nothing to rank, so the limit ends the read
        let read_limit = if terms.is_empty() { limit } else { usize::MAX };
        let mut found = self.rows(&sql, project_id, ranked, read_limit)?;
        // stable: equal ranks keep the newest-first order above
        found.sort_by_key(|found| std::cmp::Reverse(found.0));
        found.truncate(limit);
        Ok(found.into_iter().map(|(_, session)| session).collect())
    }

    /// Registered with a project that exists and is not global -- the sessions a
    /// summary belongs to -- newest first.
    pub fn bound(&self) -> Result<Vec<Session>, CoreError> {
        let columns = SESSION_COLUMNS
            .split(", ")
            .map(|column| format!("s.{}", column.trim()))
            .collect::<Vec<_>>()
            .join(", ");
        let sql = format!(
            "SELECT {columns} FROM sessions s JOIN projects p ON p.id = s.project_id \
             WHERE s.status = 'registered' AND p.is_global = 0 \
             ORDER BY s.last_active_at DESC, s.harness, s.session_id"
        );
        self.rows(&sql, None, |row| session_from_row(&row).ok(), usize::MAX)
    }

    /// Stores a session's summary, when the session is bound to a project and has not
    /// been active since the caller read it; `SessionMoved` otherwise, an absent store
    /// included.
    pub fn publish_summary(
        &self,
        key: &SessionKey,
        text: &str,
        expected_last_active_at: &str,
        at: &str,
    ) -> Written<()> {
        require_timestamp(at)?;
        let published = self.written(|conn| {
            let bound = |session: &Session| -> Result<bool, CoreError> {
                let Some(project_id) = &session.project_id else {
                    return Ok(false);
                };
                let found = conn
                    .query_row(
                        "SELECT 1 FROM projects WHERE id = ?1 AND is_global = 0",
                        [project_id],
                        |_| Ok(()),
                    )
                    .optional()?;
                Ok(session.status == SessionStatus::Registered && found.is_some())
            };
            match stored(conn, key)? {
                Some(mut session)
                    if bound(&session)? && session.last_active_at == expected_last_active_at =>
                {
                    session.summary = Some(text.to_string());
                    session.summary_at = Some(at.to_string());
                    save(conn, session).map(drop)
                }
                // activity since the caller read the session means the summary does
                // not cover it
                _ => Err(CoreError::SessionMoved.into()),
            }
        })?;
        published.ok_or(CoreError::SessionMoved.into())
    }
}

/// A write that was handed nothing a caller could have got wrong.
pub(crate) fn core(err: WriteError) -> CoreError {
    match err {
        WriteError::Core(err) => err,
        WriteError::Value(_) => CoreError::StorageFailure,
    }
}
