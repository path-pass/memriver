//! Project contexts: the header, state and read/write set a caller answers for.
//!
//! Shared by the project service (a directory's context) and the session service (a
//! session row's context), so both build the same header for the same facts. Plain
//! functions over models: no store, no service.

use crate::models::context::{ProjectContext, ProjectContextState, ReadWriteSet};
use crate::models::helpers::{cut, single_line};
use crate::models::project::Project;
use crate::models::session::SessionKey;

// The project context header's fixed lines; the registered and degraded lines are
// composed per project context below.
pub const NONE_HEADER: &str =
    "project: none — global is read-only; ask the user to run memriver project init";
pub const STORE_UNREADABLE_HEADER: &str = "project: unavailable — the memory store could not be \
                                           read; ask the user to run memriver doctor";
pub const PENDING_HEADER: &str = "project: awaiting confirmation — this session is not \
                                  registered; ask the user, then call session_confirm";
// pending with no candidate: confirming would register the session with no project
// (session_register can assign one later), so the header offers session_register rather
// than session_confirm
pub const PENDING_NO_CANDIDATE_HEADER: &str = "project: none — this session is not registered, and its recorded directory is not in any \
     registered project, so global is read-only; to save, ask the user to run memriver project \
     init in the directory this session is working in, then call session_register";
pub const UNIDENTIFIED_HEADER: &str = "project: none — this session is not registered with memriver: its hooks may not be \
     installed (memriver install) or trusted (Codex asks on start), or the harness sent no \
     session id; ask the user to fix that, then start a new session";
// a session row's project never re-resolves on its own: one with no project gets one only
// through session_register (its recorded directory, else the directory the call came
// from) or a Claude Code /cd, and one whose project is gone needs a new session
pub const SESSION_NONE_HEADER: &str = "project: none — this session has no project, so global is read-only; to save, ask the \
     user to run memriver project init in the directory this session is working in, then call \
     session_register";
pub const SESSION_PROJECT_GONE_HEADER: &str = "project: unavailable — this session's project no longer exists or became global, so \
     global is read-only; to save, ask the user to start a new session";

/// One stored value as it may appear in the agent-facing header.
pub fn header_field(value: &str, max_chars: usize) -> String {
    cut(&single_line(value), max_chars).to_string()
}

pub fn registered_context(
    project: Project,
    global_project_id: Option<String>,
    header_field_chars: usize,
    session_key: Option<SessionKey>,
) -> ProjectContext {
    ProjectContext {
        state: ProjectContextState::Registered,
        header: format!(
            "project: {} [{}] (root {})",
            header_field(&project.name, header_field_chars),
            project.id,
            header_field(
                project.root.as_deref().unwrap_or_default(),
                header_field_chars
            )
        ),
        read_write_set: ReadWriteSet {
            project_id: Some(project.id.clone()),
            global_project_id,
        },
        project: Some(project),
        diagnostic: None,
        session_key,
    }
}

pub fn degraded_context(
    diagnostic: &str,
    global_project_id: Option<String>,
    header_field_chars: usize,
    session_key: Option<SessionKey>,
) -> ProjectContext {
    ProjectContext {
        state: ProjectContextState::Degraded,
        header: format!(
            "project: unavailable — this directory could not be matched to one project ({}); \
             ask the user to run memriver project explain",
            header_field(diagnostic, header_field_chars)
        ),
        read_write_set: ReadWriteSet {
            project_id: None,
            global_project_id,
        },
        project: None,
        diagnostic: Some(diagnostic.to_string()),
        session_key,
    }
}

/// A context that may read global only.
pub fn no_project_context(
    state: ProjectContextState,
    header: &str,
    global_project_id: Option<String>,
    session_key: Option<SessionKey>,
) -> ProjectContext {
    ProjectContext {
        state,
        header: header.to_string(),
        read_write_set: ReadWriteSet {
            project_id: None,
            global_project_id,
        },
        project: None,
        diagnostic: None,
        session_key,
    }
}

