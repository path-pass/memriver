//! What a resolved directory or session may read and write, and how every surface
//! states it.

use std::collections::BTreeSet;

use super::errors::named;
use super::project::Project;
use super::session::SessionKey;

/// Which projects one caller may read and write. Built only by the project and
/// session services from a resolved context, never from tool arguments: knowing an id
/// grants nothing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadWriteSet {
    pub project_id: Option<String>,
    pub global_project_id: Option<String>,
}

impl ReadWriteSet {
    pub fn readable(&self) -> BTreeSet<&str> {
        [&self.project_id, &self.global_project_id]
            .into_iter()
            .flatten()
            .map(String::as_str)
            .collect()
    }

    /// Global is read-only to every agent-facing caller, whatever the set claims.
    pub fn writable(&self) -> BTreeSet<&str> {
        match &self.project_id {
            Some(project_id) if self.global_project_id.as_ref() != Some(project_id) => {
                BTreeSet::from([project_id.as_str()])
            }
            _ => BTreeSet::new(),
        }
    }
}

named! {
    ResolutionState {
        Registered = "registered",
        None = "none",
        Degraded = "degraded",
    }
}

/// Which project a directory belongs to. `project` only when registered;
/// `diagnostic` (fixed wording, may name a registered root) only when degraded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolution {
    pub state: ResolutionState,
    pub project: Option<Project>,
    pub diagnostic: Option<String>,
}

named! {
    ProjectContextState {
        Registered = "registered",
        None = "none",
        Degraded = "degraded",
        Unavailable = "unavailable",
        Pending = "pending",
        Unidentified = "unidentified",
    }
}

/// One resolved directory or session as every surface states it: header, rights,
/// project. `session_key` is set when the context came from a session, so a save can
/// move that session's watermark without the caller naming it twice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectContext {
    pub state: ProjectContextState,
    pub header: String,
    pub read_write_set: ReadWriteSet,
    pub project: Option<Project>,
    pub diagnostic: Option<String>,
    pub session_key: Option<SessionKey>,
}

#[cfg(test)]
mod tests {
    use super::*;

    const P: &str = "aaaaaaaaaa";
    const G: &str = "gggggggggg";

    fn set(project: Option<&str>, global: Option<&str>) -> ReadWriteSet {
        ReadWriteSet {
            project_id: project.map(str::to_string),
            global_project_id: global.map(str::to_string),
        }
    }

    fn ids(of: std::collections::BTreeSet<&str>) -> Vec<&str> {
        of.into_iter().collect()
    }

    #[test]
    fn a_project_session_reads_both_and_writes_its_project() {
        let read_write_set = set(Some(P), Some(G));
        assert_eq!(ids(read_write_set.readable()), [P, G]);
        assert_eq!(ids(read_write_set.writable()), [P]);
    }

    #[test]
    fn no_project_reads_global_only_and_writes_nothing() {
        let read_write_set = set(None, Some(G));
        assert_eq!(ids(read_write_set.readable()), [G]);
        assert!(read_write_set.writable().is_empty());
    }

    #[test]
    fn an_uninitialized_store_reads_only_the_project() {
        let read_write_set = set(Some(P), None);
        assert_eq!(ids(read_write_set.readable()), [P]);
        assert_eq!(ids(read_write_set.writable()), [P]);
    }

    #[test]
    fn global_is_never_writable_even_if_named_as_the_project() {
        let read_write_set = set(Some(G), Some(G));
        assert_eq!(ids(read_write_set.readable()), [G]);
        assert!(read_write_set.writable().is_empty());
    }

    #[test]
    fn an_empty_set_reads_and_writes_nothing() {
        let read_write_set = set(None, None);
        assert!(read_write_set.readable().is_empty() && read_write_set.writable().is_empty());
    }
}
