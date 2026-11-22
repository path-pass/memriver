//! A memory as stored, and the two pieces of metadata a management write sets.

use super::errors::{ValueError, named};
use super::helpers::{is_id, new_id, now, py_strip};

named! {
    /// The kind of fact a memory holds. `Project` names a kind of fact; it is
    /// unrelated to the project a memory belongs to.
    MemoryType {
        User = "user",
        Feedback = "feedback",
        Project = "project",
        Reference = "reference",
    }
}

named! {
    /// How trustworthy an entry's source material is, not which code path wrote it.
    Trust {
        User = "user",
        Agent = "agent",
        UntrustedDerived = "untrusted-derived",
    }
}

// a tag name is injected into the index and typed back by agents: lowercase ascii
// words joined by single hyphens, at most 32 characters
pub const TAG_NAME_MAX_CHARS: usize = 32;

pub fn is_tag_name(name: &str) -> bool {
    let word = |word: &str| {
        !word.is_empty()
            && word
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
    };
    name.len() <= TAG_NAME_MAX_CHARS && name.split('-').all(word)
}

/// `level` as stored: None (not judged) or an integer of at least 1 (1 = standing).
pub fn checked_level(level: Option<i64>) -> Result<Option<i64>, ValueError> {
    match level {
        Some(level) if level < 1 => Err(ValueError::new(
            "a level is None or an integer of at least 1",
        )),
        level => Ok(level),
    }
}

/// `tags` as stored: distinct valid names, sorted.
pub fn checked_tags(tags: &[String]) -> Result<Vec<String>, ValueError> {
    if !tags.iter().all(|name| is_tag_name(name)) {
        return Err(ValueError::new(
            "a tag name is 1 to 32 of a-z, 0-9 and single inner hyphens",
        ));
    }
    let mut names = tags.to_vec();
    names.sort();
    if names.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(ValueError::new("a memory carries a tag name at most once"));
    }
    Ok(names)
}

/// The tags column: a compact array. Names are ASCII, so nothing is escaped.
pub fn tags_to_json(tags: &[String]) -> String {
    serde_json::Value::from(tags).to_string()
}

