//! Operations, changes and history: what a batch of writes takes and returns.
//!
//! Every creation or state change of a memory is one operation of one change; a
//! change keeps a step per memory it touched, a memory keeps every version it ever
//! had. Values only: the rules live in the store and the services.

use std::collections::BTreeSet;

use super::errors::named;
use super::memory::{MemoryType, Trust};

named! {
    OpName {
        Create = "create",
        Update = "update",
        SoftDelete = "soft_delete",
        Restore = "restore",
    }
}

/// One cited version of another memory.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SourceRef {
    pub memory_id: String,
    pub version: i64,
}

/// One operation as a caller hands it in; nothing in it has been checked yet.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Op {
    Create {
        project_id: String,
        type_: String,
        description: String,
        body: String,
        sources: Vec<SourceRef>,
        /// Ignored, like `sync`, when there are sources: the store derives both from them.
        trust: String,
        sync: bool,
        /// None: not judged.
        level: Option<i64>,
        tags: Vec<String>,
    },
    /// None keeps a value; `sources` and `tags` replace the whole set.
    Update {
        memory_id: String,
        expected_version: i64,
        description: Option<String>,
        body: Option<String>,
        sources: Option<Vec<SourceRef>>,
        level: Option<i64>,
        tags: Option<Vec<String>>,
    },
    /// With `unread_since`, also refused when the memory was read at or after that time.
    SoftDelete {
        memory_id: String,
        expected_version: i64,
        unread_since: Option<String>,
    },
    /// Makes `to_version`'s whole recorded state the new current version.
    Restore {
        memory_id: String,
        expected_version: i64,
        to_version: i64,
    },
}

impl Op {
    /// A create with no sources, trust `agent`, synced, not judged and untagged.
    pub fn create(project_id: &str, type_: &str, description: &str, body: &str) -> Op {
        Op::Create {
            project_id: project_id.to_string(),
            type_: type_.to_string(),
            description: description.to_string(),
            body: body.to_string(),
            sources: Vec::new(),
            trust: Trust::Agent.as_str().to_string(),
            sync: true,
            level: None,
            tags: Vec::new(),
        }
    }

    /// An update that keeps every value.
    pub fn update(memory_id: &str, expected_version: i64) -> Op {
        Op::Update {
            memory_id: memory_id.to_string(),
            expected_version,
            description: None,
            body: None,
            sources: None,
            level: None,
            tags: None,
        }
    }

    /// A soft delete that does not look at when the memory was read.
    pub fn soft_delete(memory_id: &str, expected_version: i64) -> Op {
        Op::SoftDelete {
            memory_id: memory_id.to_string(),
            expected_version,
            unread_since: None,
        }
    }

