//! Harness sessions as stored: one row per session, its prompts, its moves.

use serde_json::Value;

use super::errors::{ValueError, named};
use super::helpers::{is_id, is_timestamp, py_isprintable, py_isspace};

named! {
    /// Only the harnesses that hand memriver a session id of their own.
    Harness {
        ClaudeCode = "claude-code",
        Codex = "codex",
    }
}

named! {
    SessionStatus {
        Registered = "registered",
        Pending = "pending",
    }
}

named! {
    SessionOrigin {
        Start = "start",
        FirstSeen = "first-seen",
    }
}

named! {
    /// Why a prompt's text was left out.
    PromptOmission {
        Secret = "secret",
        TooLarge = "too-large",
        Invalid = "invalid",
        ScanError = "scan-error",
    }
}

const SESSION_ID_MAX_CHARS: usize = 128;
const CALL_ID_MAX_CHARS: usize = 256;

/// A harness-owned id: 1..max_chars characters, each printable and not whitespace. A
/// format character (a bidi override, a zero-width space) is invisible where the id
/// is shown.
fn is_harness_string(value: &str, max_chars: usize) -> bool {
    (1..=max_chars).contains(&value.chars().count())
        && value.chars().all(|c| py_isprintable(c) && !py_isspace(c))
}

/// Whether `value` can be a harness's tool-call id.
pub fn is_call_id(value: &str) -> bool {
    is_harness_string(value, CALL_ID_MAX_CHARS)
}

/// One harness session. The id is the harness's own string, never an id of memriver's.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SessionKey {
    harness: Harness,
    session_id: String,
}

impl SessionKey {
    pub fn new(harness: Harness, session_id: &str) -> Result<SessionKey, ValueError> {
        if !is_harness_string(session_id, SESSION_ID_MAX_CHARS) {
            return Err(ValueError::new("invalid session id"));
        }
        Ok(SessionKey {
            harness,
            session_id: session_id.to_string(),
        })
    }

    pub fn harness(&self) -> Harness {
        self.harness
    }

    pub fn session_id(&self) -> &str {
        &self.session_id
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PromptBody {
    Text(String),
    Omitted(PromptOmission),
}

/// One prompt as recorded: its text, or why the text was left out. Never both.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromptEntry {
    at: String,
    body: PromptBody,
}

impl PromptEntry {
    // messages name the rule only: a prompt's text never reaches an error
    fn new(at: &str, body: PromptBody) -> Result<PromptEntry, ValueError> {
        if !is_timestamp(at) {
            return Err(ValueError::new("prompt entry time is not a timestamp"));
        }
        Ok(PromptEntry {
            at: at.to_string(),
            body,
        })
    }

    pub fn text(at: &str, text: &str) -> Result<PromptEntry, ValueError> {
        Self::new(at, PromptBody::Text(text.to_string()))
    }

    pub fn omitted(at: &str, omitted: PromptOmission) -> Result<PromptEntry, ValueError> {
        Self::new(at, PromptBody::Omitted(omitted))
    }

    pub fn at(&self) -> &str {
        &self.at
    }

