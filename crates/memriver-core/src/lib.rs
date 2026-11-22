//! Every data rule of memriver: models, the store, projects, sessions, content policy.

pub mod application;
pub mod content_policy;
pub mod models;
pub mod repository;
pub mod settings;

use std::path::Path;

use application::memory::{MemoryLimits, MemoryService, policy_source};
use application::projects::ProjectService;
use content_policy::ContentClassifier;
use repository::sqlite::memory_store::MemoryStore;
use repository::sqlite::project_store::ProjectStore;
use settings::{HEADER_FIELD_CHARS, PROJECT_NAME_MAX_CHARS, Settings};

/// The services over one store, one settings object and one content policy.
pub struct Services {
    pub memory: MemoryService,
    pub project: ProjectService,
}

/// The services over the store at `settings.root`. `classifier` is the optional
/// content classifier the caller built (None: none); this crate never builds one.
pub fn build_services(
    settings: &Settings,
    home: &Path,
    classifier: Option<Box<dyn ContentClassifier>>,
) -> Services {
    let root = &settings.root;
    let memory = MemoryService::new(
        MemoryStore::new(root),
        ProjectStore::new(root, home),
        // one policy per composition, loaded on first use
        policy_source(),
        MemoryLimits::of(settings),
        classifier,
        // no session is known to these services yet: nothing is pending, and there is
        // no watermark to move
        Box::new(|_| Ok(())),
        Box::new(|_| {}),
    );
    Services {
        memory,
        project: ProjectService::new(
            ProjectStore::new(root, home),
            HEADER_FIELD_CHARS,
            PROJECT_NAME_MAX_CHARS,
        ),
    }
}
