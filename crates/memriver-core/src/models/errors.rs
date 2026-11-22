//! The stable error taxonomy shared by every store and every transport.
//!
//! Two kinds of error live here, and they differ in who owns the words. The
//! storage-boundary errors carry structured fields only: their `Display` is a line
//! for logs and must never reach a client — a transport composes client copy from the
//! operation and the fields. `ContentRejected` and `GlobalReadOnly` carry a message
//! written inside the core, where the wording is the rule being explained; it never
//! echoes the rejected value and may be forwarded as it is.

use std::fmt;

use super::changes::HardDeletePlan;

/// A value that cannot be what it claims to be: a caller's mistake, or a stored row
/// memriver could not have written. The message names the rule, never the value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValueError(String);

impl ValueError {
    pub fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl fmt::Display for ValueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ValueError {}

/// A closed set of names: the variants, each with the text it is stored and shown as.
macro_rules! named {
    ($(#[$doc:meta])* $name:ident { $($variant:ident = $text:literal),+ $(,)? }) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum $name { $($variant),+ }

        impl $name {
            pub const ALL: &'static [$name] = &[$($name::$variant),+];

            pub fn as_str(self) -> &'static str {
                match self { $($name::$variant => $text),+ }
            }

            pub fn parse(text: &str) -> Option<Self> {
                Self::ALL.iter().copied().find(|name| name.as_str() == text)
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str(self.as_str())
            }
        }
    };
}
pub(crate) use named;

named! {
    /// Why a directory could not be planned, bound or unbound.
    BindingReason {
        NotADirectory = "not-a-directory",
        CoversHome = "covers-home",
        CoversStore = "covers-store",
        InsideStore = "inside-store",
        BoundElsewhere = "bound-elsewhere",
        Unverifiable = "unverifiable",
        IsGlobal = "is-global",
        HasDirectory = "has-directory",
        PlanChanged = "plan-changed",
        BindingChanged = "binding-changed",
        NoSuchProject = "no-such-project",
    }
}

named! {
    /// Why one operation of a batch failed its check inside the transaction.
    BatchConflictReason {
        Version = "version",
        Deleted = "deleted",
        SameState = "same-state",
        Source = "source",
        Cycle = "cycle",
        ReadSince = "read-since",
        Missing = "missing",
    }
}

named! {
    /// A metadata limit, by its setting's name.
    Cap {
        StandingMaxCount = "standing_max_count",
        StandingMaxChars = "standing_max_chars",
        TagsMax = "tags_max",
        TagsPerMemoryMax = "tags_per_memory_max",
    }
}

named! {
    /// Why a change cannot be undone.
    UndoRefusedReason {
        NotFound = "not-found",
        HardDeleted = "hard-deleted",
        Changed = "changed",
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CoreError {
    /// One operation of an `apply` failed its check inside the transaction; nothing
    /// was written. `index` is its position in the batch, `memory_id` the memory it
    /// names (None for a create).
    BatchConflict {
        index: usize,
        memory_id: Option<String>,
        reason: BatchConflictReason,
    },
    /// A directory could not be planned, bound or unbound; nothing was written.
    /// `project_id` names the other project for `BoundElsewhere`.
    BindingRefused {
        reason: BindingReason,
        project_id: Option<String>,
    },
    /// A change would break one of the metadata limits; nothing was written.
    /// `memory_id` is the memory whose own state breaks it (None for a project's count).
    CapExceeded { cap: Cap, memory_id: Option<String> },
    /// From the content policy. `rule_id` is the policy's id for what matched, never
    /// the matched text; `memory_id` the memory whose resulting state was refused;
    /// `detail` a short machine label refining the rule. With an empty `message` the
    /// text is composed from the rule id alone.
    ContentRejected {
        message: String,
        rule_id: String,
        memory_id: Option<String>,
        detail: String,
    },
    /// The global project is read-only to agents; no mutation reaches it.
    GlobalReadOnly,
    /// No memory the caller may read answers to this id. Absent, another project's and
    /// orphaned are one answer; a row that is present but unreadable is `StorageFailure`.
    MemoryNotFound(String),
    /// A hard delete's plan differs from the one confirmed; nothing was deleted. Holds
    /// the plan as it stands now.
    PlanChanged(HardDeletePlan),
    /// No project answers to this id.
    ProjectNotFound(String),
    /// No project this session may use; nothing was written. `reason` names the cause
    /// where one applies, and is empty where the caller's context already says why.
    ProjectUnavailable { reason: String },
    /// The session is unknown, not bound to a project, or active since it was read.
    SessionMoved,
    /// The store failed for an infrastructure reason. Deliberately fieldless: paths,
    /// errno text, SQL and driver messages do not cross this boundary.
    StorageFailure,
    /// The store's schema version is not the one this version reads.
    StoreNeedsUpgrade(i64),
    /// A change cannot be undone; nothing was written. `memory_ids` names the memories
    /// that changed since, for `Changed`.
    UndoRefused {
        reason: UndoRefusedReason,
        memory_ids: Vec<String>,
    },
    /// The memory is readable and writable, but not at the version the caller read.
    VersionConflict(String),
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BatchConflict { index, reason, .. } => {
                write!(f, "batch conflict: {reason} at operation {index}")
            }
            Self::BindingRefused { reason, .. } => write!(f, "binding refused: {reason}"),
            Self::CapExceeded { cap, .. } => write!(f, "cap exceeded: {cap}"),
            Self::ContentRejected {
                message, rule_id, ..
            } if message.is_empty() => {
                write!(f, "content rejected ({rule_id}); no change was made")
            }
            Self::ContentRejected { message, .. } => f.write_str(message),
            Self::GlobalReadOnly => {
                f.write_str("global memories are read-only to agents; no change was made")
            }
            Self::MemoryNotFound(memory_id) => write!(f, "memory not found: {memory_id}"),
            Self::PlanChanged(plan) => write!(f, "plan changed: {}", plan.target),
            Self::ProjectNotFound(project_id) => write!(f, "project not found: {project_id}"),
            Self::ProjectUnavailable { reason } => write!(f, "project unavailable: {reason}"),
            Self::SessionMoved => f.write_str("session moved"),
            Self::StorageFailure => f.write_str("storage failure"),
            Self::StoreNeedsUpgrade(version) => {
                write!(f, "store schema {version} is not supported")
            }
            Self::UndoRefused { reason, .. } => write!(f, "undo refused: {reason}"),
            Self::VersionConflict(memory_id) => write!(f, "version conflict: {memory_id}"),
        }
    }
}

impl std::error::Error for CoreError {}

/// Why a write was not made: what it was handed is no write at all (an error of the
/// caller, in Python a `ValueError`), or the store refused it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WriteError {
    Value(ValueError),
    Core(CoreError),
}