    pub fn body(&self) -> &PromptBody {
        &self.body
    }
}

// The stored form of prompts is one exact text per value: compact separators,
// characters outside ASCII written as they are, `at` first. A column that does not
// re-encode to itself is a hand-edit, and a hand-edit is damage.

/// The stored form of one prompt (`sessions.first_prompt`).
pub fn prompt_to_json(entry: &PromptEntry) -> String {
    let (name, value) = match &entry.body {
        PromptBody::Text(text) => ("text", text.as_str()),
        PromptBody::Omitted(omitted) => ("omitted", omitted.as_str()),
    };
    format!(
        "{{\"at\":{},\"{name}\":{}}}",
        Value::from(entry.at.as_str()),
        Value::from(value)
    )
}

/// The stored form of a session's recent prompts, newest last.
pub fn prompts_to_json(entries: &[PromptEntry]) -> String {
    format!(
        "[{}]",
        entries
            .iter()
            .map(prompt_to_json)
            .collect::<Vec<_>>()
            .join(",")
    )
}

fn written_value(raw: &str) -> Result<Value, ValueError> {
    let not_written = || ValueError::new("a JSON column is not in its written form");
    let value: Value = serde_json::from_str(raw).map_err(|_| not_written())?;
    if value.to_string().as_str() != raw {
        return Err(not_written());
    }
    Ok(value)
}

fn entry_from_value(value: &Value) -> Result<PromptEntry, ValueError> {
    let fields = value.as_object().filter(|fields| fields.len() == 2);
    let found = fields.and_then(|fields| {
        let body = match (fields.get("text"), fields.get("omitted")) {
            (Some(text), None) => ("text", text),
            (None, Some(omitted)) => ("omitted", omitted),
            _ => return None,
        };
        Some((fields.get("at")?, body))
    });
    let Some((at, (name, body))) = found else {
        return Err(ValueError::new(
            "a prompt entry is not {at, text} or {at, omitted}",
        ));
    };
    let not_a_time = || ValueError::new("prompt entry time is not a timestamp");
    let at = at
        .as_str()
        .filter(|at| is_timestamp(at))
        .ok_or_else(not_a_time)?;
    match (name, body) {
        (_, Value::Null) => Err(ValueError::new(
            "a prompt entry holds exactly one of text and omitted",
        )),
        ("text", Value::String(text)) => PromptEntry::text(at, text),
        ("text", _) => Err(ValueError::new("prompt entry text is not text")),
        (_, omitted) => {
            let known = omitted.as_str().and_then(PromptOmission::parse);
            let known = known.ok_or_else(|| ValueError::new("unknown prompt omission"))?;
            PromptEntry::omitted(at, known)
        }
    }
}

/// A stored prompt; an error for anything memriver could not have written.
pub fn prompt_from_json(raw: &str) -> Result<PromptEntry, ValueError> {
    entry_from_value(&written_value(raw)?)
}

/// A stored list of prompts; an error for anything memriver could not have written.
pub fn prompts_from_json(raw: &str) -> Result<Vec<PromptEntry>, ValueError> {
    match written_value(raw)? {
        Value::Array(entries) => entries.iter().map(entry_from_value).collect(),
        _ => Err(ValueError::new("recent_prompts is not a list")),
    }
}

/// One stored `sessions` row; the stored row is the answer, never a caller's guess.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Session {
    pub key: SessionKey,
    pub status: SessionStatus,
    pub origin: SessionOrigin,
    /// Registered: the project, or None (entry unbound).
    pub project_id: Option<String>,
    /// Pending: the project to confirm, or None.
    pub candidate_id: Option<String>,
    /// Pending: the candidate's root when it was computed.
    pub candidate_root: Option<String>,
    pub entry_cwd: String,
    pub branch: Option<String>,
    pub transcript_path: Option<String>,
    /// First recorded by memriver, not the harness's start.
    pub started_at: String,
    pub last_active_at: String,
    pub ended_at: Option<String>,
    pub prompt_count: i64,
    pub last_write_prompt_count: i64,
    pub last_nudge_prompt_count: i64,
    pub first_prompt: Option<PromptEntry>,
    /// Newest last.
    pub recent_prompts: Vec<PromptEntry>,
    /// The published summary: set together with `summary_at`, or neither.
    pub summary: Option<String>,
    pub summary_at: Option<String>,
}

/// A recorded tool call: its session and the working directory its payload named.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallRecord {
    pub key: SessionKey,
    pub cwd: Option<String>,
}

/// One applied move of a session to another directory (append-only, `seq` per
/// session). Built with `new`, which refuses what a row could not hold.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionMove {
    pub key: SessionKey,
    pub seq: i64,
    pub moved_at: String,
    pub from_entry_cwd: String,
    pub from_project_id: Option<String>,
    pub to_entry_cwd: String,
    pub to_project_id: Option<String>,
}

impl SessionMove {
    pub fn new(
        key: SessionKey,
        seq: i64,
        moved_at: &str,
        from_entry_cwd: &str,
        from_project_id: Option<String>,
        to_entry_cwd: &str,
        to_project_id: Option<String>,
    ) -> Result<SessionMove, ValueError> {
        if seq < 1 {
            return Err(ValueError::new("move sequence is not a positive integer"));
        }
        if !is_timestamp(moved_at) {
            return Err(ValueError::new("move time is not a timestamp"));
        }
        if [&from_project_id, &to_project_id]
            .into_iter()
            .flatten()
            .any(|id| !is_id(id))
        {
            return Err(ValueError::new("a move project id is not addressable"));
        }
        Ok(SessionMove {
            key,
            seq,
            moved_at: moved_at.to_string(),
            from_entry_cwd: from_entry_cwd.to_string(),
            from_project_id,
            to_entry_cwd: to_entry_cwd.to_string(),
            to_project_id,
        })
    }
}

