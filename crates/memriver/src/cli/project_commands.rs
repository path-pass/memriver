//! `memriver project`: plan, confirm and report directory bindings.
//!
//! Every command shows its plan and asks before writing; `explain` writes nothing. None
//! of them ever creates or changes a file inside a target directory. Every rule and
//! every write is the core's; this module owns the plan text and the sentences.

use std::path::{Path, PathBuf};

use memriver_core::application::projects::{InitError, ProjectService};
use memriver_core::models::context::{ProjectContextState, ResolutionState};
use memriver_core::models::errors::{BindingReason, CoreError};
use memriver_core::models::project::{RootPlan, project_name};
use memriver_core::repository::directories::{realpath, tidied};
use memriver_core::settings::PROJECT_NAME_MAX_CHARS;

use super::views::{echoed, visible};
use super::{Invocation, Outcome, Stop, confirm, services};

const RESTART_NOTE: &str = "Restart the affected harness sessions and their memriver MCP \
                            servers for the change to take effect.";
const SUBTREE_NOTE: &str = "Every directory under this root -- including repositories added \
                            later -- will share this project's memories.";
const INDEPENDENT_NOTE: &str = "These registered sub-projects stay independent:";
const OUT_OF_DATE: &str =
    "refused: the plan is out of date (a path changed while waiting); run the command again";
const STORE_FAILURE: &str =
    "refused: could not complete the store write; inspect with memriver project explain";
const CANNOT_VERIFY: &str = "refused: could not verify the directory's relation to the home \
                             directory, the memory store or the bound directories";
const GLOBAL_REFUSAL: &str = "refused: the global project cannot be bound to a directory";
const BINDING_CHANGED: &str =
    "refused: the binding changed while waiting; nothing was changed; run the command again";
const DECLINED: &str = "aborted; nothing was written";

/// The project service over the store, and the store's path for display: with its links
/// read where that can be done, the store need not exist yet.
fn opened(root: Option<&str>, inv: &Invocation) -> Result<(ProjectService, String), Stop> {
    let (services, store) = services(root, inv)?;
    let shown = realpath(&store, false).unwrap_or(store);
    Ok((services.project, shown.to_string_lossy().into_owned()))
}

/// A directory a user gave, from the working directory; its links are not read here.
fn absolute(directory: Option<&str>, cwd: &Path) -> PathBuf {
    tidied(&cwd.join(directory.unwrap_or_default()))
}

fn shown(path: &Path) -> String {
    visible(&path.to_string_lossy())
}

/// The sentence for one refusal; `target` and `store` are what the user typed or saw.
fn refusal(reason: BindingReason, project_id: Option<&str>, target: &str, store: &str) -> String {
    let (target, store) = (visible(target), visible(store));
    match reason {
        BindingReason::NotADirectory => format!("refused: {target} is not an existing directory"),
        BindingReason::CoversHome => "refused: the filesystem root, the home directory and its \
                                      ancestors cannot be a project"
            .to_string(),
        BindingReason::CoversStore => format!(
            "refused: the memory store {store} lies inside this directory; the store would end \
             up inside the project"
        ),
        BindingReason::InsideStore => {
            format!("refused: {target} lies inside the memory store {store}")
        }
        BindingReason::BoundElsewhere => format!(
            "refused: {target} is already bound to project {}",
            project_id.unwrap_or("None")
        ),
        BindingReason::Unverifiable => CANNOT_VERIFY.to_string(),
        BindingReason::IsGlobal => GLOBAL_REFUSAL.to_string(),
        BindingReason::PlanChanged => OUT_OF_DATE.to_string(),
        BindingReason::BindingChanged => BINDING_CHANGED.to_string(),
        // the callers word these two themselves
        BindingReason::NoSuchProject | BindingReason::HasDirectory => {
            "refused: the project could not be bound".to_string()
        }
    }
}

fn no_such_project(raw: &str, inv: &mut Invocation) -> Outcome {
    inv.say(format_args!("no such project: {}\n", echoed(raw)));
    Ok(2)
}

