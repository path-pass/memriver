//! The project service: projects and global, bindings, and the context one directory
//! grants.
//!
//! The project store owns collections and directories; this service creates, reads,
//! lists, binds and unbinds projects and builds the context a directory answers in.
//! Nothing here touches a file, a table or the settings: every limit is handed in.

use std::fmt;
use std::path::Path;

use super::contexts::{
    NONE_HEADER, degraded_context, no_project_context, registered_context, unavailable_context,
};
use crate::models::context::{ProjectContext, ProjectContextState, Resolution, ResolutionState};
use crate::models::errors::{CoreError, ValueError};
use crate::models::project::{Project, RootPlan, UnbindPlan};
use crate::repository::sqlite::project_store::ProjectStore;

/// Why a project was not created: its name is not one, or the store refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InitError {
    Name(ValueError),
    Store(CoreError),
}

impl fmt::Display for InitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            InitError::Name(err) => err.fmt(f),
            InitError::Store(err) => err.fmt(f),
        }
    }
}

impl std::error::Error for InitError {}

pub struct ProjectService {
    project_store: ProjectStore,
    header_field_chars: usize,
    project_name_max_chars: usize,
}

impl ProjectService {
    pub fn new(
        project_store: ProjectStore,
        header_field_chars: usize,
        project_name_max_chars: usize,
    ) -> ProjectService {
        ProjectService {
            project_store,
            header_field_chars,
            project_name_max_chars,
        }
    }

    /// The header, state and read/write set for one directory. Never fails for a store
    /// problem: an unreadable store is an empty, clearly labelled project context.
    pub fn open_project_context(&self, start: &Path) -> ProjectContext {
        let read = || {
            let resolution = self.project_store.resolve(start, None, None)?;
            Ok::<_, CoreError>((resolution, self.project_store.global_project_id()?))
        };
        let Ok((resolution, global_project_id)) = read() else {
            return unavailable_context(None);
        };
        match resolution {
            Resolution {
                state: ResolutionState::Registered,
                project: Some(project),
                ..
            } if Some(&project.id) != global_project_id.as_ref() => {
                registered_context(project, global_project_id, self.header_field_chars, None)
            }
            Resolution {
                state: ResolutionState::Degraded,
                diagnostic,
                ..
            } => degraded_context(
                diagnostic.as_deref().unwrap_or_default(),
                global_project_id,
                self.header_field_chars,
                None,
            ),
            _ => no_project_context(
                ProjectContextState::None,
                NONE_HEADER,
                global_project_id,
                None,
            ),
        }
    }

    pub fn global_project_id(&self) -> Result<Option<String>, CoreError> {
        self.project_store.global_project_id()
    }

    pub fn ensure_global(&self) -> Result<String, CoreError> {
        self.project_store.ensure_global()
    }

    pub fn read_project(&self, project_id: &str) -> Result<Project, CoreError> {
        self.project_store.read(project_id)
    }

    pub fn list_projects(&self) -> Result<Vec<Project>, CoreError> {
        self.project_store.list()
    }

    pub fn plan_root(
        &self,
        directory: &Path,
        project_id: Option<&str>,
    ) -> Result<RootPlan, CoreError> {
        self.project_store.plan_root(directory, project_id)
    }

    /// A new project named `name`, bound to the planned directory.
    pub fn init_project(&self, name: &str, plan: &RootPlan) -> Result<Project, InitError> {
        let project = Project::new(name, self.project_name_max_chars).map_err(InitError::Name)?;
        self.project_store
            .create(&project, plan)
            .map_err(InitError::Store)?;
        Ok(Project {
            root: Some(plan.root.clone()),
            ..project
        })
    }

    pub fn adopt(&self, project_id: &str, plan: &RootPlan) -> Result<(), CoreError> {
        self.project_store.bind(project_id, plan)
    }

    pub fn plan_unbind(
        &self,
        project_id: &str,
        root: &str,
        cwd: &Path,
    ) -> Result<(UnbindPlan, Resolution), CoreError> {
        self.project_store.plan_unbind(project_id, root, cwd)
    }

    pub fn unbind(&self, plan: &UnbindPlan) -> Result<(), CoreError> {
        self.project_store.unbind(plan)
    }
}
