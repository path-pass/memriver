//! Git facts as every caller gets them, against real git: where a directory inside a
//! repository belongs, its branch, and what is left when git cannot say.

use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};

use memriver_core::application::contexts::degraded_context;
use memriver_core::models::context::ResolutionState;
use memriver_core::models::project::Project;
use memriver_core::repository::sqlite::project_store::ProjectStore;
use memriver_core::repository::worktree::{GitFacts, git_facts};
use memriver_core::settings::{Env, HEADER_FIELD_CHARS};
use memriver_test_support::{Sandbox, isolated_command};

struct Fixture {
    sandbox: Sandbox,
    base: PathBuf,
    env: Env,
}

fn fixture() -> Fixture {
    let sandbox = Sandbox::new();
    let base = fs::canonicalize(sandbox.dir.path()).unwrap();
    let env = sandbox.env();
    Fixture { sandbox, base, env }
}

fn at(main_tree: &Path, branch: Option<&str>) -> Option<GitFacts> {
    Some(GitFacts {
        main_tree: main_tree.to_path_buf(),
        branch: branch.map(str::to_string),
    })
}

fn text(path: &Path) -> &str {
    path.to_str().unwrap()
}

impl Fixture {
    /// Real git, for building a repository: isolated from the user's and the system's
    /// configuration. None when this git cannot do what is asked.
    fn try_git(&self, cwd: &Path, args: &[&str]) -> Option<String> {
        let git = ["/usr/bin/git", "/bin/git", "/usr/local/bin/git"]
            .into_iter()
            .find(|git| Path::new(git).exists())
            .expect("git is installed");
        let output = isolated_command(&self.sandbox, Path::new(git))
            .current_dir(cwd)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .args(["-c", "user.name=t", "-c", "user.email=t@t"])
            .args(args)
            .output()
            .unwrap();
        output
            .status
            .success()
            .then(|| String::from_utf8(output.stdout).unwrap())
    }

    fn git(&self, cwd: &Path, args: &[&str]) -> String {
        self.try_git(cwd, args)
            .unwrap_or_else(|| panic!("git {args:?} failed"))
    }

    fn repository(&self, name: &str) -> PathBuf {
        let dir = self.base.join(name);
        self.git(&self.base, &["init", "-q", "-b", "main", text(&dir)]);
        self.git(&dir, &["commit", "-q", "--allow-empty", "-m", "init"]);
        dir
    }

    fn worktree(&self, main: &Path, name: &str, branch: &str) -> PathBuf {
        let dir = self.base.join(name);
        self.git(main, &["worktree", "add", "-q", "-b", branch, text(&dir)]);
        dir
    }

    fn facts(&self, dir: &Path) -> Option<GitFacts> {
        git_facts(dir, &self.env)
    }

    /// The environment with a program called `git` first on the search path, and the
    /// file that program leaves when it runs.
    fn fake_git(&self, script: &str) -> (Env, PathBuf) {
        let bin = self.base.join("fake-bin");
        fs::create_dir(&bin).unwrap();
        let mark = self.base.join("git-ran");
        // written by another process: a file this process had open for writing can still be
        // held by a child that another test is starting just then, and the system refuses to
        // run a file someone has open for writing
        let program = format!("#!/bin/sh\ntouch '{}'\n{script}", mark.display());
        let written = isolated_command(&self.sandbox, Path::new("/bin/sh"))
            .args([
                "-c",
                "printf '%s\\n' \"$2\" > \"$1\" && chmod 755 \"$1\"",
                "sh",
            ])
            .arg(bin.join("git"))
            .arg(program)
            .status()
            .unwrap();
        assert!(written.success());
        let mut env = self.env.clone();
        env.insert("PATH".to_string(), format!("{}:/usr/bin:/bin", text(&bin)));
        (env, mark)
    }

    /// A store with one project bound to `directory`.
    fn bound(&self, directory: &Path) -> (ProjectStore, Project) {
        let projects = ProjectStore::new(&self.sandbox.root, &self.sandbox.home);
        let project = Project::new("demo", 120).unwrap();
        let plan = projects.plan_root(directory, None).unwrap();
        projects.create(&project, &plan).unwrap();
        (projects, project)
    }
}

