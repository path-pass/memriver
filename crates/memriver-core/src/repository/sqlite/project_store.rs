//! Projects over the SQLite database: the collection, the global project, and which
//! directory belongs to which project.
//!
//! A binding is planned, shown to the user and then written. The plan pins what was
//! shown -- the directory and the store, both by their real paths -- and the write
//! checks it again as a precondition: a store or a directory that changed while the
//! prompt was open is refused, never re-planned.

use std::path::{Path, PathBuf};

use rusqlite::types::Value;
use rusqlite::{Connection, ErrorCode, OptionalExtension};

use super::database::{Database, PROJECT_COLUMNS, project_from_row, values};
use crate::models::context::{Resolution, ResolutionState};
use crate::models::errors::{BindingReason, CoreError};
use crate::models::helpers::{is_id, new_id};
use crate::models::project::{Project, RootPlan, UnbindPlan};
use crate::repository::directories::{self, Match};
use crate::settings::{BUSY_TIMEOUT_MS, located_root};

pub const GLOBAL_PROJECT_NAME: &str = "global";

fn refused(reason: BindingReason) -> CoreError {
    CoreError::BindingRefused {
        reason,
        project_id: None,
    }
}

fn project(row: &[Value]) -> Result<(Project, bool), CoreError> {
    project_from_row(row).map_err(|_| CoreError::StorageFailure)
}

/// The row of one project, None when there is none.
fn row(conn: &Connection, condition: &str, id: &str) -> Result<Option<Vec<Value>>, CoreError> {
    let sql = format!("SELECT {PROJECT_COLUMNS} FROM projects WHERE {condition}");
    Ok(conn.query_row(&sql, [id], values).optional()?)
}

fn global_row(conn: &Connection) -> Result<Option<Vec<Value>>, CoreError> {
    let sql = format!("SELECT {PROJECT_COLUMNS} FROM projects WHERE is_global = 1");
    Ok(conn.query_row(&sql, [], values).optional()?)
}

/// Every project that holds a directory; one invalid row fails the whole answer.
///
/// A partly read set of bindings could hide the nearer project, so there is no skipping
/// here.
fn bound(conn: Option<&Connection>) -> Result<Vec<Project>, CoreError> {
    let Some(conn) = conn else {
        return Ok(Vec::new());
    };
    let sql = format!("SELECT {PROJECT_COLUMNS} FROM projects WHERE root IS NOT NULL ORDER BY id");
    let rows: Vec<Vec<Value>> = conn
        .prepare(&sql)?
        .query_map([], values)?
        .collect::<Result<_, _>>()?;
    rows.iter().map(|row| Ok(project(row)?.0)).collect()
}

/// A project's root, which every bound project has.
fn root_of(project: &Project) -> &str {
    project.root.as_deref().unwrap_or_default()
}

/// After a constraint stopped the write of `root`: "bound elsewhere" if a row now holds
/// it, a storage failure for any other constraint.
///
/// An owner id that is not text, or not an id, is damage, not a binding to name.
fn bound_elsewhere(conn: &Connection, root: &str) -> CoreError {
    let owner = conn
        .query_row("SELECT id FROM projects WHERE root = ?1", [root], values)
        .optional();
    match owner.as_ref().map(|row| row.as_deref()) {
        Ok(Some([Value::Text(owner)])) if is_id(owner) => CoreError::BindingRefused {
            reason: BindingReason::BoundElsewhere,
            project_id: Some(owner.clone()),
        },
        _ => CoreError::StorageFailure,
    }
}

/// A write that a constraint may stop because a peer bound `root` in the meantime.
fn write_root(
    conn: &Connection,
    sql: &str,
    params: impl rusqlite::Params,
    root: &str,
) -> Result<(), CoreError> {
    match conn.execute(sql, params) {
        Ok(_) => Ok(()),
        Err(rusqlite::Error::SqliteFailure(failure, _))
            if failure.code == ErrorCode::ConstraintViolation =>
        {
            Err(bound_elsewhere(conn, root))
        }
        Err(_) => Err(CoreError::StorageFailure),
    }
}

fn same(a: &str, b: &str) -> Result<bool, CoreError> {
    if a == b {
        return Ok(true);
    }
    directories::same_directory(Path::new(a), Path::new(b))
        .ok_or(refused(BindingReason::Unverifiable))
}

