//! What the store inspector and the diagnostics built on it report.

use super::errors::named;
use super::memory::Memory;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InspectedMemory {
    pub memory: Memory,
    pub location_hint: String,
}

named! {
    RootState {
        Ok = "ok",
        Missing = "missing",
        NotCanonical = "not-canonical",
        Unverifiable = "unverifiable",
        Unbound = "unbound",
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InspectedProject {
    pub id: String,
    pub name: String,
    pub root: Option<String>,
    pub is_global: bool,
    pub root_state: RootState,
    pub active_memories: i64,
    pub deleted_memories: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoreFinding {
    pub kind: String,
    pub project_id: Option<String>,
    pub location_hint: String,
    pub memory_id: Option<String>,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoreReport {
    pub initialized: bool,
    pub entries: Vec<InspectedMemory>,
    pub projects: Vec<InspectedProject>,
    pub findings: Vec<StoreFinding>,
    /// Changes with fewer stored steps than they recorded: listed, never a finding.
    pub incomplete_changes: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiagnosticFinding {
    pub kind: String,
    pub memory_ids: Vec<String>,
    pub project_ids: Vec<String>,
    pub location_hints: Vec<String>,
    pub reason: String,
    pub suggestion: String,
}

named! {
    DiagnosticsState {
        Uninitialized = "uninitialized",
        Empty = "empty",
        Healthy = "healthy",
        Degraded = "degraded",
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiagnosticsReport {
    pub state: DiagnosticsState,
    pub findings: Vec<DiagnosticFinding>,
    /// A finding outranks "uninitialized" in `state`, so whether the global project
    /// exists is carried on its own.
    pub initialized: bool,
    /// The inspector's projects, passed through so doctor renders them from the one
    /// diagnostics entry.
    pub projects: Vec<InspectedProject>,
    /// The inspector's incomplete changes (not undoable), passed through for doctor.
    pub incomplete_changes: Vec<String>,
}