#[test]
fn a_plain_directory_is_unchanged_and_git_is_never_run() {
    let it = fixture();
    let plain = it.base.join("plain/deep");
    fs::create_dir_all(&plain).unwrap();
    let (env, mark) = it.fake_git("exit 1");
    assert_eq!(git_facts(&plain, &env), at(&plain, None));
    assert!(!mark.exists());
}

#[test]
fn an_ordinary_repository_is_answered_without_running_git() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir(worktree.join("sub")).unwrap();
    let (env, mark) = it.fake_git("exit 1");
    assert_eq!(git_facts(&main, &env), at(&main, Some("main")));
    assert_eq!(
        git_facts(&worktree.join("sub"), &env),
        at(&main.join("sub"), Some("feature"))
    );
    assert!(!mark.exists());
    // and a shape the files do not settle is asked of git
    fs::write(
        main.join(".git/refs/tags/main"),
        fs::read(main.join(".git/refs/heads/main")).unwrap(),
    )
    .unwrap();
    assert_eq!(git_facts(&main, &env), None);
    assert!(mark.exists());
    assert_eq!(it.facts(&main), at(&main, Some("heads/main")));
}

#[test]
fn the_main_tree_root_and_its_sub_directories_are_unchanged() {
    let it = fixture();
    let main = it.repository("main");
    fs::create_dir(main.join("sub")).unwrap();
    assert_eq!(it.facts(&main), at(&main, Some("main")));
    assert_eq!(
        it.facts(&main.join("sub")),
        at(&main.join("sub"), Some("main"))
    );
}

#[test]
fn a_linked_worktree_maps_to_the_main_root_keeping_the_sub_directory() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir(worktree.join("newdir")).unwrap();
    assert_eq!(it.facts(&worktree), at(&main, Some("feature")));
    assert_eq!(
        it.facts(&worktree.join("newdir")),
        at(&main.join("newdir"), Some("feature"))
    );
    assert!(!main.join("newdir").exists());
}

#[test]
fn a_worktree_with_relative_paths_maps_too() {
    let it = fixture();
    let main = it.repository("main");
    let relative = it.base.join("rel");
    let added = [
        "worktree",
        "add",
        "-q",
        "--relative-paths",
        "-b",
        "rel",
        text(&relative),
    ];
    if it.try_git(&main, &added).is_none() {
        return; // a git that does not know the option
    }
    assert_eq!(it.facts(&relative), at(&main, Some("rel")));
}

#[test]
fn a_submodule_is_its_own_main_tree() {
    let it = fixture();
    let main = it.repository("main");
    let library = it.repository("library");
    let added = [
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        text(&library),
        "sub",
    ];
    it.git(&main, &added);
    let branch = it.git(&main.join("sub"), &["rev-parse", "--abbrev-ref", "HEAD"]);
    assert_eq!(
        it.facts(&main.join("sub")),
        at(&main.join("sub"), Some(branch.trim()))
    );
}

#[test]
fn a_separate_git_dir_repository_is_unchanged() {
    let it = fixture();
    let (meta, work) = (it.base.join("meta"), it.base.join("work"));
    it.git(
        &it.base,
        &[
            "init",
            "-q",
            "-b",
            "main",
            "--separate-git-dir",
            text(&meta),
            text(&work),
        ],
    );
    // no commit yet: the place is known, the branch is not
    assert_eq!(it.facts(&work), at(&work, None));
}

#[test]
fn a_worktree_whose_admin_commondir_is_gone_is_unknown() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::remove_file(main.join(".git/worktrees/wt/commondir")).unwrap();
    assert_eq!(it.facts(&worktree), None);
}

#[test]
fn a_git_file_pointing_nowhere_is_unknown() {
    let it = fixture();
    let broken = it.base.join("broken");
    fs::create_dir(&broken).unwrap();
    fs::write(broken.join(".git"), "gitdir: ../gone\n").unwrap();
    assert_eq!(it.facts(&broken), None);
}