fn store_failure(inv: &mut Invocation) -> Outcome {
    inv.say(format_args!("{STORE_FAILURE}\n"));
    Ok(2)
}

/// Direct children holding a `.git` entry of any kind; None when they cannot be listed.
pub(crate) fn count_child_git_markers(directory: &Path) -> Option<usize> {
    let mut count = 0;
    for child in std::fs::read_dir(directory).ok()? {
        let child = child.ok()?;
        // a link to a directory is no child directory
        let is_directory = match child.file_type() {
            Ok(kind) => kind.is_dir(),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
            Err(_) => return None,
        };
        if is_directory && child.path().join(".git").symlink_metadata().is_ok() {
            count += 1;
        }
    }
    Some(count)
}

/// The lines of a plan that are the same for `init` and `adopt`.
fn binding_plan(command: &str, project: &str, plan: &RootPlan) -> String {
    let count = count_child_git_markers(Path::new(&plan.root)).map_or_else(
        || "could not be counted".to_string(),
        |count| count.to_string(),
    );
    let mut text = format!(
        "memriver project {command}\n  store:   {}\n  project: {project}\n  root:    {}\n  \
         {SUBTREE_NOTE}\n  Direct child directories containing a .git marker: {count}\n",
        visible(&plan.store),
        visible(&plan.root)
    );
    if !plan.nested.is_empty() {
        text.push_str(&format!("  {INDEPENDENT_NOTE}\n"));
    }
    for nested in &plan.nested {
        let root = visible(nested.root.as_deref().unwrap_or_default());
        text.push_str(&format!("    {}: {root}\n", nested.id));
    }
    text
}

/// Shows the plan, then asks. None to go ahead, else the exit code.
fn confirmed(plan: &str, yes: bool, inv: &mut Invocation) -> Option<i32> {
    inv.say(format_args!("{plan}"));
    confirm(inv.io, yes, DECLINED).refused()
}

pub(crate) fn init(
    directory: Option<&str>,
    name: Option<&str>,
    root: Option<&str>,
    yes: bool,
    inv: &mut Invocation,
) -> Outcome {
    let (projects, store) = opened(root, inv)?;
    let target = absolute(directory, inv.cwd);
    let plan = match projects.plan_root(&target, None) {
        Ok(plan) => plan,
        Err(CoreError::BindingRefused { reason, project_id }) => {
            let target = target.to_string_lossy();
            let line = refusal(reason, project_id.as_deref(), &target, &store);
            inv.say(format_args!("{line}\n"));
            return Ok(2);
        }
        Err(CoreError::StorageFailure) => return store_failure(inv),
        Err(other) => return Err(other.into()),
    };
    let last = Path::new(&plan.root).file_name();
    let given = name.unwrap_or_else(|| last.and_then(|name| name.to_str()).unwrap_or_default());
    let display = match project_name(given, PROJECT_NAME_MAX_CHARS) {
        Ok(display) => display,
        Err(error) => {
            inv.say(format_args!("refused: {error}\n"));
            return Ok(2);
        }
    };
    let project = format!("{}  (new; id assigned when written)", visible(&display));
    if let Some(code) = confirmed(&binding_plan("init", &project, &plan), yes, inv) {
        return Ok(code);
    }
    // one transaction: the project and its directory, under the confirmed plan
    let project = match projects.init_project(&display, &plan) {
        Ok(project) => project,
        Err(InitError::Store(CoreError::BindingRefused { reason, project_id })) => {
            let line = refusal(reason, project_id.as_deref(), &plan.root, &plan.store);
            inv.say(format_args!("{line}\n"));
            return Ok(2);
        }
        Err(InitError::Store(CoreError::StorageFailure)) => return store_failure(inv),
        Err(InitError::Store(other)) => return Err(other.into()),
        Err(InitError::Name(_)) => return Err(Stop::Unexpected("invalid-name")),
    };
    inv.say(format_args!(
        "created project {} [{}] and bound {}\n{RESTART_NOTE}\n",
        visible(&project.name),
        project.id,
        visible(&plan.root)
    ));
    Ok(0)
}