pub fn unavailable_context(session_key: Option<SessionKey>) -> ProjectContext {
    no_project_context(
        ProjectContextState::Unavailable,
        STORE_UNREADABLE_HEADER,
        None,
        session_key,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::session::Harness;

    const P: &str = "pppppppppp";
    const G: &str = "gggggggggg";

    fn project(name: &str, root: Option<&str>) -> Project {
        Project {
            id: P.to_string(),
            name: name.to_string(),
            root: root.map(str::to_string),
        }
    }

    #[test]
    fn the_fixed_headers_are_the_sentences_agents_are_given() {
        assert_eq!(
            NONE_HEADER,
            "project: none \u{2014} global is read-only; ask the user to run memriver project init"
        );
        assert_eq!(
            STORE_UNREADABLE_HEADER,
            "project: unavailable \u{2014} the memory store could not be read; ask the user to run \
             memriver doctor"
        );
        assert_eq!(
            PENDING_HEADER,
            "project: awaiting confirmation \u{2014} this session is not registered; ask the user, \
             then call session_confirm"
        );
        assert_eq!(
            PENDING_NO_CANDIDATE_HEADER,
            "project: none \u{2014} this session is not registered, and its recorded directory is \
             not in any registered project, so global is read-only; to save, ask the user to run \
             memriver project init in the directory this session is working in, then call \
             session_register"
        );
        assert_eq!(
            UNIDENTIFIED_HEADER,
            "project: none \u{2014} this session is not registered with memriver: its hooks may \
             not be installed (memriver install) or trusted (Codex asks on start), or the harness \
             sent no session id; ask the user to fix that, then start a new session"
        );
        assert_eq!(
            SESSION_NONE_HEADER,
            "project: none \u{2014} this session has no project, so global is read-only; to save, \
             ask the user to run memriver project init in the directory this session is working \
             in, then call session_register"
        );
        assert_eq!(
            SESSION_PROJECT_GONE_HEADER,
            "project: unavailable \u{2014} this session's project no longer exists or became \
             global, so global is read-only; to save, ask the user to start a new session"
        );
    }

    #[test]
    fn a_registered_context_names_the_project_and_its_root() {
        let key = SessionKey::new(Harness::ClaudeCode, "s1").unwrap();
        let context = registered_context(
            project("demo", Some("/w")),
            Some(G.to_string()),
            120,
            Some(key.clone()),
        );
        assert_eq!(context.state, ProjectContextState::Registered);
        assert_eq!(context.header, format!("project: demo [{P}] (root /w)"));
        assert_eq!(
            context.read_write_set,
            ReadWriteSet {
                project_id: Some(P.to_string()),
                global_project_id: Some(G.to_string()),
            }
        );
        assert_eq!(context.project, Some(project("demo", Some("/w"))));
        assert_eq!((context.diagnostic, context.session_key), (None, Some(key)));
        // a project without a root has an empty one in its header
        let unbound = registered_context(project("demo", None), None, 120, None);
        assert_eq!(unbound.header, format!("project: demo [{P}] (root )"));
    }

    #[test]
    fn a_degraded_context_says_why_and_grants_global_only() {
        let diagnostic = "/r: matched by more than one project";
        let context = degraded_context(diagnostic, Some(G.to_string()), 120, None);
        assert_eq!(context.state, ProjectContextState::Degraded);
        assert_eq!(
            context.header,
            "project: unavailable \u{2014} this directory could not be matched to one project \
             (/r: matched by more than one project); ask the user to run memriver project explain"
        );
        assert_eq!(context.diagnostic.as_deref(), Some(diagnostic));
        assert_eq!(context.read_write_set.project_id, None);
        assert_eq!(context.read_write_set.global_project_id.as_deref(), Some(G));
        assert_eq!(context.project, None);
    }

    #[test]
    fn header_fields_are_single_line_and_capped() {
        let name = format!("n\nx{}", "y".repeat(200));
        let root = format!("/w\n{}", "\u{9879}".repeat(200));
        let header = registered_context(project(&name, Some(&root)), None, 120, None).header;
        let (shown_name, shown_root) = (
            format!("n x{}", "y".repeat(117)),
            format!("/w {}", "\u{9879}".repeat(117)),
        );
        assert_eq!(
            header,
            format!("project: {shown_name} [{P}] (root {shown_root})")
        );
        assert_eq!(shown_root.chars().count(), 120);

        let diagnostic = format!("/r:\n{}", "d".repeat(200));
        let context = degraded_context(&diagnostic, None, 120, None);
        assert!(!context.header.contains('\n'));
        assert!(
            context
                .header
                .contains(&format!("(/r: {}); ask the user", "d".repeat(116)))
        );
        // the diagnostic itself is kept whole
        assert_eq!(context.diagnostic, Some(diagnostic));
        assert_eq!(header_field("  a \u{2028} b  ", 3), "a b");
    }

    #[test]
    fn contexts_without_a_project_read_global_at_most() {
        let none = no_project_context(
            ProjectContextState::None,
            NONE_HEADER,
            Some(G.to_string()),
            None,
        );
        assert_eq!(
            (none.state, none.header.as_str()),
            (ProjectContextState::None, NONE_HEADER)
        );
        assert_eq!(
            none.read_write_set
                .readable()
                .into_iter()
                .collect::<Vec<_>>(),
            [G]
        );
        assert!(none.read_write_set.writable().is_empty());
        let key = SessionKey::new(Harness::Codex, "s2").unwrap();
        let unavailable = unavailable_context(Some(key.clone()));
        assert_eq!(unavailable.state, ProjectContextState::Unavailable);
        assert_eq!(unavailable.header, STORE_UNREADABLE_HEADER);
        assert!(unavailable.read_write_set.readable().is_empty());
        assert_eq!(unavailable.session_key, Some(key));
    }
}