#[test]
fn a_worktree_of_a_bare_repository_named_dot_git_is_unknown() {
    let it = fixture();
    let main = it.repository("main");
    let bare = it.base.join("x/.git");
    it.git(
        &it.base,
        &["init", "-q", "--bare", "-b", "main", text(&bare)],
    );
    it.git(&main, &["push", "-q", text(&bare), "main"]);
    let worktree = it.base.join("bare-wt");
    it.git(&bare, &["worktree", "add", "-q", text(&worktree), "main"]);
    assert_eq!(it.facts(&worktree), None);
}

/// A repository whose git directory lies at `meta`, with a linked worktree.
fn separate_with_worktree(it: &Fixture, meta: &Path) -> PathBuf {
    let work = it.base.join("work");
    it.git(
        &it.base,
        &[
            "init",
            "-q",
            "-b",
            "main",
            "--separate-git-dir",
            text(meta),
            text(&work),
        ],
    );
    it.git(&work, &["commit", "-q", "--allow-empty", "-m", "init"]);
    let worktree = it.base.join("wt2");
    it.git(
        &work,
        &["worktree", "add", "-q", "-b", "feature", text(&worktree)],
    );
    worktree
}

#[test]
fn a_worktree_of_a_separate_git_dir_repository_is_unknown() {
    let it = fixture();
    let worktree = separate_with_worktree(&it, &it.base.join("meta"));
    assert_eq!(it.facts(&worktree), None);
}

#[test]
fn a_separate_git_dir_named_dot_git_maps_into_its_parent_known_limitation() {
    // git lists <X> (the metadata directory minus "/.git") first, and asked in <X> it
    // answers as a working tree of the same repository. Pinned so that changing this is a
    // deliberate act.
    let it = fixture();
    fs::create_dir(it.base.join("x")).unwrap();
    let worktree = separate_with_worktree(&it, &it.base.join("x/.git"));
    assert_eq!(it.facts(&worktree), at(&it.base.join("x"), Some("feature")));
}

#[test]
fn a_main_tree_whose_git_names_another_toplevel_is_unknown() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir(it.base.join("other")).unwrap();
    it.git(
        &main,
        &["config", "core.worktree", text(&it.base.join("other"))],
    );
    assert_eq!(it.facts(&worktree), None);
}

#[test]
fn a_worktree_spelled_through_a_case_alias_is_unknown_never_mis_mapped() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir(worktree.join("sub")).unwrap();
    let alias = it.base.join("WT/sub");
    if !alias.exists() {
        return; // names differing in case are different names here
    }
    assert_eq!(it.facts(&alias), None);
}

#[test]
fn git_not_installed_is_unknown() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    // shapes only git can answer for
    it.git(&main, &["config", "extensions.worktreeConfig", "true"]);
    assert_eq!(it.facts(&worktree), at(&main, Some("feature")));
    let empty = it.base.join("empty-bin");
    fs::create_dir(&empty).unwrap();
    let mut env = it.env.clone();
    env.insert("PATH".to_string(), text(&empty).to_string());
    assert_eq!(git_facts(&main, &env), None);
    assert_eq!(git_facts(&worktree, &env), None);
}

#[test]
fn git_that_hangs_past_the_timeout_is_unknown() {
    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["config", "extensions.worktreeConfig", "true"]);
    let (env, mark) = it.fake_git("exec sleep 30");
    let started = std::time::Instant::now();
    assert_eq!(git_facts(&main, &env), None);
    assert!(mark.exists());
    assert!(started.elapsed() < std::time::Duration::from_secs(10));
}

#[test]
fn inherited_git_variables_are_ignored() {
    let it = fixture();
    let main = it.repository("main");
    let other = it.repository("other");
    let worktree = it.worktree(&main, "wt", "feature");
    let mut env = it.env.clone();
    env.insert("GIT_DIR".to_string(), text(&other.join(".git")).to_string());
    env.insert("GIT_WORK_TREE".to_string(), text(&other).to_string());
    env.insert(
        "GIT_CEILING_DIRECTORIES".to_string(),
        text(&it.base).to_string(),
    );
    assert_eq!(git_facts(&worktree, &env), at(&main, Some("feature")));
    assert_eq!(git_facts(&main, &env), at(&main, Some("main")));
}