pub(crate) fn adopt(
    project_id: &str,
    directory: Option<&str>,
    root: Option<&str>,
    yes: bool,
    inv: &mut Invocation,
) -> Outcome {
    let (projects, store) = opened(root, inv)?;
    let target = absolute(directory, inv.cwd);
    let read = || -> Result<_, CoreError> {
        let plan = projects.plan_root(&target, Some(project_id))?;
        Ok((plan, projects.read_project(project_id)?))
    };
    let (plan, project) = match read() {
        Ok(read) => read,
        Err(CoreError::BindingRefused {
            reason: BindingReason::NoSuchProject,
            ..
        })
        | Err(CoreError::ProjectNotFound(_)) => return no_such_project(project_id, inv),
        Err(CoreError::BindingRefused {
            reason: BindingReason::HasDirectory,
            ..
        }) => {
            // a second read: the project may be gone or the store broken by now
            #[cfg(test)]
            if let Some(meanwhile) = tests::BEFORE_THE_SECOND_READ.take() {
                meanwhile();
            }
            let current = match projects.read_project(project_id) {
                Ok(project) => project.root.unwrap_or_default(),
                Err(CoreError::ProjectNotFound(_)) => return no_such_project(project_id, inv),
                Err(CoreError::StorageFailure) => return store_failure(inv),
                Err(other) => return Err(other.into()),
            };
            inv.say(format_args!(
                "refused: project {project_id} already has a directory ({}); unbind it first\n",
                visible(&current)
            ));
            return Ok(2);
        }
        Err(CoreError::BindingRefused { reason, project_id }) => {
            let target = target.to_string_lossy();
            let line = refusal(reason, project_id.as_deref(), &target, &store);
            inv.say(format_args!("{line}\n"));
            return Ok(2);
        }
        Err(CoreError::StorageFailure) => return store_failure(inv),
        Err(other) => return Err(other.into()),
    };
    if plan.already_bound {
        inv.say(format_args!(
            "{} is already bound to {project_id}; nothing to do\n",
            visible(&plan.root)
        ));
        return Ok(0);
    }
    let shown = format!("{} [{}]  (existing)", visible(&project.name), project.id);
    if let Some(code) = confirmed(&binding_plan("adopt", &shown, &plan), yes, inv) {
        return Ok(code);
    }
    match projects.adopt(project_id, &plan) {
        Ok(()) => {}
        Err(CoreError::BindingRefused {
            reason: BindingReason::NoSuchProject,
            ..
        }) => return no_such_project(project_id, inv),
        Err(CoreError::BindingRefused {
            reason: BindingReason::HasDirectory,
            ..
        }) => {
            inv.say(format_args!(
                "refused: project {project_id} already has a directory; unbind it first\n"
            ));
            return Ok(2);
        }
        Err(CoreError::BindingRefused { reason, project_id }) => {
            let line = refusal(reason, project_id.as_deref(), &plan.root, &plan.store);
            inv.say(format_args!("{line}\n"));
            return Ok(2);
        }
        Err(CoreError::StorageFailure) => return store_failure(inv),
        Err(other) => return Err(other.into()),
    }
    inv.say(format_args!(
        "bound {} to {project_id}\n{RESTART_NOTE}\n",
        visible(&plan.root)
    ));
    Ok(0)
}

