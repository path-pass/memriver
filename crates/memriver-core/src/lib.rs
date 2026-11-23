//! Every data rule of memriver: models, the store, projects, sessions, content policy.

pub mod application;
pub mod content_policy;
pub mod models;
pub mod repository;
pub mod settings;

use std::path::Path;
use std::sync::Arc;

use application::maintenance::MaintenanceService;
use application::memory::{MemoryLimits, MemoryService, policy_source};
use application::projects::ProjectService;
use application::sessions::SessionService;
use content_policy::ContentClassifier;
use repository::sqlite::inspector::StoreInspector;
use repository::sqlite::memory_store::MemoryStore;
use repository::sqlite::project_store::ProjectStore;
use repository::sqlite::session_store::SessionStore;
use settings::{Env, HEADER_FIELD_CHARS, PROJECT_NAME_MAX_CHARS, Settings};

/// The services over one store, one settings object and one content policy.
pub struct Services {
    pub memory: MemoryService,
    pub project: ProjectService,
    /// Shared with the memory service, whose writes ask it about their session.
    pub session: Arc<SessionService>,
    /// The checks and repairs of the human commands and dream; no agent path reaches it.
    pub maintenance: MaintenanceService,
}

/// The services over the store at `settings.root`. `classifier` is the optional
/// content classifier the caller built (None: none); this crate never builds one. `env`
/// is what git is run with when a session's directory has to be asked about.
pub fn build_services(
    settings: &Settings,
    home: &Path,
    env: &Env,
    classifier: Option<Box<dyn ContentClassifier>>,
) -> Services {
    let root = &settings.root;
    // one policy per composition, loaded on first use and shared by the three services
    // that check text
    let policy = policy_source();
    let session = Arc::new(SessionService::new(
        SessionStore::new(root),
        ProjectStore::new(root, home),
        Arc::clone(&policy),
        env.clone(),
    ));
    let maintenance = MaintenanceService::new(
        StoreInspector::new(root),
        MemoryStore::new(root),
        Arc::clone(&policy),
    );
    let (refusing, marking) = (Arc::clone(&session), Arc::clone(&session));
    let memory = MemoryService::new(
        MemoryStore::new(root),
        ProjectStore::new(root, home),
        policy,
        MemoryLimits::of(settings),
        classifier,
        Box::new(move |context| refusing.refuse_pending(context)),
        Box::new(move |context| marking.mark_saved(context)),
    );
    Services {
        memory,
        session,
        maintenance,
        project: ProjectService::new(
            ProjectStore::new(root, home),
            HEADER_FIELD_CHARS,
            PROJECT_NAME_MAX_CHARS,
        ),
    }
}