fn covers(outer: &Path, inner: &Path, reason: BindingReason) -> Result<(), CoreError> {
    match directories::covers(outer, inner) {
        None => Err(refused(BindingReason::Unverifiable)),
        Some(true) => Err(refused(reason)),
        Some(false) => Ok(()),
    }
}

pub struct ProjectStore {
    root: PathBuf,
    home: PathBuf,
    busy_timeout_ms: u64,
}

impl ProjectStore {
    pub fn new(root: &Path, home: &Path) -> ProjectStore {
        ProjectStore::with_busy_timeout(root, home, BUSY_TIMEOUT_MS)
    }

    /// For a test that must not wait the whole `BUSY_TIMEOUT_MS` for a lock.
    pub fn with_busy_timeout(root: &Path, home: &Path, busy_timeout_ms: u64) -> ProjectStore {
        ProjectStore {
            root: root.to_path_buf(),
            home: home.to_path_buf(),
            busy_timeout_ms,
        }
    }

    fn database(&self, root: &Path) -> Database {
        Database::with_busy_timeout(root, self.busy_timeout_ms)
    }

    // --- collections ---

    /// The project and its directory in one transaction, under the confirmed plan; a
    /// taken id is a storage failure, and nothing is written on any refusal.
    pub fn create(&self, project: &Project, plan: &RootPlan) -> Result<(), CoreError> {
        if !is_id(&project.id) {
            return Err(CoreError::StorageFailure);
        }
        // the filesystem-only checks first, so a refused plan creates no store
        self.check_confirmed(None, plan, None)?;
        self.pinned_write(&plan.store, None, |conn| {
            self.check_confirmed(Some(conn), plan, None)?;
            if row(conn, "id = ?1", &project.id)?.is_some() {
                return Err(CoreError::StorageFailure);
            }
            // a row the read path would reject is never committed: the same decoder that
            // would refuse it on the next read refuses it now
            self::project(&[
                Value::Text(project.id.clone()),
                Value::Text(project.name.clone()),
                Value::Text(plan.root.clone()),
                Value::Integer(0),
            ])?;
            write_root(
                conn,
                "INSERT INTO projects (id, name, root, is_global) VALUES (?1, ?2, ?3, 0)",
                (&project.id, &project.name, &plan.root),
                &plan.root,
            )
        })
    }

    /// Malformed or absent is "not found"; an invalid row is a storage failure.
    pub fn read(&self, project_id: &str) -> Result<Project, CoreError> {
        let not_found = || CoreError::ProjectNotFound(project_id.to_string());
        if !is_id(project_id) {
            return Err(not_found());
        }
        let found = self.database(&self.root).read(|conn| match conn {
            Some(conn) => row(conn, "id = ?1", project_id),
            None => Ok(None),
        })?;
        Ok(project(&found.ok_or_else(not_found)?)?.0)
    }

    /// Every project, global last. A bulk scan skips an invalid row; doctor reports it.
    pub fn list(&self) -> Result<Vec<Project>, CoreError> {
        let sql = format!("SELECT {PROJECT_COLUMNS} FROM projects ORDER BY is_global, name, id");
        let rows: Vec<Vec<Value>> = self.database(&self.root).read(|conn| match conn {
            Some(conn) => Ok(conn
                .prepare(&sql)?
                .query_map([], values)?
                .collect::<Result<_, _>>()?),
            None => Ok(Vec::new()),
        })?;
        Ok(rows
            .iter()
            .filter_map(|row| project_from_row(row).ok())
            .map(|(project, _)| project)
            .collect())
    }

    /// None for a store that has no global project yet.
    pub fn global_project_id(&self) -> Result<Option<String>, CoreError> {
        let found = self.database(&self.root).read(|conn| match conn {
            Some(conn) => global_row(conn),
            None => Ok(None),
        })?;
        found.map(|row| Ok(project(&row)?.0.id)).transpose()
    }