#[test]
fn current_branch_of_main_worktree_and_detached_head() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    assert_eq!(it.facts(&main).unwrap().branch.as_deref(), Some("main"));
    assert_eq!(
        it.facts(&worktree).unwrap().branch.as_deref(),
        Some("feature")
    );
    it.git(&main, &["checkout", "-q", "--detach"]);
    assert_eq!(it.facts(&main).unwrap().branch.as_deref(), Some("HEAD"));
}

#[test]
fn a_symlinked_alias_outside_git_answers_its_canonical_directory() {
    let it = fixture();
    let target = it.base.join("a/sub");
    fs::create_dir_all(&target).unwrap();
    fs::create_dir(it.base.join("b")).unwrap();
    symlink(&target, it.base.join("b/alias")).unwrap();
    assert_eq!(it.facts(&it.base.join("b/alias")), at(&target, None));
}

#[test]
fn an_alias_into_a_linked_worktree_maps_like_the_worktree_itself() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir(worktree.join("sub")).unwrap();
    symlink(worktree.join("sub"), it.base.join("link")).unwrap();
    assert_eq!(
        it.facts(&it.base.join("link")),
        at(&main.join("sub"), Some("feature"))
    );
}

#[test]
fn a_path_that_is_not_an_existing_directory_is_unknown_never_fails() {
    let it = fixture();
    fs::write(it.base.join("file"), "x").unwrap();
    for path in ["x\0y", "/does/not/exist", text(&it.base.join("file"))] {
        assert_eq!(it.facts(Path::new(path)), None, "{path:?}");
    }
}

// --- what a session started there is given to resolve with ---

#[test]
fn a_directory_in_a_repository_git_refuses_has_no_facts_whatever_is_bound_there() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    let (projects, project) = it.bound(&main);
    // the files alone would name the main tree, and with it this project
    assert_eq!(it.facts(&worktree), at(&main, Some("feature")));
    let resolved = projects.resolve(&worktree, None, Some(&main)).unwrap();
    assert_eq!(resolved.project.map(|found| found.id), Some(project.id));
    // a format version git refuses: nothing about the repository is known, and no place
    // is handed on to look a project up with
    let config = main.join(".git/config");
    let written = fs::read_to_string(&config).unwrap();
    fs::write(
        &config,
        written.replace(
            "repositoryformatversion = 0",
            "repositoryformatversion = 99",
        ),
    )
    .unwrap();
    assert_eq!(it.facts(&worktree), None);
    assert_eq!(it.facts(&main), None);
    // the sentence such a session is answered with
    let context = degraded_context(
        "the git worktree could not be mapped to its main working tree",
        None,
        HEADER_FIELD_CHARS,
        None,
    );
    assert_eq!(
        context.header,
        "project: unavailable \u{2014} this directory could not be matched to one project (the \
         git worktree could not be mapped to its main working tree); ask the user to run \
         memriver project explain"
    );
    assert_eq!(context.read_write_set.project_id, None);
}

#[test]
fn a_directory_in_a_repository_with_a_damaged_branch_ref_keeps_its_place_and_has_no_branch() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir(worktree.join("sub")).unwrap();
    let (projects, project) = it.bound(&main);
    fs::write(main.join(".git/refs/heads/feature"), "not-a-sha\n").unwrap();
    // the layout is known, only the branch is not
    let facts = it.facts(&worktree.join("sub")).unwrap();
    assert_eq!(Some(facts.clone()), at(&main.join("sub"), None));
    let resolved = projects
        .resolve(&worktree.join("sub"), None, Some(&facts.main_tree))
        .unwrap();
    assert_eq!(resolved.state, ResolutionState::Registered);
    assert_eq!(resolved.project.map(|found| found.id), Some(project.id));
}