pub(crate) fn unbind(
    project_id: &str,
    directory: &str,
    root: Option<&str>,
    yes: bool,
    inv: &mut Invocation,
) -> Outcome {
    let (projects, store) = opened(root, inv)?;
    let literal = absolute(Some(directory), inv.cwd);
    let literal = literal.to_string_lossy();
    let (plan, afterwards) = match projects.plan_unbind(project_id, &literal, inv.cwd) {
        Ok(planned) => planned,
        Err(CoreError::BindingRefused {
            reason,
            project_id: other,
        }) => {
            return match reason {
                BindingReason::NoSuchProject => no_such_project(project_id, inv),
                BindingReason::BindingChanged => {
                    inv.say(format_args!(
                        "refused: {} is not bound to {project_id}\n",
                        visible(&literal)
                    ));
                    Ok(2)
                }
                reason => {
                    let line = refusal(reason, other.as_deref(), &literal, &store);
                    inv.say(format_args!("{line}\n"));
                    Ok(2)
                }
            };
        }
        Err(CoreError::StorageFailure) => return store_failure(inv),
        Err(other) => return Err(other.into()),
    };
    let afterwards = match (&afterwards.state, &afterwards.project) {
        (ResolutionState::Registered, Some(project)) => format!("registered {}", project.id),
        (state, _) => state.to_string(),
    };
    let text = format!(
        "memriver project unbind\n  store:   {}\n  project: {project_id}\n  directory before: \
         {}\n  directory after:  (none)\n  the current directory resolves afterwards: \
         {afterwards}\n",
        visible(&plan.store),
        visible(&plan.root)
    );
    if let Some(code) = confirmed(&text, yes, inv) {
        return Ok(code);
    }
    // exactly the pair the confirmation showed
    match projects.unbind(&plan) {
        Ok(()) => {}
        Err(CoreError::BindingRefused { reason, project_id }) => {
            let line = refusal(reason, project_id.as_deref(), &plan.root, &plan.store);
            inv.say(format_args!("{line}\n"));
            return Ok(2);
        }
        Err(CoreError::StorageFailure) => return store_failure(inv),
        Err(other) => return Err(other.into()),
    }
    inv.say(format_args!(
        "unbound {} from {project_id}\n{RESTART_NOTE}\n",
        visible(&plan.root)
    ));
    Ok(0)
}