    /// The global project's id, made when there is none: one transaction, never touches
    /// a memory.
    pub fn ensure_global(&self) -> Result<String, CoreError> {
        self.database(&self.root).write(true, |conn| {
            if let Some(found) = global_row(conn)? {
                return Ok(project(&found)?.0.id);
            }
            let project_id = new_id();
            if row(conn, "id = ?1", &project_id)?.is_some() {
                return Err(CoreError::StorageFailure);
            }
            conn.execute(
                "INSERT INTO projects (id, name, root, is_global) VALUES (?1, ?2, NULL, 1)",
                (&project_id, GLOBAL_PROJECT_NAME),
            )?;
            Ok(project_id)
        })
    }

    // --- directories ---

    /// Which project `start` belongs to. `ignoring` (a project id and its root) previews
    /// the answer without that binding; `logical` walks that path's ancestors instead of
    /// `start`'s (a linked worktree mapped onto its main tree), `start` still having to be
    /// a real directory.
    pub fn resolve(
        &self,
        start: &Path,
        ignoring: Option<(&str, &str)>,
        logical: Option<&Path>,
    ) -> Result<Resolution, CoreError> {
        let mut bound = self.database(&self.root).read(bound)?;
        if let Some(ignored) = ignoring {
            bound.retain(|project| (project.id.as_str(), root_of(project)) != ignored);
        }
        let pairs: Vec<(String, String)> = bound
            .iter()
            .map(|project| (project.id.clone(), root_of(project).to_string()))
            .collect();
        let resolution = |state, project, diagnostic| Resolution {
            state,
            project,
            diagnostic,
        };
        Ok(match directories::nearest_bound(start, &pairs, logical) {
            Match::Registered(project_id) => resolution(
                ResolutionState::Registered,
                bound.into_iter().find(|project| project.id == project_id),
                None,
            ),
            Match::None => resolution(ResolutionState::None, None, None),
            Match::Degraded(diagnostic) => {
                resolution(ResolutionState::Degraded, None, Some(diagnostic))
            }
        })
    }

    /// `directory` checked for binding -- to a new project, or to the existing
    /// `project_id` -- as it is shown to the user before confirmation.
    pub fn plan_root(
        &self,
        directory: &Path,
        project_id: Option<&str>,
    ) -> Result<RootPlan, CoreError> {
        let (project, bound) = self.database(&self.root).read(|conn| {
            let project = match project_id {
                Some(project_id) => Some(adoptable(conn, project_id)?),
                None => None,
            };
            Ok((project, bound(conn)?))
        })?;
        let canonical = directories::canonical(directory)
            .and_then(|canonical| canonical.to_str().map(str::to_string))
            .ok_or(refused(BindingReason::NotADirectory))?;
        let store = self.canonical_store()?;
        self.refuse(&canonical, &store, &bound, project_id)?;
        let mut already_bound = false;
        if let Some(held) = project.as_ref().and_then(|project| project.root.as_deref()) {
            if !same(held, &canonical)? {
                return Err(refused(BindingReason::HasDirectory));
            }
            already_bound = true;
        }
        let mut nested = Vec::new();
        for other in bound {
            if Some(other.id.as_str()) == project_id {
                continue;
            }
            let inside = directories::covers(Path::new(&canonical), Path::new(root_of(&other)))
                .ok_or(refused(BindingReason::Unverifiable))?;
            if inside && !same(&canonical, root_of(&other))? {
                nested.push(other);
            }
        }
        Ok(RootPlan {
            root: canonical,
            store,
            nested,
            already_bound,
        })
    }

    /// Bind the existing `project_id` to the planned directory. A project that already
    /// holds that directory is left as it is.
    pub fn bind(&self, project_id: &str, plan: &RootPlan) -> Result<(), CoreError> {
        if !is_id(project_id) {
            return Err(refused(BindingReason::NoSuchProject));
        }
        self.pinned_write(&plan.store, Some(BindingReason::NoSuchProject), |conn| {
            let project = adoptable(Some(conn), project_id)?;
            self.check_confirmed(Some(conn), plan, Some(project_id))?;
            // decided on the current row, never on what the plan said about it
            if let Some(held) = &project.root {
                return match same(held, &plan.root)? {
                    true => Ok(()),
                    false => Err(refused(BindingReason::HasDirectory)),
                };
            }
            // only the root is written; the full row is validated as it will stand
            self::project(&[
                Value::Text(project.id.clone()),
                Value::Text(project.name.clone()),
                Value::Text(plan.root.clone()),
                Value::Integer(0),
            ])?;
            write_root(
                conn,
                "UPDATE projects SET root = ?1 WHERE id = ?2 AND root IS NULL",
                (&plan.root, project_id),
                &plan.root,
            )
        })
    }

