//! A project: a named collection of memories, bound to at most one directory.

use super::errors::ValueError;
use super::helpers::{new_id, single_line};

/// A readable project name: one line, non-empty, capped. Never an identity. The cap is
/// a setting handed in by the caller.
pub fn project_name(raw: &str, max_chars: usize) -> Result<String, ValueError> {
    let name = single_line(raw);
    if name.is_empty() {
        return Err(ValueError::new("project name is empty"));
    }
    if name.chars().count() > max_chars {
        return Err(ValueError::new(format!(
            "project name is longer than {max_chars} characters"
        )));
    }
    Ok(name)
}

/// Membership lives on a memory's `project_id`. `root` is the canonical directory a
/// harness resolves to this project, or None (global, or unbound).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Project {
    pub id: String,
    pub name: String,
    pub root: Option<String>,
}

impl Project {
    pub fn new(name: &str, max_chars: usize) -> Result<Project, ValueError> {
        Ok(Project {
            id: new_id(),
            name: project_name(name, max_chars)?,
            root: None,
        })
    }
}

/// A directory checked for binding, as shown to the user before confirmation.
/// `already_bound` is for display only; execution decides on the current row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RootPlan {
    pub root: String,
    pub store: String,
    pub nested: Vec<Project>,
    pub already_bound: bool,
}

/// The exact binding a confirmation showed, and the store it was read from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnbindPlan {
    pub project_id: String,
    pub root: String,
    pub store: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::helpers::is_id;

    #[test]
    fn new_generates_an_id_and_normalizes_the_name() {
        let project = Project::new("  my\nwork  ", 120).unwrap();
        assert!(is_id(&project.id));
        assert_eq!((project.name.as_str(), project.root), ("my work", None));
    }

    #[test]
    fn an_empty_name_is_refused() {
        for raw in ["", "   ", "\n\t", "\u{1f}\u{2028}\u{3000}"] {
            assert_eq!(
                project_name(raw, 120).unwrap_err().to_string(),
                "project name is empty"
            );
            assert!(Project::new(raw, 120).is_err());
        }
    }

    #[test]
    fn a_name_longer_than_the_cap_is_refused() {
        assert_eq!(project_name(&"x".repeat(7), 7).unwrap(), "x".repeat(7));
        assert_eq!(
            project_name(&"x".repeat(8), 7).unwrap_err().to_string(),
            "project name is longer than 7 characters"
        );
        // the cap counts characters, and only what is left after the line is folded
        let wide = "\u{9879}".repeat(7);
        assert_eq!(project_name(&wide, 7).unwrap(), wide);
        assert!(project_name(&"\u{9879}".repeat(8), 7).is_err());
        assert_eq!(project_name("  a\n\n\nb    c ", 5).unwrap(), "a b c");
    }

    #[test]
    fn names_are_not_identities() {
        assert_ne!(
            Project::new("same", 120).unwrap().id,
            Project::new("same", 120).unwrap().id
        );
    }
}