/// What a directory resolves to and what its project context may read and write. The
/// rights come from the same context the server and the hooks use, so a damaged store
/// is reported here exactly as it would degrade a session.
pub(crate) fn explain(
    project_dir: Option<&str>,
    root: Option<&str>,
    inv: &mut Invocation,
) -> Outcome {
    let (projects, store) = opened(root, inv)?;
    let start = absolute(project_dir, inv.cwd);
    let canonical = realpath(&start, true).unwrap_or_else(|_| start.clone());
    let context = projects.open_project_context(&start)?;
    let mut lines = vec![
        format!("store: {}", visible(&store)),
        format!("cwd: {}", shown(&canonical)),
        format!("state: {}", context.state),
    ];
    let mut code = 0;
    match (context.state, &context.project) {
        (ProjectContextState::Degraded, _) => {
            let diagnostic = context.diagnostic.as_deref().unwrap_or_default();
            lines.push(format!("diagnostic: {}", visible(diagnostic)));
            code = 1;
        }
        (ProjectContextState::Registered, Some(project)) => lines.extend([
            format!("project: {}", project.id),
            format!("name: {}", visible(&project.name)),
            format!(
                "root: {}",
                visible(project.root.as_deref().unwrap_or_default())
            ),
        ]),
        (ProjectContextState::Unavailable, _) => {
            lines.push("diagnostic: the memory store could not be read".to_string());
            code = 1;
        }
        _ => {}
    }
    let set = &context.read_write_set;
    let mut reads: Vec<&str> = set.project_id.iter().map(String::as_str).collect();
    if set.global_project_id.is_some() {
        reads.push("global");
    }
    let writes: Vec<&str> = set.writable().into_iter().collect();
    let listed = |ids: &[&str]| match ids {
        [] => "none".to_string(),
        ids => ids.join(", "),
    };
    lines.extend([
        format!("reads: {}", listed(&reads)),
        format!("writes: {}", listed(&writes)),
    ]);
    inv.say(format_args!("{}\n", lines.join("\n")));
    Ok(code)
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::fs;

    use memriver_core::repository::directories::DATABASE_FILENAME;
    use memriver_core::settings::{Env, Settings};
    use memriver_test_support::Sandbox;

    use super::*;
    use crate::cli::{Io, run};

    thread_local! {
        /// For a test, on its thread: run once between the refusal that says a project
        /// has a directory and the read that asks which -- nothing a caller can do
        /// reaches that moment.
        pub(super) static BEFORE_THE_SECOND_READ: RefCell<Option<Box<dyn FnOnce()>>> =
            const { RefCell::new(None) };
    }

    /// `project adopt <bound project> <another directory> --yes`, with `meanwhile` run
    /// between adopt's two reads: (exit code, stdout, stderr, the project's id).
    fn adopt_with(meanwhile: impl FnOnce(&Path, &str) + 'static) -> (i32, String, String, String) {
        let sandbox = Sandbox::new();
        let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
        let (home, store) = (tmp.join("home"), tmp.join("mem"));
        let (bound, other) = (tmp.join("bound"), tmp.join("other"));
        for directory in [&home, &bound, &other] {
            fs::create_dir_all(directory).unwrap();
        }
        let store_text = store.to_string_lossy().into_owned();
        let env = Env::from([("MEMRIVER_ROOT".to_string(), store_text)]);
        let services =
            memriver_core::build_services(&Settings::light(store.clone()), &home, &env, None);
        let plan = services.project.plan_root(&bound, None).unwrap();
        let id = services.project.init_project("demo", &plan).unwrap().id;
        let (database, gone) = (store.join(DATABASE_FILENAME), id.clone());
        BEFORE_THE_SECOND_READ.set(Some(Box::new(move || meanwhile(&database, &gone))));
        let argv = [
            "memriver",
            "project",
            "adopt",
            &id,
            other.to_str().unwrap(),
            "--yes",
        ];
        let (mut stdin, mut stdout, mut stderr) = (&b""[..], Vec::new(), Vec::new());
        let mut io = Io {
            stdin: &mut stdin,
            stdin_is_tty: false,
            stdout: &mut stdout,
            stderr: &mut stderr,
        };
        let code = run(
            argv.map(str::to_string).to_vec(),
            &env,
            &home,
            &tmp,
            &mut io,
        );
        assert!(
            BEFORE_THE_SECOND_READ.take().is_none(),
            "the second read was reached"
        );
        let text = |bytes: Vec<u8>| String::from_utf8(bytes).unwrap();
        (code, text(stdout), text(stderr), id)
    }

    #[test]
    fn adopt_of_a_project_that_keeps_its_directory_names_the_directory() {
        let (code, stdout, stderr, id) = adopt_with(|_, _| {});
        assert!(stdout.starts_with(&format!("refused: project {id} already has a directory (")));
        assert!(stdout.ends_with("/bound); unbind it first\n"), "{stdout}");
        assert_eq!((code, stderr.as_str()), (2, ""));
    }

    #[test]
    fn adopt_of_a_project_gone_between_its_two_reads_says_there_is_none() {
        let (code, stdout, stderr, id) = adopt_with(|database, id| {
            let conn = rusqlite::Connection::open(database).unwrap();
            let gone = conn.execute("DELETE FROM projects WHERE id = ?1", [id]);
            assert_eq!(gone, Ok(1));
        });
        assert_eq!(
            (code, stdout, stderr.as_str()),
            (2, format!("no such project: {id}\n"), "")
        );
    }

    #[test]
    fn adopt_whose_second_read_fails_says_the_store_failed() {
        let (code, stdout, stderr, _) = adopt_with(|database, _| {
            fs::write(database, b"no database any more").unwrap();
        });
        assert_eq!(
            (code, stdout.as_str(), stderr.as_str()),
            (
                2,
                "refused: could not complete the store write; inspect with memriver project explain\n",
                ""
            )
        );
    }

    #[test]
    fn count_child_git_markers_counts_direct_children_only() {
        let sandbox = Sandbox::new();
        let base = sandbox.dir.path().join("base");
        let outside = sandbox.dir.path().join("outside-repo");
        for directory in ["a/.git", "b", "c/deep/.git", ".git"] {
            std::fs::create_dir_all(base.join(directory)).unwrap();
        }
        std::fs::write(base.join("b/.git"), "gitdir: elsewhere").unwrap();
        std::fs::create_dir_all(outside.join(".git")).unwrap();
        std::os::unix::fs::symlink(&outside, base.join("linked")).unwrap();
        assert_eq!(count_child_git_markers(&base), Some(2));
        assert_eq!(count_child_git_markers(&base.join("missing")), None);
    }
}