/// What a move did, for the caller to answer from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MoveOutcome {
    pub from_project_id: Option<String>,
    pub to_project_id: Option<String>,
    pub from_candidate_id: Option<String>,
    pub to_candidate_id: Option<String>,
    pub to_entry_cwd: String,
    pub pending: bool,
    pub degraded: bool,
}

impl MoveOutcome {
    /// A pending row compares candidates, a registered one projects.
    pub fn changed(&self) -> bool {
        if self.degraded {
            return true;
        }
        if self.pending {
            return self.to_candidate_id != self.from_candidate_id;
        }
        self.to_project_id != self.from_project_id
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const AT: &str = "2026-11-22T02:16:43.300060Z";

    fn key() -> SessionKey {
        SessionKey::new(Harness::ClaudeCode, "s1").unwrap()
    }

    #[test]
    fn a_session_key_accepts_the_two_harnesses_that_supply_ids() {
        for (name, harness) in [
            ("claude-code", Harness::ClaudeCode),
            ("codex", Harness::Codex),
        ] {
            assert_eq!(Harness::parse(name), Some(harness));
            assert_eq!(harness.as_str(), name);
            assert_eq!(
                SessionKey::new(harness, "abc-123").unwrap().harness(),
                harness
            );
        }
        for other in ["cursor", "kiro", "Codex", ""] {
            assert_eq!(Harness::parse(other), None, "{other:?}");
        }
    }

    #[test]
    fn a_session_key_accepts_printable_ids_up_to_128_characters() {
        let longest = "x".repeat(128);
        // 128 characters of four bytes each: the limit counts characters
        let wide = "\u{1f600}".repeat(128);
        for session_id in [
            "a",
            &longest,
            "0199a2b4-7c1d-7e2f-9a3b-4c5d6e7f8091",
            "\u{4f1a}\u{8bdd}",
            &wide,
        ] {
            assert_eq!(
                SessionKey::new(Harness::Codex, session_id)
                    .unwrap()
                    .session_id(),
                session_id
            );
        }
    }

    #[test]
    fn key_refuses_space_zero_width_and_129_chars() {
        for session_id in [
            "",
            &"x".repeat(129),
            &"\u{4f1a}".repeat(129),
            "a b",
            "a\tb",
            "a\n",
            "a\u{202e}",
            "a\u{200b}",
            "a\u{3000}",
            "a\u{a0}",
            "a\u{e000}",
            "a\u{378}",
        ] {
            assert_eq!(
                SessionKey::new(Harness::ClaudeCode, session_id)
                    .unwrap_err()
                    .to_string(),
                "invalid session id",
                "{session_id:?}"
            );
        }
    }

    #[test]
    fn a_call_id_is_printable_up_to_256_characters() {
        for call_id in ["a", &"x".repeat(256), "toolu_01ABCdef", "\u{8c03}\u{7528}"] {
            assert!(is_call_id(call_id), "{call_id:?}");
        }
        for call_id in ["", &"x".repeat(257), "a b", "a\n", "a\u{202e}"] {
            assert!(!is_call_id(call_id), "{call_id:?}");
        }
    }

    #[test]
    fn a_prompt_entry_holds_text_or_an_omission() {
        let entry = PromptEntry::text(AT, "fix the build").unwrap();
        assert_eq!(
            (entry.at(), entry.body()),
            (AT, &PromptBody::Text("fix the build".to_string()))
        );
        let entry = PromptEntry::omitted(AT, PromptOmission::Secret).unwrap();
        assert_eq!(entry.body(), &PromptBody::Omitted(PromptOmission::Secret));
        for name in ["secret", "too-large", "invalid", "scan-error"] {
            assert_eq!(PromptOmission::parse(name).unwrap().as_str(), name);
        }
        assert_eq!(PromptOmission::parse("because"), None);
    }

    #[test]
    fn a_prompt_entry_needs_a_timestamp() {
        for at in ["", "yesterday", "2026-02-30T00:00:00.000000Z"] {
            assert_eq!(
                PromptEntry::text(at, "my secret prompt")
                    .unwrap_err()
                    .to_string(),
                "prompt entry time is not a timestamp"
            );
            assert!(PromptEntry::omitted(at, PromptOmission::Invalid).is_err());
        }
    }

    #[test]
    fn prompts_json_matches_python_bytes() {
        let text = PromptEntry::text(
            AT,
            "\u{4fee}\u{590d} \"\u{6784}\u{5efa}\"\\ \u{1f600}\n\t\u{1}\u{7f}\u{2028}\u{e9}",
        )
        .unwrap();
        let omitted = PromptEntry::omitted(AT, PromptOmission::TooLarge).unwrap();
        // what the Python implementation writes for these two entries, as bytes
        let first: &[u8] = b"{\"at\":\"2026-11-22T02:16:43.300060Z\",\"text\":\"\xe4\xbf\xae\xe5\xa4\x8d \\\"\xe6\x9e\x84\xe5\xbb\xba\\\"\\\\ \xf0\x9f\x98\x80\\n\\t\\u0001\x7f\xe2\x80\xa8\xc3\xa9\"}";
        let second: &[u8] = b"{\"at\":\"2026-11-22T02:16:43.300060Z\",\"omitted\":\"too-large\"}";
        assert_eq!(prompt_to_json(&text).as_bytes(), first);
        assert_eq!(prompt_to_json(&omitted).as_bytes(), second);
        let both = [text, omitted];
        let list = prompts_to_json(&both);
        assert_eq!(list.as_bytes(), [b"[", first, b",", second, b"]"].concat());
        assert_eq!(prompts_to_json(&[]), "[]");
        // and back
        assert_eq!(prompts_from_json(&list).unwrap(), both);
        assert_eq!(
            prompt_from_json(&prompt_to_json(&both[0])).unwrap(),
            both[0]
        );
        assert_eq!(prompts_from_json("[]").unwrap(), []);
        // every other control character is written as Python writes it
        for code in (0u32..0x20).chain([0x7f, 0x80, 0x9f]) {
            let c = char::from_u32(code).unwrap();
            let escaped = match c {
                '\u{8}' => "\\b".to_string(),
                '\t' => "\\t".to_string(),
                '\n' => "\\n".to_string(),
                '\u{c}' => "\\f".to_string(),
                '\r' => "\\r".to_string(),
                _ if code < 0x20 => format!("\\u{code:04x}"),
                c => c.to_string(),
            };
            let entry = PromptEntry::text(AT, &c.to_string()).unwrap();
            assert_eq!(
                prompt_to_json(&entry),
                format!("{{\"at\":\"{AT}\",\"text\":\"{escaped}\"}}")
            );
        }
    }

    #[test]
    fn a_stored_prompt_is_read_only_in_its_written_form() {
        let written = format!("{{\"at\":\"{AT}\",\"text\":\"x\"}}");
        assert_eq!(
            prompt_from_json(&written).unwrap(),
            PromptEntry::text(AT, "x").unwrap()
        );
        // the keys in the other order read back too: the value re-encodes to the same text
        let swapped = format!("{{\"text\":\"x\",\"at\":\"{AT}\"}}");
        assert_eq!(
            prompt_from_json(&swapped).unwrap(),
            PromptEntry::text(AT, "x").unwrap()
        );
        for (raw, message) in [
            (
                format!("{{\"at\": \"{AT}\",\"text\":\"x\"}}"),
                "a JSON column is not in its written form",
            ),
            (
                format!("{{\"at\":\"{AT}\",\"text\":\"\\u00e9\"}}"),
                "a JSON column is not in its written form",
            ),
            (
                format!("{{\"at\":\"{AT}\",\"text\":\"\\/\"}}"),
                "a JSON column is not in its written form",
            ),
            (
                format!("{{\"at\":\"{AT}\",\"at\":\"{AT}\",\"text\":\"x\"}}"),
                "a JSON column is not in its written form",
            ),
            (
                format!("{{\"at\":\"{AT}\",\"text\":\"x\"}}\n"),
                "a JSON column is not in its written form",
            ),
            (
                format!("{{\"at\":\"{AT}\",\"text\":\"x\",\"omitted\":\"secret\"}}"),
                "a prompt entry is not {at, text} or {at, omitted}",
            ),
            (
                format!("{{\"at\":\"{AT}\"}}"),
                "a prompt entry is not {at, text} or {at, omitted}",
            ),
            (
                "[\"x\"]".to_string(),
                "a prompt entry is not {at, text} or {at, omitted}",
            ),
            (
                format!("{{\"at\":\"{AT}\",\"omitted\":\"because\"}}"),
                "unknown prompt omission",
            ),
            (
                format!("{{\"at\":\"{AT}\",\"text\":5}}"),
                "prompt entry text is not text",
            ),
            (
                format!("{{\"at\":\"{AT}\",\"text\":null}}"),
                "a prompt entry holds exactly one of text and omitted",
            ),
            (
                "{\"at\":\"yesterday\",\"text\":\"x\"}".to_string(),
                "prompt entry time is not a timestamp",
            ),
        ] {
            assert_eq!(
                prompt_from_json(&raw).unwrap_err().to_string(),
                message,
                "{raw}"
            );
        }
        assert_eq!(
            prompts_from_json(&written).unwrap_err().to_string(),
            "recent_prompts is not a list"
        );
        assert!(prompts_from_json(&format!("[{written},5]")).is_err());
        assert!(prompts_from_json("[").is_err());
    }

    #[test]
    fn a_rejected_prompt_never_echoes_its_text() {
        let refused = PromptEntry::text("not a time", "my secret prompt").unwrap_err();
        assert!(!refused.to_string().contains("my secret prompt"));
        let raw = "{\"at\":\"not a time\",\"text\":\"my secret prompt\"}";
        assert!(
            !prompt_from_json(raw)
                .unwrap_err()
                .to_string()
                .contains("my secret prompt")
        );
        let broken = "{\"at\":\"not a time\",\"text\":\"my secret prompt\"";
        assert!(
            !prompt_from_json(broken)
                .unwrap_err()
                .to_string()
                .contains("my secret prompt")
        );
    }

    fn a_move(
        seq: i64,
        moved_at: &str,
        from: Option<&str>,
        to: Option<&str>,
    ) -> Result<SessionMove, ValueError> {
        SessionMove::new(
            key(),
            seq,
            moved_at,
            "/a",
            from.map(str::to_string),
            "/b",
            to.map(str::to_string),
        )
    }

    #[test]
    fn a_session_move_holds_what_it_was_given() {
        let moved = a_move(1, AT, Some("0123456789"), None).unwrap();
        assert_eq!(
            (
                moved.seq,
                moved.from_project_id.as_deref(),
                moved.to_project_id,
                moved.moved_at.as_str()
            ),
            (1, Some("0123456789"), None, AT)
        );
        assert_eq!(
            (moved.from_entry_cwd.as_str(), moved.to_entry_cwd.as_str()),
            ("/a", "/b")
        );
    }

    #[test]
    fn a_session_move_rejects_a_value_it_could_not_hold() {
        for seq in [0, -1] {
            assert_eq!(
                a_move(seq, AT, None, None).unwrap_err().to_string(),
                "move sequence is not a positive integer"
            );
        }
        assert_eq!(
            a_move(1, "yesterday", None, None).unwrap_err().to_string(),
            "move time is not a timestamp"
        );
        for (from, to) in [(Some("short"), None), (None, Some("ABCDEFGHIJ"))] {
            assert_eq!(
                a_move(1, AT, from, to).unwrap_err().to_string(),
                "a move project id is not addressable"
            );
        }
    }

    #[test]
    fn a_call_record_is_a_key_and_an_optional_directory() {
        assert_eq!(
            CallRecord {
                key: key(),
                cwd: Some("/w".to_string())
            }
            .cwd
            .as_deref(),
            Some("/w")
        );
        assert_eq!(
            CallRecord {
                key: key(),
                cwd: None
            }
            .cwd,
            None
        );
    }

    #[test]
    fn a_move_outcome_changed_truth_table() {
        let a = Some("aaaaaaaaaa");
        let b = Some("bbbbbbbbbb");
        // (from project, to project, from candidate, to candidate, pending, degraded) -> changed
        for (row, changed) in [
            ((None, None, None, None, false, false), false),
            ((a, a, None, None, false, false), false),
            ((a, None, None, None, false, false), true),
            ((None, b, None, None, false, false), true),
            ((a, b, None, None, false, false), true),
            ((None, None, None, None, false, true), true),
            ((None, None, None, None, true, false), false),
            ((None, None, a, a, true, false), false),
            ((None, None, None, b, true, false), true),
            // a pending row compares candidates, never project ids
            ((a, None, None, None, true, false), false),
            // a registered row compares project ids, never candidates
            ((None, None, None, b, false, false), false),
        ] {
            let outcome = MoveOutcome {
                from_project_id: row.0.map(str::to_string),
                to_project_id: row.1.map(str::to_string),
                from_candidate_id: row.2.map(str::to_string),
                to_candidate_id: row.3.map(str::to_string),
                to_entry_cwd: "/b".to_string(),
                pending: row.4,
                degraded: row.5,
            };
            assert_eq!(outcome.changed(), changed, "{row:?}");
        }
    }
}