    pub fn name(&self) -> OpName {
        match self {
            Op::Create { .. } => OpName::Create,
            Op::Update { .. } => OpName::Update,
            Op::SoftDelete { .. } => OpName::SoftDelete,
            Op::Restore { .. } => OpName::Restore,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Step {
    pub step: i64,
    pub memory_id: String,
    pub op: OpName,
    pub before_version: Option<i64>,
    pub after_version: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Change {
    pub change_id: String,
    pub at: String,
    pub changed_by: String,
    pub changed_via: Option<String>,
    pub step_count: i64,
    pub undoes: Option<String>,
    /// The steps still stored (fewer after a hard delete).
    pub steps: Vec<Step>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemoryVersion {
    pub memory_id: String,
    pub version: i64,
    pub type_: MemoryType,
    pub trust: Trust,
    pub sync: bool,
    pub description: String,
    pub body: String,
    pub deleted: bool,
    pub sources: Vec<SourceRef>,
    /// None = an imported version.
    pub change: Option<Change>,
    pub level: Option<i64>,
    pub tags: Vec<String>,
}

/// The limits every write keeps for each project's level-1 entries and tags: a write
/// never breaks one it kept, nor grows a breach a lowered limit left.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MetadataCaps {
    /// Current level-1 entries of one project.
    pub standing_max_count: usize,
    /// The body of one level-1 entry.
    pub standing_max_chars: usize,
    /// Distinct names the current entries of one project carry.
    pub tags_max: usize,
    /// Names one entry carries.
    pub tags_per_memory_max: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Usage {
    pub reads: i64,
    pub last_read_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Citation {
    /// The citing memory.
    pub memory_id: String,
    /// The citing version.
    pub version: i64,
    /// The cited version of the queried memory.
    pub source_version: i64,
    /// The citing version is its memory's current version.
    pub current: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyHit {
    pub memory_id: String,
    pub version: i64,
    pub rule_id: String,
    pub current: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanCitation {
    pub citing_id: String,
    pub citing_version: i64,
    pub cited_id: String,
    pub cited_version: i64,
    pub citing_current: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HardDeleteItem {
    pub memory_id: String,
    pub project_id: String,
    pub version: i64,
    pub deleted: bool,
    /// Why it is in the plan (empty for the target).
    pub citations: Vec<PlanCitation>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HardDeletePlan {
    pub target: String,
    /// Target first, then by id.
    pub items: Vec<HardDeleteItem>,
    /// 16 hex characters.
    pub code: String,
}

impl HardDeletePlan {
    /// The (memory id, current version) set an interactive confirmation shows.
    pub fn expected(&self) -> BTreeSet<(&str, i64)> {
        self.items
            .iter()
            .map(|item| (item.memory_id.as_str(), item.version))
            .collect()
    }
}

/// How a hard delete was confirmed: exactly one of the set that was shown and its code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Confirmation<'a> {
    /// `HardDeletePlan::expected` of the plan that was shown.
    Expected(&'a BTreeSet<(&'a str, i64)>),
    /// `HardDeletePlan::code` of the plan that was shown.
    Code(&'a str),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn create_defaults_match_the_python_ones() {
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
        } = Op::create("pppppppppp", "project", "cue", "body")
        else {
            panic!("not a create");
        };
        assert_eq!(
            (
                project_id.as_str(),
                type_.as_str(),
                description.as_str(),
                body.as_str()
            ),
            ("pppppppppp", "project", "cue", "body")
        );
        assert_eq!(
            (sources, trust.as_str(), sync, level, tags),
            (vec![], "agent", true, None, vec![])
        );
    }

    #[test]
    fn update_keeps_every_value_it_is_not_given() {
        let Op::Update {
            memory_id,
            expected_version,
            description,
            body,
            sources,
            level,
            tags,
        } = Op::update("mmmmmmmmmm", 3)
        else {
            panic!("not an update");
        };
        assert_eq!((memory_id.as_str(), expected_version), ("mmmmmmmmmm", 3));
        assert_eq!(
            (description, body, sources, level, tags),
            (None, None, None, None, None)
        );
    }

    #[test]
    fn soft_delete_unread_since_defaults_to_none() {
        assert_eq!(
            Op::soft_delete("mmmmmmmmmm", 1),
            Op::SoftDelete {
                memory_id: "mmmmmmmmmm".to_string(),
                expected_version: 1,
                unread_since: None
            }
        );
    }

    #[test]
    fn every_operation_has_its_stored_name() {
        let ops = [
            Op::create("pppppppppp", "user", "", "b"),
            Op::update("mmmmmmmmmm", 1),
            Op::soft_delete("mmmmmmmmmm", 1),
            Op::Restore {
                memory_id: "mmmmmmmmmm".to_string(),
                expected_version: 2,
                to_version: 1,
            },
        ];
        let names: Vec<&str> = ops.iter().map(|op| op.name().as_str()).collect();
        assert_eq!(names, ["create", "update", "soft_delete", "restore"]);
        for name in names {
            assert_eq!(OpName::parse(name).unwrap().as_str(), name);
        }
        assert_eq!(OpName::parse("delete"), None);
    }

    #[test]
    fn a_plan_expects_every_item_at_its_current_version() {
        let item = |memory_id: &str, version, deleted| HardDeleteItem {
            memory_id: memory_id.to_string(),
            project_id: "pppppppppp".to_string(),
            version,
            deleted,
            citations: vec![],
        };
        let plan = HardDeletePlan {
            target: "aaaaaaaaaa".to_string(),
            items: vec![item("bbbbbbbbbb", 1, true), item("aaaaaaaaaa", 3, false)],
            code: "0123456789abcdef".to_string(),
        };
        let expected: Vec<(&str, i64)> = plan.expected().into_iter().collect();
        assert_eq!(expected, [("aaaaaaaaaa", 3), ("bbbbbbbbbb", 1)]);
    }
}