    /// The binding of `project_id` to `root` as it would be removed, and what `cwd`
    /// resolves to without it.
    pub fn plan_unbind(
        &self,
        project_id: &str,
        root: &str,
        cwd: &Path,
    ) -> Result<(UnbindPlan, Resolution), CoreError> {
        if !is_id(project_id) {
            return Err(refused(BindingReason::NoSuchProject));
        }
        let store = self.canonical_store()?;
        let found = self.database(&self.root).read(|conn| match conn {
            Some(conn) => row(conn, "id = ?1", project_id),
            None => Ok(None),
        })?;
        let found = found.ok_or(refused(BindingReason::NoSuchProject))?;
        let stored = project(&found)?
            .0
            .root
            .filter(|stored| names_root(root, stored))
            .ok_or(refused(BindingReason::BindingChanged))?;
        let resolution = self.resolve(cwd, Some((project_id, &stored)), None)?;
        let plan = UnbindPlan {
            project_id: project_id.to_string(),
            root: stored,
            store,
        };
        Ok((plan, resolution))
    }

    /// Remove exactly the binding the plan showed.
    pub fn unbind(&self, plan: &UnbindPlan) -> Result<(), CoreError> {
        self.pinned_write(&plan.store, Some(BindingReason::BindingChanged), |conn| {
            let changed = conn.execute(
                "UPDATE projects SET root = NULL WHERE id = ?1 AND root = ?2",
                (&plan.project_id, &plan.root),
            )?;
            match changed {
                1 => Ok(()),
                _ => Err(refused(BindingReason::BindingChanged)),
            }
        })
    }

    // --- helpers ---

    /// The store's real path, links read as far as it exists: it may not be there yet
    /// when the first project is planned.
    fn canonical_store(&self) -> Result<String, CoreError> {
        directories::realpath(&located_root(&self.root), false)
            .ok()
            .and_then(|store| store.to_str().map(str::to_string))
            .ok_or(CoreError::StorageFailure)
    }

    fn canonical_home(&self) -> PathBuf {
        directories::realpath(&self.home, false).unwrap_or_else(|_| self.home.clone())
    }

    /// A write transaction on exactly the store a confirmed plan was made against.
    ///
    /// Checked before anything is opened or created, so a store redirected during the
    /// prompt to a place with no database gets no database. `missing` is the refusal
    /// for an operation that cannot succeed without an existing database (bind, unbind):
    /// it never creates one.
    fn pinned_write<T>(
        &self,
        store: &str,
        missing: Option<BindingReason>,
        body: impl FnOnce(&Connection) -> Result<T, CoreError>,
    ) -> Result<T, CoreError> {
        if self.canonical_store()? != store {
            return Err(refused(BindingReason::PlanChanged));
        }
        let database = self.database(Path::new(store));
        if let Some(missing) = missing
            && !database.exists()?
        {
            return Err(refused(missing));
        }
        database.write(true, body)
    }

    /// The confirmed target is a precondition, not planned again. Without a connection
    /// only the filesystem checks run: there are no bindings to compare.
    fn check_confirmed(
        &self,
        conn: Option<&Connection>,
        plan: &RootPlan,
        project_id: Option<&str>,
    ) -> Result<(), CoreError> {
        let canonical = directories::canonical(Path::new(&plan.root));
        if canonical.as_deref().map(Path::as_os_str) != Some(plan.root.as_ref()) {
            return Err(refused(BindingReason::PlanChanged));
        }
        self.refuse(&plan.root, &plan.store, &bound(conn)?, project_id)
    }

    fn refuse(
        &self,
        root: &str,
        store: &str,
        bound: &[Project],
        project_id: Option<&str>,
    ) -> Result<(), CoreError> {
        let (root_path, store_path) = (Path::new(root), Path::new(store));
        covers(root_path, &self.canonical_home(), BindingReason::CoversHome)?;
        covers(root_path, store_path, BindingReason::CoversStore)?;
        covers(store_path, root_path, BindingReason::InsideStore)?;
        for other in bound {
            if Some(other.id.as_str()) != project_id && same(root, root_of(other))? {
                return Err(CoreError::BindingRefused {
                    reason: BindingReason::BoundElsewhere,
                    project_id: Some(other.id.clone()),
                });
            }
        }
        Ok(())
    }
}