impl fmt::Display for WriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WriteError::Value(err) => err.fmt(f),
            WriteError::Core(err) => err.fmt(f),
        }
    }
}

impl std::error::Error for WriteError {}

impl From<ValueError> for WriteError {
    fn from(err: ValueError) -> WriteError {
        WriteError::Value(err)
    }
}

impl From<CoreError> for WriteError {
    fn from(err: CoreError) -> WriteError {
        WriteError::Core(err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::changes::HardDeletePlan;

    fn id(text: &str) -> Option<String> {
        Some(text.to_string())
    }

    #[test]
    fn display_matches_python_text() {
        let plan = HardDeletePlan {
            target: "aaaaaaaaaa".to_string(),
            items: vec![],
            code: "0123456789abcdef".to_string(),
        };
        let rejected = |message: &str, rule_id: &str, detail: &str| CoreError::ContentRejected {
            message: message.to_string(),
            rule_id: rule_id.to_string(),
            memory_id: None,
            detail: detail.to_string(),
        };
        for (error, text) in [
            (
                CoreError::BatchConflict {
                    index: 2,
                    memory_id: id("mmmmmmmmmm"),
                    reason: BatchConflictReason::Version,
                },
                "batch conflict: version at operation 2",
            ),
            (
                CoreError::BatchConflict {
                    index: 0,
                    memory_id: None,
                    reason: BatchConflictReason::ReadSince,
                },
                "batch conflict: read-since at operation 0",
            ),
            (
                CoreError::BindingRefused {
                    reason: BindingReason::BoundElsewhere,
                    project_id: id("pppppppppp"),
                },
                "binding refused: bound-elsewhere",
            ),
            (
                CoreError::BindingRefused {
                    reason: BindingReason::PlanChanged,
                    project_id: None,
                },
                "binding refused: plan-changed",
            ),
            (
                CoreError::CapExceeded {
                    cap: Cap::TagsMax,
                    memory_id: None,
                },
                "cap exceeded: tags_max",
            ),
            (
                CoreError::CapExceeded {
                    cap: Cap::StandingMaxChars,
                    memory_id: id("mmmmmmmmmm"),
                },
                "cap exceeded: standing_max_chars",
            ),
            (
                rejected("", "github-pat", ""),
                "content rejected (github-pat); no change was made",
            ),
            (
                rejected("", "", ""),
                "content rejected (); no change was made",
            ),
            (
                rejected("", "classifier-unavailable", "timeout"),
                "content rejected (classifier-unavailable); no change was made",
            ),
            (
                rejected("content is empty; nothing to store", "empty", ""),
                "content is empty; nothing to store",
            ),
            (
                CoreError::GlobalReadOnly,
                "global memories are read-only to agents; no change was made",
            ),
            (
                CoreError::MemoryNotFound("mmmmmmmmmm".to_string()),
                "memory not found: mmmmmmmmmm",
            ),
            (CoreError::PlanChanged(plan), "plan changed: aaaaaaaaaa"),
            (
                CoreError::ProjectNotFound("pppppppppp".to_string()),
                "project not found: pppppppppp",
            ),
            (
                CoreError::ProjectUnavailable {
                    reason: String::new(),
                },
                "project unavailable: ",
            ),
            (
                CoreError::ProjectUnavailable {
                    reason: "candidate-changed".to_string(),
                },
                "project unavailable: candidate-changed",
            ),
            (CoreError::SessionMoved, "session moved"),
            (CoreError::StorageFailure, "storage failure"),
            (
                CoreError::StoreNeedsUpgrade(2),
                "store schema 2 is not supported",
            ),
            (
                CoreError::UndoRefused {
                    reason: UndoRefusedReason::Changed,
                    memory_ids: vec!["aaaaaaaaaa".to_string(), "bbbbbbbbbb".to_string()],
                },
                "undo refused: changed",
            ),
            (
                CoreError::UndoRefused {
                    reason: UndoRefusedReason::HardDeleted,
                    memory_ids: vec![],
                },
                "undo refused: hard-deleted",
            ),
            (
                CoreError::VersionConflict("mmmmmmmmmm".to_string()),
                "version conflict: mmmmmmmmmm",
            ),
        ] {
            assert_eq!(error.to_string(), text);
        }
        assert_eq!(
            ValueError::new("invalid session id").to_string(),
            "invalid session id"
        );
    }

    #[test]
    fn the_reasons_are_the_closed_sets_python_names() {
        let binding: Vec<&str> = BindingReason::ALL
            .iter()
            .map(|reason| reason.as_str())
            .collect();
        assert_eq!(
            binding,
            [
                "not-a-directory",
                "covers-home",
                "covers-store",
                "inside-store",
                "bound-elsewhere",
                "unverifiable",
                "is-global",
                "has-directory",
                "plan-changed",
                "binding-changed",
                "no-such-project"
            ]
        );
        let batch: Vec<&str> = BatchConflictReason::ALL
            .iter()
            .map(|reason| reason.as_str())
            .collect();
        assert_eq!(
            batch,
            [
                "version",
                "deleted",
                "same-state",
                "source",
                "cycle",
                "read-since",
                "missing"
            ]
        );
        let caps: Vec<&str> = Cap::ALL.iter().map(|cap| cap.as_str()).collect();
        assert_eq!(
            caps,
            [
                "standing_max_count",
                "standing_max_chars",
                "tags_max",
                "tags_per_memory_max"
            ]
        );
        let undo: Vec<&str> = UndoRefusedReason::ALL
            .iter()
            .map(|reason| reason.as_str())
            .collect();
        assert_eq!(undo, ["not-found", "hard-deleted", "changed"]);
        // a name outside its set is not a reason
        assert_eq!(
            BindingReason::parse("bound-elsewhere"),
            Some(BindingReason::BoundElsewhere)
        );
        assert_eq!(BindingReason::parse("because"), None);
        assert_eq!(BatchConflictReason::parse("because"), None);
        assert_eq!(Cap::parse("max_body_chars"), None);
        assert_eq!(UndoRefusedReason::parse("expired"), None);
    }
}