/// A stored tags column: any JSON array of valid distinct names, sorted again.
pub fn tags_from_json(text: &str) -> Result<Vec<String>, ValueError> {
    let value: serde_json::Value =
        serde_json::from_str(text).map_err(|_| ValueError::new("tags is not JSON"))?;
    let serde_json::Value::Array(names) = value else {
        return Err(ValueError::new("tags is not a JSON array"));
    };
    let names: Option<Vec<String>> = names
        .iter()
        .map(|name| name.as_str().map(str::to_string))
        .collect();
    // an element that is not text is no tag name
    checked_tags(&names.unwrap_or_else(|| vec![String::new()]))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Memory {
    pub id: String,
    pub project_id: String,
    pub type_: MemoryType,
    // set at creation, never changed
    pub source_harness: String,
    pub source_method: String,
    pub trust: Trust,
    pub sync: bool,
    pub created: String,
    pub updated: String,
    pub description: String,
    pub body: String,
    /// Optimistic concurrency: every update and delete moves it on, and a write names
    /// the version it read.
    pub version: i64,
    /// Set only on the management reads; agent-facing paths never return a deleted memory.
    pub deleted_at: Option<String>,
    /// The last successful read by an agent; None = never read. Not shown to agents.
    pub last_read_at: Option<String>,
    /// 1 = a standing rule, larger is lower, None = not judged. Not shown to agents.
    pub level: Option<i64>,
    /// Sorted names.
    pub tags: Vec<String>,
}

impl Memory {
    /// A memory that has never been stored: a fresh id, one timestamp for `created`
    /// and `updated`, version 1, the description and the body stripped.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        body: &str,
        type_: MemoryType,
        project_id: &str,
        source_harness: &str,
        source_method: &str,
        trust: Trust,
        sync: bool,
        description: &str,
    ) -> Result<Memory, ValueError> {
        if !is_id(project_id) {
            return Err(ValueError::new("invalid project id"));
        }
        let timestamp = now();
        Ok(Memory {
            id: new_id(),
            project_id: project_id.to_string(),
            type_,
            source_harness: source_harness.to_string(),
            source_method: source_method.to_string(),
            trust,
            sync,
            created: timestamp.clone(),
            updated: timestamp,
            description: py_strip(description).to_string(),
            body: py_strip(body).to_string(),
            version: 1,
            deleted_at: None,
            last_read_at: None,
            level: None,
            tags: Vec::new(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::helpers::is_id;

    const PROJECT: &str = "zzzzzzzzzz";

    fn new(body: &str, description: &str) -> Memory {
        Memory::new(
            body,
            MemoryType::User,
            PROJECT,
            "claude-code",
            "agent",
            Trust::Agent,
            true,
            description,
        )
        .unwrap()
    }

    fn names(tags: &[&str]) -> Vec<String> {
        tags.iter().map(|tag| tag.to_string()).collect()
    }

    #[test]
    fn new_generates_an_id_timestamps_and_keeps_the_project() {
        let memory = new(
            "  \u{7528}\u{6237}\u{504f}\u{597d}\u{4e2d}\u{6587}\u{56de}\u{590d} ",
            "",
        );
        assert!(is_id(&memory.id));
        assert_eq!(memory.project_id, PROJECT);
        assert_eq!(memory.created, memory.updated);
        assert_eq!(
            memory.body,
            "\u{7528}\u{6237}\u{504f}\u{597d}\u{4e2d}\u{6587}\u{56de}\u{590d}"
        );
        assert_eq!(
            (memory.trust, memory.sync, memory.description.as_str()),
            (Trust::Agent, true, "")
        );
        assert_eq!(
            (
                memory.source_harness.as_str(),
                memory.source_method.as_str()
            ),
            ("claude-code", "agent")
        );
        assert_eq!(
            (memory.last_read_at, memory.level, memory.tags),
            (None, None, vec![])
        );
    }

    #[test]
    fn new_memories_draw_fresh_ids() {
        let ids: std::collections::HashSet<String> = (0..200).map(|_| new("b", "").id).collect();
        assert_eq!(ids.len(), 200);
    }

    #[test]
    fn new_rejects_a_project_id_outside_the_id_rule() {
        for project_id in ["", "global", "demo-0123456789abcdef", "AAAAAAAAAA"] {
            let refused = Memory::new(
                "b",
                MemoryType::User,
                project_id,
                "",
                "",
                Trust::Agent,
                true,
                "",
            );
            assert_eq!(
                refused.unwrap_err().to_string(),
                "invalid project id",
                "{project_id:?}"
            );
        }
    }

    #[test]
    fn an_unknown_type_or_trust_is_not_one() {
        for name in ["user", "feedback", "project", "reference"] {
            assert_eq!(MemoryType::parse(name).unwrap().as_str(), name);
        }
        for name in ["user", "agent", "untrusted-derived"] {
            assert_eq!(Trust::parse(name).unwrap().as_str(), name);
        }
        for name in ["task", "preference", "User", ""] {
            assert_eq!(MemoryType::parse(name), None, "{name:?}");
        }
        for name in ["bogus", "untrusted", ""] {
            assert_eq!(Trust::parse(name), None, "{name:?}");
        }
    }

    #[test]
    fn description_and_body_are_stripped_as_python_strips() {
        assert_eq!(new("b", "  cue ").description, "cue");
        let memory = new("\u{1f}\u{3000}body\u{1c}\n", "\u{1e}cue\u{a0}");
        assert_eq!(
            (memory.body.as_str(), memory.description.as_str()),
            ("body", "cue")
        );
    }

    #[test]
    fn a_new_memory_starts_at_version_one_and_not_deleted() {
        let memory = new("b", "");
        assert_eq!((memory.version, memory.deleted_at), (1, None));
    }

    #[test]
    fn tags_sorted_distinct_at_most_32() {
        assert_eq!(
            checked_tags(&names(&["b", "a-1", "0"])).unwrap(),
            ["0", "a-1", "b"]
        );
        assert_eq!(checked_tags(&[]).unwrap(), Vec::<String>::new());
        let longest = "a".repeat(32);
        assert_eq!(
            checked_tags(std::slice::from_ref(&longest)).unwrap(),
            [longest]
        );
        for bad in [
            "",
            "A",
            "a-",
            "-a",
            "a--b",
            "a b",
            "a_b",
            "\u{e9}",
            "a\n",
            &"a".repeat(33),
        ] {
            assert_eq!(
                checked_tags(&names(&[bad])).unwrap_err().to_string(),
                "a tag name is 1 to 32 of a-z, 0-9 and single inner hyphens",
                "{bad:?}"
            );
        }
        assert_eq!(
            checked_tags(&names(&["a", "b", "a"]))
                .unwrap_err()
                .to_string(),
            "a memory carries a tag name at most once"
        );
    }

    #[test]
    fn level_refuses_zero() {
        assert_eq!(checked_level(None), Ok(None));
        assert_eq!(checked_level(Some(1)), Ok(Some(1)));
        assert_eq!(checked_level(Some(7)), Ok(Some(7)));
        for bad in [0, -1, i64::MIN] {
            assert_eq!(
                checked_level(Some(bad)).unwrap_err().to_string(),
                "a level is None or an integer of at least 1"
            );
        }
    }

    #[test]
    fn tags_are_stored_as_a_compact_array_and_read_leniently() {
        assert_eq!(tags_to_json(&names(&["a", "b-1"])), "[\"a\",\"b-1\"]");
        assert_eq!(tags_to_json(&[]), "[]");
        assert_eq!(tags_from_json("[\"b\",\"a\"]").unwrap(), ["a", "b"]);
        assert_eq!(tags_from_json(" [ \"b\" , \"a\" ] ").unwrap(), ["a", "b"]);
        assert_eq!(tags_from_json("[]").unwrap(), Vec::<String>::new());
        for (raw, message) in [
            ("[\"a\",\"a\"]", "a memory carries a tag name at most once"),
            (
                "[\"A\"]",
                "a tag name is 1 to 32 of a-z, 0-9 and single inner hyphens",
            ),
            (
                "[1]",
                "a tag name is 1 to 32 of a-z, 0-9 and single inner hyphens",
            ),
            (
                "[[\"a\"]]",
                "a tag name is 1 to 32 of a-z, 0-9 and single inner hyphens",
            ),
            ("{\"a\":1}", "tags is not a JSON array"),
            ("\"a\"", "tags is not a JSON array"),
            ("null", "tags is not a JSON array"),
            ("nope", "tags is not JSON"),
            ("", "tags is not JSON"),
            ("[\"a\"", "tags is not JSON"),
        ] {
            assert_eq!(
                tags_from_json(raw).unwrap_err().to_string(),
                message,
                "{raw:?}"
            );
        }
    }
}