/// The project a directory may be bound to: it exists and is not the global one.
fn adoptable(conn: Option<&Connection>, project_id: &str) -> Result<Project, CoreError> {
    let no_such = || refused(BindingReason::NoSuchProject);
    let conn = conn.filter(|_| is_id(project_id)).ok_or_else(no_such)?;
    let found = row(conn, "id = ?1", project_id)?.ok_or_else(no_such)?;
    match project(&found)? {
        (_, true) => Err(refused(BindingReason::IsGlobal)),
        (project, false) => Ok(project),
    }
}

/// Whether `given` names the stored root: by its spelling first, then by its strict real
/// path; the path need not exist.
fn names_root(given: &str, stored: &str) -> bool {
    given == stored
        || directories::realpath(Path::new(given), true)
            .is_ok_and(|real| real.as_os_str() == stored)
}

#[cfg(test)]
mod tests {
    use memriver_test_support::Sandbox;

    use super::*;

    const A: &str = "aaaaaaaaaa";
    const B: &str = "bbbbbbbbbb";

    /// A store with two projects, the first bound to `/held`.
    fn store() -> (Sandbox, Database) {
        let sandbox = Sandbox::new();
        let database = Database::new(&sandbox.root);
        database
            .write(true, |conn| {
                conn.execute_batch(&format!(
                    "INSERT INTO projects (id, name, root, is_global) VALUES
                     ('{A}', 'a', '/held', 0), ('{B}', 'b', NULL, 0)"
                ))?;
                Ok(())
            })
            .unwrap();
        (sandbox, database)
    }

    fn bound_elsewhere_to(project_id: &str) -> CoreError {
        CoreError::BindingRefused {
            reason: BindingReason::BoundElsewhere,
            project_id: Some(project_id.to_string()),
        }
    }

    #[test]
    fn a_root_a_constraint_refused_names_the_project_that_holds_it() {
        let (_sandbox, database) = store();
        let sql = "UPDATE projects SET root = ?1 WHERE id = ?2";
        // the column is unique: the second project cannot take the first one's root
        let taken = database.write(false, |conn| write_root(conn, sql, ("/held", B), "/held"));
        assert_eq!(taken, Err(bound_elsewhere_to(A)));
        // another constraint is a storage failure, with no project to name
        let unnamed = "UPDATE projects SET name = ?1 WHERE id = ?2";
        let other = database.write(false, |conn| write_root(conn, unnamed, ("", B), "/nobody"));
        assert_eq!(other, Err(CoreError::StorageFailure));
        let free = database.write(false, |conn| write_root(conn, sql, ("/free", B), "/free"));
        assert_eq!(free, Ok(()));
        // a statement that is wrong altogether
        let wrong = database.write(false, |conn| write_root(conn, "UPDATE nothing", [], "/x"));
        assert_eq!(wrong, Err(CoreError::StorageFailure));
    }

    #[test]
    fn an_undecodable_binding_owner_id_is_damage_not_a_bound_elsewhere_refusal() {
        let (_sandbox, database) = store();
        let asked =
            |root: &'static str| database.write(false, |conn| Ok(bound_elsewhere(conn, root)));
        assert_eq!(asked("/held"), Ok(bound_elsewhere_to(A)));
        assert_eq!(asked("/nobody"), Ok(CoreError::StorageFailure));
        // an outside writer can plant an id that is not text, or not an id
        for planted in ["CAST(X'80' AS TEXT)", "'../../evil'", "''"] {
            database
                .write(false, |conn| {
                    conn.execute_batch(&format!(
                        "PRAGMA ignore_check_constraints = ON;
                         UPDATE projects SET id = {planted} WHERE root = '/held'"
                    ))?;
                    Ok(())
                })
                .unwrap();
            assert_eq!(asked("/held"), Ok(CoreError::StorageFailure), "{planted}");
        }
    }
}
