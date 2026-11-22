//! The two ways to the same facts, against real git: where the files answer they must
//! say what git says, and every shape they are not sure about must reach git.
//!
//! Each repository is made with the real `git` in a temporary directory and then, where
//! a case needs it, damaged by editing its files.

use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};

use memriver_test_support::{Sandbox, isolated_command};

use super::*;

const WAIT: Duration = Duration::from_secs(10);
const ID_40: &str = "0123456789abcdef0123456789abcdef01234567";
const ID_64: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

struct Fixture {
    sandbox: Sandbox,
    base: PathBuf,
    env: Env,
}

fn fixture() -> Fixture {
    let sandbox = Sandbox::new();
    let base = canonical(sandbox.dir.path()).unwrap();
    let env = sandbox.env();
    Fixture { sandbox, base, env }
}

fn at(main_tree: &Path, branch: Option<&str>) -> GitFacts {
    GitFacts {
        main_tree: main_tree.to_path_buf(),
        branch: branch.map(str::to_string),
    }
}

fn append(path: &Path, text: &str) {
    let mut content = fs::read_to_string(path).unwrap();
    content.push_str(text);
    fs::write(path, content).unwrap();
}

fn replace(path: &Path, from: &str, to: &str) {
    let content = fs::read_to_string(path).unwrap();
    assert!(content.contains(from), "{content}");
    fs::write(path, content.replacen(from, to, 1)).unwrap();
}

impl Fixture {
    /// Real git, for building a repository: isolated from the user's and the system's
    /// configuration.
    fn git(&self, cwd: &Path, args: &[&str]) -> String {
        let output = self.run_git(cwd, args);
        assert!(
            output.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap()
    }

    /// Whether this git can do what is asked; an older one may not know an option.
    fn git_can(&self, cwd: &Path, args: &[&str]) -> bool {
        self.run_git(cwd, args).status.success()
    }

    fn run_git(&self, cwd: &Path, args: &[&str]) -> std::process::Output {
        let git = ["/usr/bin/git", "/bin/git", "/usr/local/bin/git"]
            .into_iter()
            .find(|git| Path::new(git).exists())
            .expect("git is installed");
        isolated_command(&self.sandbox, Path::new(git))
            .current_dir(cwd)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .args(["-c", "user.name=t", "-c", "user.email=t@t"])
            .args(args)
            .output()
            .unwrap()
    }

    /// A repository on branch `main` with one commit.
    fn repository(&self, name: &str) -> PathBuf {
        let dir = self.base.join(name);
        self.git(
            &self.base,
            &["init", "-q", "-b", "main", dir.to_str().unwrap()],
        );
        self.git(&dir, &["commit", "-q", "--allow-empty", "-m", "init"]);
        dir
    }

    /// A linked worktree of `main` on a new branch.
    fn worktree(&self, main: &Path, name: &str, branch: &str) -> PathBuf {
        let dir = self.base.join(name);
        self.git(
            main,
            &["worktree", "add", "-q", "-b", branch, dir.to_str().unwrap()],
        );
        dir
    }

    fn asked_of_git(&self, dir: &Path) -> Option<GitFacts> {
        from_git(dir, &self.env, WAIT)
    }

    /// The files answer, and git gives the same answer.
    fn agreed(&self, dir: &Path) -> GitFacts {
        let Confirmed::Yes(files) = from_files(dir, &self.env) else {
            panic!("left to git: {dir:?}");
        };
        assert_eq!(self.asked_of_git(dir), Some(files.clone()), "{dir:?}");
        // and the files were enough: git is not run
        let before = GIT_RUNS.get();
        assert_eq!(facts(dir, &self.env, WAIT), Some(files.clone()));
        assert_eq!(GIT_RUNS.get(), before, "git was run for {dir:?}");
        files
    }

    /// The files leave it to git, and git's answer is the answer.
    fn left_to_git(&self, dir: &Path) -> Option<GitFacts> {
        assert_eq!(from_files(dir, &self.env), Confirmed::Unsure, "{dir:?}");
        let before = GIT_RUNS.get();
        let answer = facts(dir, &self.env, WAIT);
        assert!(GIT_RUNS.get() > before, "git was not run for {dir:?}");
        assert_eq!(answer, self.asked_of_git(dir), "{dir:?}");
        answer
    }

    /// `left_to_git`, unless nothing stops this user (root): then nothing is asserted.
    fn left_to_git_or_skip(&self, dir: &Path, stopped: bool) -> Option<GitFacts> {
        if stopped { self.left_to_git(dir) } else { None }
    }

    /// Whether git's own layout query succeeds in `dir`.
    fn git_knows_the_layout(&self, dir: &Path) -> bool {
        locate(dir, &self.env, WAIT).is_some()
    }
}

// --- the files answer as git does ---

#[test]
fn main_tree() {
    let it = fixture();
    let main = it.repository("main");
    assert_eq!(it.agreed(&main), at(&main, Some("main")));
}

#[test]
fn sub_directory() {
    let it = fixture();
    let main = it.repository("main");
    let deep = main.join("sub/deep");
    fs::create_dir_all(&deep).unwrap();
    assert_eq!(it.agreed(&deep), at(&deep, Some("main")));
    assert_eq!(
        it.agreed(&main.join("sub")),
        at(&main.join("sub"), Some("main"))
    );
}

#[test]
fn linked_worktree() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    assert_eq!(it.agreed(&worktree), at(&main, Some("feature")));
    // the main tree is not disturbed by having one
    assert_eq!(it.agreed(&main), at(&main, Some("main")));
}

#[test]
fn linked_worktree_sub_directory() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir_all(worktree.join("newdir/x")).unwrap();
    assert_eq!(
        it.agreed(&worktree.join("newdir/x")),
        at(&main.join("newdir/x"), Some("feature"))
    );
    assert!(!main.join("newdir").exists());
    // a worktree that lies inside the main tree is still its own
    let inside = main.join("nested");
    it.git(
        &main,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "inner",
            inside.to_str().unwrap(),
        ],
    );
    fs::create_dir(inside.join("sub")).unwrap();
    assert_eq!(
        it.agreed(&inside.join("sub")),
        at(&main.join("sub"), Some("inner"))
    );
}

#[test]
fn detached_head() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    it.git(&main, &["checkout", "-q", "--detach"]);
    it.git(&worktree, &["checkout", "-q", "--detach"]);
    assert_eq!(it.agreed(&main), at(&main, Some("HEAD")));
    assert_eq!(it.agreed(&worktree), at(&main, Some("HEAD")));
}

#[test]
fn a_40_hex_head_is_detached_on_the_fast_path() {
    let it = fixture();
    let main = it.repository("main");
    // an id git has no object for is still a detached HEAD to git
    fs::write(main.join(".git/HEAD"), format!("{ID_40}\n")).unwrap();
    assert_eq!(it.agreed(&main), at(&main, Some("HEAD")));
}

#[test]
fn unborn_branch() {
    let it = fixture();
    let fresh = it.base.join("fresh");
    it.git(
        &it.base,
        &["init", "-q", "-b", "main", fresh.to_str().unwrap()],
    );
    assert_eq!(it.agreed(&fresh), at(&fresh, None));
    // and in a repository that has commits elsewhere
    let main = it.repository("main");
    it.git(&main, &["checkout", "-q", "--orphan", "empty"]);
    assert_eq!(it.agreed(&main), at(&main, None));
}

#[test]
fn unborn_branch_only_when_both_lookups_succeeded() {
    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["pack-refs", "--all"]);
    it.git(&main, &["checkout", "-q", "--orphan", "empty"]);
    // neither loose nor packed: unborn, by the files and by git
    assert_eq!(it.agreed(&main), at(&main, None));
    // the packed file cannot be read: the branch may be in it
    let packed = main.join(".git/packed-refs");
    fs::set_permissions(&packed, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&packed).is_err() {
        assert_eq!(from_files(&main, &it.env), Confirmed::Unsure);
    }
    fs::set_permissions(&packed, fs::Permissions::from_mode(0o644)).unwrap();
    // the loose ref cannot be looked at: a file where its directory should be
    fs::write(main.join(".git/refs/heads/empty"), "").unwrap();
    assert_eq!(from_files(&main, &it.env), Confirmed::Unsure);
    fs::remove_file(main.join(".git/refs/heads/empty")).unwrap();
    fs::write(main.join(".git/refs/heads/dir"), format!("{ID_40}\n")).unwrap();
    fs::write(main.join(".git/HEAD"), "ref: refs/heads/dir/below\n").unwrap();
    it.left_to_git(&main);
}

#[test]
fn branch_named_like_a_tag() {
    let it = fixture();
    let main = it.repository("main");
    for name in ["v1.0", "release/2.0", "feat/\u{fc}-\u{9879}", "tags/x"] {
        it.git(&main, &["checkout", "-q", "-b", name]);
        assert_eq!(it.agreed(&main), at(&main, Some(name)), "{name}");
    }
    // a name that is also a file of the git directory is git's to spell
    it.git(&main, &["checkout", "-q", "-b", "config"]);
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("config"))));
}

#[test]
fn packed_refs_only() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    it.git(&main, &["tag", "v1"]);
    it.git(&main, &["tag", "-a", "-m", "annotated", "v2"]);
    it.git(&main, &["pack-refs", "--all"]);
    assert!(!main.join(".git/refs/heads/main").exists());
    assert!(
        fs::read_to_string(main.join(".git/packed-refs"))
            .unwrap()
            .contains("\n^")
    );
    assert_eq!(it.agreed(&main), at(&main, Some("main")));
    assert_eq!(it.agreed(&worktree), at(&main, Some("feature")));
}

#[test]
fn a_40_digit_loose_ref_stays_on_the_fast_path() {
    let it = fixture();
    let main = it.repository("main");
    fs::write(main.join(".git/refs/heads/main"), format!("{ID_40}\n")).unwrap();
    assert_eq!(it.agreed(&main), at(&main, Some("main")));
}

#[test]
fn a_separate_git_directory_is_a_main_tree() {
    let it = fixture();
    let (meta, work) = (it.base.join("meta"), it.base.join("work"));
    let args = ["init", "-q", "-b", "main", "--separate-git-dir"];
    it.git(
        &it.base,
        &[&args[..], &[meta.to_str().unwrap(), work.to_str().unwrap()]].concat(),
    );
    it.git(&work, &["commit", "-q", "--allow-empty", "-m", "init"]);
    fs::create_dir(work.join("sub")).unwrap();
    assert_eq!(
        it.agreed(&work.join("sub")),
        at(&work.join("sub"), Some("main"))
    );
}

// --- what the files are not sure about goes to git ---

#[test]
fn bare_repository() {
    let it = fixture();
    let bare = it.base.join("x/.git");
    it.git(
        &it.base,
        &["init", "-q", "--bare", "-b", "main", bare.to_str().unwrap()],
    );
    let main = it.repository("main");
    it.git(&main, &["push", "-q", bare.to_str().unwrap(), "main"]);
    let worktree = it.base.join("bare-wt");
    it.git(
        &bare,
        &["worktree", "add", "-q", worktree.to_str().unwrap(), "main"],
    );
    assert_eq!(it.left_to_git(&worktree), None);
}

#[test]
fn bare_without_spaces() {
    let it = fixture();
    let main = it.repository("main");
    replace(&main.join(".git/config"), "bare = false", "bare=true");
    assert_eq!(it.left_to_git(&main), None);
}

#[test]
fn bare_in_other_case_and_spelling() {
    for spelling in [
        "BARE = Yes",
        "Bare = ON",
        "bare = 1",
        "bare",
        "bare = maybe",
    ] {
        let it = fixture();
        let main = it.repository("main");
        replace(&main.join(".git/config"), "bare = false", spelling);
        it.left_to_git(&main);
    }
    // every spelling of false stays with the files
    for spelling in [
        "bare = false",
        "BARE = No",
        "bare=off",
        "bare = 0",
        "bare =",
    ] {
        let it = fixture();
        let main = it.repository("main");
        replace(&main.join(".git/config"), "bare = false", spelling);
        assert_eq!(it.agreed(&main), at(&main, Some("main")), "{spelling}");
    }
}

#[test]
fn core_worktree_set() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir(it.base.join("other")).unwrap();
    it.git(
        &main,
        &[
            "config",
            "core.worktree",
            it.base.join("other").to_str().unwrap(),
        ],
    );
    // git names another toplevel for the main tree: the linked one cannot be mapped
    assert_eq!(it.left_to_git(&worktree), None);
    it.left_to_git(&main);
}

#[test]
fn worktree_without_spaces() {
    let it = fixture();
    let main = it.repository("main");
    let elsewhere = it.base.join("elsewhere");
    fs::create_dir(&elsewhere).unwrap();
    append(
        &main.join(".git/config"),
        &format!("[CORE]\n\tworktree={}\n", elsewhere.display()),
    );
    it.left_to_git(&main);
}

#[test]
fn reftable() {
    let it = fixture();
    let dir = it.base.join("reftable");
    let init = [
        "init",
        "-q",
        "-b",
        "main",
        "--ref-format=reftable",
        dir.to_str().unwrap(),
    ];
    if !it.git_can(&it.base, &init) {
        return; // a git that does not know the format
    }
    it.git(&dir, &["commit", "-q", "--allow-empty", "-m", "init"]);
    assert_eq!(it.left_to_git(&dir), Some(at(&dir, Some("main"))));
}

#[test]
fn worktree_config_extension() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    it.git(&main, &["config", "extensions.worktreeConfig", "true"]);
    assert_eq!(it.left_to_git(&worktree), Some(at(&main, Some("feature"))));
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("main"))));
}

#[test]
fn include_in_config() {
    let it = fixture();
    let main = it.repository("main");
    fs::write(main.join(".git/more"), "[core]\n\tbare = true\n").unwrap();
    append(&main.join(".git/config"), "[include]\n\tpath = more\n");
    // what git makes of the included file is git's business (when it looks for the
    // working tree: nothing)
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("main"))));
}

#[test]
fn includeif_in_config() {
    let it = fixture();
    let main = it.repository("main");
    append(
        &main.join(".git/config"),
        "[includeIf \"gitdir:/nowhere/\"]\n\tpath = more\n",
    );
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("main"))));
}

#[test]
fn config_the_parser_cannot_parse() {
    for text in [
        "[user]\n\tname = a \\\n\t\tb\n",
        "[core] bare = false\n",
        "[alias]\n\tx = \"!f() { echo \\\"hi\\\"; }; \\\n f\"\n",
    ] {
        let it = fixture();
        let main = it.repository("main");
        append(&main.join(".git/config"), text);
        assert_eq!(
            it.left_to_git(&main),
            Some(at(&main, Some("main"))),
            "{text:?}"
        );
    }
    // not configuration at all: git refuses it too
    let it = fixture();
    let main = it.repository("main");
    append(&main.join(".git/config"), "[core\n");
    assert_eq!(it.left_to_git(&main), None);
    // no configuration file
    let it = fixture();
    let main = it.repository("main");
    fs::remove_file(main.join(".git/config")).unwrap();
    it.left_to_git(&main);
}

#[test]
fn git_dir_variable_in_env() {
    let it = fixture();
    let main = it.repository("main");
    let other = it.repository("other");
    let worktree = it.worktree(&main, "wt", "feature");
    for name in ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"] {
        let mut env = it.env.clone();
        env.insert(
            name.to_string(),
            other.join(".git").to_str().unwrap().to_string(),
        );
        assert_eq!(from_files(&worktree, &env), Confirmed::Unsure, "{name}");
        // git is asked without the variable: the answer is for this directory
        let before = GIT_RUNS.get();
        assert_eq!(
            facts(&worktree, &env, WAIT),
            Some(at(&main, Some("feature")))
        );
        assert!(GIT_RUNS.get() > before);
    }
    // any other variable of git's changes nothing about the files
    let mut env = it.env.clone();
    env.insert("GIT_AUTHOR_NAME".to_string(), "someone".to_string());
    assert_eq!(
        from_files(&worktree, &env),
        Confirmed::Yes(at(&main, Some("feature")))
    );
}

#[test]
fn gitfile_with_a_second_line() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    let gitfile = worktree.join(".git");
    let written = fs::read_to_string(&gitfile).unwrap();
    for damaged in [
        format!("{written}second line\n"),
        written.replace("gitdir: ", "gitdir:"),
        written.replace('\n', "\r\n"),
        "gitdir: \n".to_string(),
        "gitdir: ../gone\n".to_string(),
        String::new(),
    ] {
        fs::write(&gitfile, &damaged).unwrap();
        it.left_to_git(&worktree);
    }
    // without the line end it is still one line
    fs::write(&gitfile, written.trim_end()).unwrap();
    assert_eq!(it.agreed(&worktree), at(&main, Some("feature")));
    // a link in place of the entry
    fs::remove_file(&gitfile).unwrap();
    symlink(main.join(".git/worktrees/wt"), &gitfile).unwrap();
    it.left_to_git(&worktree);
}

#[test]
fn a_git_file_pointing_nowhere_is_unknown() {
    let it = fixture();
    let broken = it.base.join("broken");
    fs::create_dir(&broken).unwrap();
    fs::write(broken.join(".git"), "gitdir: ../gone\n").unwrap();
    assert_eq!(it.left_to_git(&broken), None);
}

#[test]
fn linked_worktree_whose_main_tree_has_no_git_directory() {
    // the shared directory is not `<main>/.git`: a separate git directory
    let it = fixture();
    let (meta, work) = (it.base.join("meta"), it.base.join("work"));
    let args = ["init", "-q", "-b", "main", "--separate-git-dir"];
    it.git(
        &it.base,
        &[&args[..], &[meta.to_str().unwrap(), work.to_str().unwrap()]].concat(),
    );
    it.git(&work, &["commit", "-q", "--allow-empty", "-m", "init"]);
    let worktree = it.base.join("wt2");
    it.git(
        &work,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "feature",
            worktree.to_str().unwrap(),
        ],
    );
    assert_eq!(it.left_to_git(&worktree), None);

    // `commondir` is gone, names nothing, or names a file called `.git`
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    let commondir = main.join(".git/worktrees/wt/commondir");
    fs::write(it.base.join("file"), "x").unwrap();
    fs::create_dir(it.base.join("plain")).unwrap();
    fs::write(it.base.join("plain/.git"), "x").unwrap();
    for named in [
        "../../../gone\n",
        "\n",
        "",
        "../../../../file\n",
        "../../../../plain/.git\n",
    ] {
        fs::write(&commondir, named).unwrap();
        it.left_to_git(&worktree);
    }
    fs::remove_file(&commondir).unwrap();
    assert_eq!(it.left_to_git(&worktree), None);
}

#[test]
fn a_linked_worktree_whose_main_tree_git_would_not_answer_in() {
    // the worktree's own files are in order; git asks the main tree as well
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::write(main.join(".git/HEAD"), "garbage\n").unwrap();
    assert_eq!(it.left_to_git(&worktree), None);
}

#[test]
fn a_git_directory_that_is_not_under_worktrees() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    // the administrative directory moved somewhere git does not keep them
    let moved = main.join(".git/elsewhere");
    fs::rename(main.join(".git/worktrees/wt"), &moved).unwrap();
    fs::write(moved.join("commondir"), "..\n").unwrap();
    fs::write(
        worktree.join(".git"),
        format!("gitdir: {}\n", moved.display()),
    )
    .unwrap();
    it.left_to_git(&worktree);
}

#[test]
fn inside_the_git_directory_there_is_no_working_tree() {
    let it = fixture();
    let main = it.repository("main");
    for inside in [".git", ".git/refs/heads", ".git/hooks"] {
        fs::create_dir_all(main.join(inside)).unwrap();
        assert_eq!(it.left_to_git(&main.join(inside)), None, "{inside}");
    }
    // a `.git` that is no git directory is passed over by git, on its way up
    let it = fixture();
    let main = it.repository("main");
    fs::create_dir_all(main.join("sub/.git")).unwrap();
    assert_eq!(
        it.left_to_git(&main.join("sub")),
        Some(at(&main.join("sub"), Some("main")))
    );
}

#[test]
fn a_repository_of_another_user_is_left_to_git() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    // SAFETY: no arguments, cannot fail
    let user = unsafe { libc::geteuid() };
    for dir in [&main, &worktree] {
        assert!(read_files(dir, &it.env, user).is_some());
        assert_eq!(read_files(dir, &it.env, user + 1), None);
    }
}

#[test]
fn on_a_file_system_that_ignores_case_a_worktree_spelled_through_a_case_alias_is_never_mis_mapped()
{
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    fs::create_dir(worktree.join("sub")).unwrap();
    let alias = it.base.join("WT/sub");
    if !alias.exists() {
        return; // names differing in case are different names here
    }
    assert_eq!(it.left_to_git(&alias), None);
    // below the worktree's root the spelling is kept, as git's answer keeps it
    let below = worktree.join("SUB");
    assert_eq!(
        it.left_to_git(&below),
        Some(at(&main.join("SUB"), Some("feature")))
    );
    // in a main working tree the directory itself is the answer
    fs::create_dir(main.join("sub")).unwrap();
    assert_eq!(
        it.agreed(&main.join("SUB")),
        at(&main.join("SUB"), Some("main"))
    );
}

#[test]
fn empty_head() {
    let it = fixture();
    let main = it.repository("main");
    fs::write(main.join(".git/HEAD"), "").unwrap();
    assert_eq!(it.left_to_git(&main), None);
}

#[test]
fn truncated_head() {
    for content in [
        "ref: refs/heads/main",
        "ref: refs/heads/",
        "ref: refs/heads/\n",
        "ref: refs/hea",
        &ID_40[..39],
        ID_40,
        &format!("{}\n", &ID_40[..39]),
    ] {
        let it = fixture();
        let main = it.repository("main");
        fs::write(main.join(".git/HEAD"), content).unwrap();
        it.left_to_git(&main);
    }
}

#[test]
fn head_with_garbage() {
    for content in [
        "garbage\n",
        "ref: refs/heads/main\nmore\n",
        "ref: refs/heads/main\n\n",
        "ref:  refs/heads/main\n",
        "ref: refs/heads/main \n",
        "ref: refs/heads/ma in\n",
        "ref: refs/heads/a..b\n",
        "ref: refs/heads/x.lock\n",
        "ref: refs/heads/.hidden\n",
        "ref: refs/heads/a//b\n",
        "ref: refs/heads/main\r\n",
        "ref: refs/heads/caf\u{85}e\n",
        &format!("{}\n", ID_40.to_uppercase()),
        &format!("{ID_40} \n"),
        &format!("{ID_40}\n{ID_40}\n"),
    ] {
        let it = fixture();
        let main = it.repository("main");
        fs::write(main.join(".git/HEAD"), content).unwrap();
        it.left_to_git(&main);
    }
    // a link where the file should be
    let it = fixture();
    let main = it.repository("main");
    fs::rename(main.join(".git/HEAD"), main.join(".git/REAL_HEAD")).unwrap();
    symlink("REAL_HEAD", main.join(".git/HEAD")).unwrap();
    it.left_to_git(&main);
}

#[test]
fn head_ref_outside_refs_heads() {
    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["tag", "v1"]);
    for content in [
        "ref: refs/tags/v1\n",
        "ref: refs/remotes/origin/main\n",
        "ref: main\n",
    ] {
        fs::write(main.join(".git/HEAD"), content).unwrap();
        it.left_to_git(&main);
    }
}

#[test]
fn packed_refs_exists_but_unreadable() {
    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["pack-refs", "--all"]);
    let id = it.git(&main, &["rev-parse", "HEAD"]);
    let packed = main.join(".git/packed-refs");
    fs::set_permissions(&packed, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&packed).is_err() {
        it.left_to_git(&main);
        // even with the branch there as a loose ref: a tag of its name may be packed
        fs::write(main.join(".git/refs/heads/main"), &id).unwrap();
        it.left_to_git(&main);
    }
    fs::set_permissions(&packed, fs::Permissions::from_mode(0o644)).unwrap();
}

#[test]
fn branch_also_a_tag() {
    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["tag", "main"]);
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("heads/main"))));
    // the tag only in the packed file
    it.git(&main, &["pack-refs", "--all"]);
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("heads/main"))));
}

#[test]
fn branch_also_another_ref() {
    // a remote of the branch's name with a HEAD, a remote-tracking ref, a top-level ref
    let it = fixture();
    let main = it.repository("main");
    let id = it.git(&main, &["rev-parse", "HEAD"]);
    it.git(&main, &["checkout", "-q", "-b", "origin"]);
    fs::create_dir_all(main.join(".git/refs/remotes/origin")).unwrap();
    fs::write(main.join(".git/refs/remotes/origin/HEAD"), &id).unwrap();
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("heads/origin"))));

    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["update-ref", "refs/remotes/main", "HEAD"]);
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("heads/main"))));

    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["checkout", "-q", "-b", "FETCH_HEAD"]);
    fs::write(main.join(".git/FETCH_HEAD"), &id).unwrap();
    it.left_to_git(&main);

    // in a linked worktree the other ref lies in the shared directory
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    it.git(&main, &["tag", "feature"]);
    assert_eq!(
        it.left_to_git(&worktree),
        Some(at(&main, Some("heads/feature")))
    );
}

// --- condition 8: only format version 0 without any extension ---

#[test]
fn v0_repository_with_objectformat_sha256_goes_to_git_and_git_refuses() {
    let it = fixture();
    let main = it.repository("main");
    append(
        &main.join(".git/config"),
        "[extensions]\n\tobjectFormat = sha256\n",
    );
    assert_eq!(it.left_to_git(&main), None);
    assert!(!it.git_knows_the_layout(&main));
}

#[test]
fn preciousobjects_with_a_non_boolean_value_goes_to_git_and_git_refuses() {
    let it = fixture();
    let main = it.repository("main");
    append(
        &main.join(".git/config"),
        "[extensions]\n\tpreciousObjects = maybe\n",
    );
    assert_eq!(it.left_to_git(&main), None);
    assert!(!it.git_knows_the_layout(&main));
}

#[test]
fn repository_format_version_99_goes_to_git_and_git_refuses() {
    let it = fixture();
    let main = it.repository("main");
    replace(
        &main.join(".git/config"),
        "repositoryformatversion = 0",
        "repositoryformatversion = 99",
    );
    assert_eq!(it.left_to_git(&main), None);
    assert!(!it.git_knows_the_layout(&main));
}

#[test]
fn a_non_numeric_format_version_goes_to_git() {
    for version in ["zero", "", "00", "0x0", "+0", "0 0"] {
        let it = fixture();
        let main = it.repository("main");
        replace(
            &main.join(".git/config"),
            "repositoryformatversion = 0",
            &format!("repositoryformatversion = {version}"),
        );
        it.left_to_git(&main);
    }
    // written twice, the second time otherwise
    let it = fixture();
    let main = it.repository("main");
    append(
        &main.join(".git/config"),
        "[core]\n\trepositoryFormatVersion = 1\n",
    );
    it.left_to_git(&main);
}

#[test]
fn format_version_1_goes_to_git_even_without_extensions() {
    let it = fixture();
    let main = it.repository("main");
    replace(
        &main.join(".git/config"),
        "repositoryformatversion = 0",
        "repositoryformatversion = 1",
    );
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("main"))));
    // absent is version 0
    let it = fixture();
    let main = it.repository("main");
    replace(
        &main.join(".git/config"),
        "\trepositoryformatversion = 0\n",
        "",
    );
    assert_eq!(it.agreed(&main), at(&main, Some("main")));
}

#[test]
fn any_extension_key_in_version_0_goes_to_git() {
    for key in [
        "noop",
        "partialClone = origin",
        "worktreeConfig = true",
        "refStorage = files",
        "invented = 1",
    ] {
        let it = fixture();
        let main = it.repository("main");
        append(
            &main.join(".git/config"),
            &format!("[extensions]\n\t{key}\n"),
        );
        it.left_to_git(&main);
    }
    let it = fixture();
    let main = it.repository("main");
    append(&main.join(".git/config"), "[EXTENSIONS \"sub\"]\n\tx = 1\n");
    it.left_to_git(&main);
}

#[test]
fn an_empty_extensions_section_header_goes_to_git() {
    let it = fixture();
    let main = it.repository("main");
    append(&main.join(".git/config"), "[extensions]\n");
    assert_eq!(it.left_to_git(&main), Some(at(&main, Some("main"))));
}

// --- condition 6: the ref is validated, not just found ---

#[test]
fn a_loose_ref_with_garbage_content_goes_to_git() {
    let it = fixture();
    let main = it.repository("main");
    fs::write(main.join(".git/refs/heads/main"), "not-a-sha\n").unwrap();
    // the layout is known, only the branch is not
    assert!(it.git_knows_the_layout(&main));
    assert_eq!(it.left_to_git(&main), Some(at(&main, None)));
}

#[test]
fn an_empty_or_truncated_loose_ref_goes_to_git() {
    for content in [
        "",
        "\n",
        &ID_40[..39],
        ID_40,
        &format!("{}\n", &ID_40[..39]),
        &format!("{ID_40}\n\n"),
    ] {
        let it = fixture();
        let main = it.repository("main");
        fs::write(main.join(".git/refs/heads/main"), content).unwrap();
        it.left_to_git(&main);
    }
    // a directory where the ref should be
    let it = fixture();
    let main = it.repository("main");
    fs::remove_file(main.join(".git/refs/heads/main")).unwrap();
    fs::create_dir(main.join(".git/refs/heads/main")).unwrap();
    it.left_to_git(&main);
}

#[test]
fn a_loose_ref_holding_a_symbolic_ref_goes_to_git() {
    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["branch", "other"]);
    fs::write(main.join(".git/refs/heads/main"), "ref: refs/heads/other\n").unwrap();
    it.left_to_git(&main);
    // a link to another ref
    fs::remove_file(main.join(".git/refs/heads/main")).unwrap();
    symlink("other", main.join(".git/refs/heads/main")).unwrap();
    it.left_to_git(&main);
}

#[test]
fn a_64_digit_loose_ref_goes_to_git_and_the_branch_is_null() {
    let it = fixture();
    let main = it.repository("main");
    fs::write(main.join(".git/refs/heads/main"), format!("{ID_64}\n")).unwrap();
    assert!(it.git_knows_the_layout(&main));
    assert_eq!(it.left_to_git(&main), Some(at(&main, None)));
}

#[test]
fn a_64_digit_detached_head_goes_to_git_and_the_branch_is_null() {
    let it = fixture();
    let main = it.repository("main");
    fs::write(main.join(".git/HEAD"), format!("{ID_64}\n")).unwrap();
    assert!(it.git_knows_the_layout(&main));
    assert_eq!(it.left_to_git(&main), Some(at(&main, None)));
}

#[test]
fn a_64_digit_packed_refs_line_goes_to_git() {
    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["pack-refs", "--all"]);
    let packed = main.join(".git/packed-refs");
    let id = it.git(&main, &["rev-parse", "HEAD"]);
    replace(&packed, id.trim(), ID_64);
    assert!(it.git_knows_the_layout(&main));
    assert_eq!(it.left_to_git(&main), Some(at(&main, None)));
}

#[test]
fn a_malformed_packed_refs_line_for_the_branch_goes_to_git() {
    let it = fixture();
    let main = it.repository("main");
    it.git(&main, &["branch", "other"]);
    it.git(&main, &["pack-refs", "--all"]);
    let packed = main.join(".git/packed-refs");
    let written = fs::read_to_string(&packed).unwrap();
    let id = it.git(&main, &["rev-parse", "HEAD"]);
    let id = id.trim();
    for damaged in [
        written.replace(
            &format!("{id} refs/heads/main"),
            "not-a-sha refs/heads/main",
        ),
        written.replace(
            &format!("{id} refs/heads/main"),
            &format!("{id}  refs/heads/main"),
        ),
        written.replace(
            &format!("{id} refs/heads/main"),
            &format!("{id}\trefs/heads/main"),
        ),
        written.replace(&format!("{id} refs/heads/main"), "refs/heads/main"),
        // a line of another ref, a line cut short at the end, a peeled line with no ref
        written.replace(&format!("{id} refs/heads/other"), "garbage"),
        written.trim_end().to_string(),
        format!("^{id}\n{written}"),
        format!("{written}# a comment further down\n"),
    ] {
        fs::write(&packed, &damaged).unwrap();
        assert_eq!(from_files(&main, &it.env), Confirmed::Unsure, "{damaged:?}");
        assert!(it.git_knows_the_layout(&main));
        it.left_to_git(&main);
    }
    // an empty file holds no ref: the branch is not there
    fs::write(&packed, "").unwrap();
    assert_eq!(it.agreed(&main), at(&main, None));
}

// --- what a failing git means ---

/// A program called `git`, first on the search path, that leaves a mark when it runs.
///
/// The script is written by another process: a file this process had open for
/// writing can still be held by a child that another test is starting just then, and the
/// system refuses to run a file someone has open for writing.
fn fake_git(it: &Fixture, script: &str) -> (Env, PathBuf) {
    let bin = it.base.join("fake-bin");
    fs::create_dir(&bin).unwrap();
    let mark = it.base.join("git-ran");
    let text = format!("#!/bin/sh\ntouch '{}'\n{script}", mark.display());
    let written = isolated_command(&it.sandbox, Path::new("/bin/sh"))
        .args([
            "-c",
            "printf '%s\\n' \"$2\" > \"$1\" && chmod 755 \"$1\"",
            "sh",
        ])
        .arg(bin.join("git"))
        .arg(text)
        .status()
        .unwrap();
    assert!(written.success());
    let mut env = it.env.clone();
    env.insert(
        "PATH".to_string(),
        format!("{}:/usr/bin:/bin", bin.display()),
    );
    (env, mark)
}

#[test]
fn layout_query_failing_gives_none() {
    let it = fixture();
    let main = it.repository("main");
    // a shape the files leave to git, and a git that fails
    append(&main.join(".git/config"), "[extensions]\n");
    let (env, mark) = fake_git(&it, "exit 128");
    assert_eq!(facts(&main, &env, WAIT), None);
    assert!(mark.exists());
    // answers that are not three absolute paths
    for (number, answer) in [
        "/a\\n/b",
        "/a\\n/b\\n/c\\n/d",
        "/a\\nb\\n/c",
        "/a\\n\\n/c",
        "/a\\n/b\\n/c\\205",
    ]
    .into_iter()
    .enumerate()
    {
        let it = fixture();
        let main = it.repository("main");
        append(&main.join(".git/config"), "[extensions]\n");
        let (env, _) = fake_git(&it, &format!("printf '{answer}\\n'"));
        assert_eq!(facts(&main, &env, WAIT), None, "{number}");
    }
}

#[test]
fn layout_ok_but_branch_query_failing_gives_the_main_tree_with_branch_none() {
    let it = fixture();
    let main = it.repository("main");
    append(&main.join(".git/config"), "[extensions]\n");
    let dot_git = main.join(".git");
    let layout = format!(
        "printf '%s\\n' '{0}' '{1}' '{1}'",
        main.display(),
        dot_git.display()
    );
    for branch_answer in [
        "exit 128",
        "exit 0",
        "echo; exit 0",
        "printf 'a\\nb\\n'",
        "printf 'main\\n'; exit 1",
    ] {
        let it = fixture();
        let script = format!("case \"$*\" in *--abbrev-ref*) {branch_answer};; *) {layout};; esac");
        let (env, _) = fake_git(&it, &script);
        assert_eq!(
            facts(&main, &env, WAIT),
            Some(at(&main, None)),
            "{branch_answer}"
        );
    }
    // and with an answer, that answer
    let it = fixture();
    let script =
        format!("case \"$*\" in *--abbrev-ref*) printf 'topic\\r\\n';; *) {layout};; esac");
    let (env, _) = fake_git(&it, &script);
    assert_eq!(facts(&main, &env, WAIT), Some(at(&main, Some("topic"))));
}

#[test]
fn git_missing_or_timing_out_is_unknown_not_a_guess() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    append(&main.join(".git/config"), "[extensions]\n");
    // not installed
    let empty = it.base.join("empty-bin");
    fs::create_dir(&empty).unwrap();
    let mut env = it.env.clone();
    env.insert("PATH".to_string(), empty.to_str().unwrap().to_string());
    for dir in [&main, &worktree] {
        assert_eq!(facts(dir, &env, WAIT), None);
    }
    // not executable, and hanging past the time it is given
    fs::write(empty.join("git"), "#!/bin/sh\nexit 0\n").unwrap();
    assert_eq!(facts(&main, &env, WAIT), None);
    let (env, _) = fake_git(&it, "exec sleep 30");
    let started = Instant::now();
    assert_eq!(facts(&main, &env, Duration::from_millis(200)), None);
    assert!(started.elapsed() < Duration::from_secs(2));
    // a git that closes its output and then hangs is not waited for either
    let it = fixture();
    let (env, _) = fake_git(&it, "exec >&- ; exec sleep 30");
    let started = Instant::now();
    assert_eq!(facts(&main, &env, Duration::from_millis(200)), None);
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn lines_are_split_where_python_splits_them() {
    assert_eq!(py_splitlines(""), Vec::<&str>::new());
    assert_eq!(py_splitlines("a"), ["a"]);
    assert_eq!(py_splitlines("a\n"), ["a"]);
    assert_eq!(py_splitlines("a\n\nb"), ["a", "", "b"]);
    assert_eq!(py_splitlines("\n"), [""]);
    assert_eq!(py_splitlines("a\r\nb\rc\n"), ["a", "b", "c"]);
    assert_eq!(
        py_splitlines("a\u{b}b\u{c}c\u{1c}d\u{1d}e\u{1e}f\u{85}g\u{2028}h\u{2029}i"),
        ["a", "b", "c", "d", "e", "f", "g", "h", "i"]
    );
    assert_eq!(py_splitlines("a\u{1f}b\tc d"), ["a\u{1f}b\tc d"]);
}

// --- a configuration only git can weigh ---

#[test]
fn a_bare_variable_followed_by_a_comment_goes_to_git_and_git_refuses() {
    for line in ["foo ; why", "foo # why", "bare;", "filemode #"] {
        let it = fixture();
        let main = it.repository("main");
        let worktree = it.worktree(&main, "wt", "feature");
        append(&main.join(".git/config"), &format!("[core]\n\t{line}\n"));
        assert_eq!(it.left_to_git(&main), None, "{line}");
        assert_eq!(it.left_to_git(&worktree), None, "{line}");
        assert!(!it.git_knows_the_layout(&main));
    }
}

#[test]
fn a_variable_git_checks_when_it_starts_goes_to_git() {
    // values git refuses before it answers anything: the place is unknown
    for text in [
        "[core]\n\tfilemode = maybe\n",
        "[core]\n\tignorecase = maybe\n",
        "[core]\n\tsymlinks = maybe\n",
        "[core]\n\tprecomposeunicode = maybe\n",
        "[core]\n\tquotepath = maybe\n",
        "[core]\n\tabbrev = banana\n",
        "[core]\n\tcompression = 99\n",
        "[core]\n\tautocrlf = banana\n",
        "[core]\n\tcheckstat = banana\n",
        "[user]\n\tname\n",
        "[i18n]\n\tlogoutputencoding\n",
    ] {
        let it = fixture();
        let main = it.repository("main");
        let worktree = it.worktree(&main, "wt", "feature");
        append(&main.join(".git/config"), text);
        assert_eq!(it.left_to_git(&main), None, "{text:?}");
        assert_eq!(it.left_to_git(&worktree), None, "{text:?}");
    }
    // a value git reads only when it needs it: whatever git says then
    let it = fixture();
    let main = it.repository("main");
    append(
        &main.join(".git/config"),
        "[core]\n\tbigfilethreshold = x\n",
    );
    it.left_to_git(&main);
    // values that spoil the branch, and with a worktree to list sometimes the place too:
    // git never names a branch then
    for text in [
        "[core]\n\tlogallrefupdates = banana\n",
        "[core]\n\twarnambiguousrefs = maybe\n",
    ] {
        let it = fixture();
        let main = it.repository("main");
        append(&main.join(".git/config"), text);
        assert_eq!(it.left_to_git(&main), Some(at(&main, None)), "{text:?}");
        let it = fixture();
        let main = it.repository("main");
        let worktree = it.worktree(&main, "wt", "feature");
        append(&main.join(".git/config"), text);
        for dir in [&main, &worktree] {
            let answer = it.left_to_git(dir);
            assert!(
                answer.is_none_or(|facts| facts.branch.is_none()),
                "{text:?}"
            );
        }
    }
}

#[test]
fn a_variable_git_does_not_write_itself_goes_to_git() {
    // nothing wrong with any of these: they are simply not on the short list
    for text in [
        "[user]\n\tname = someone\n\temail = someone@example.com\n",
        "[alias]\n\tco = checkout\n",
        "[core]\n\tautocrlf = input\n",
        "[core]\n\twarnambiguousrefs = false\n",
        "[core]\n\tsharedrepository = group\n",
        "[core]\n\tlogallrefupdates = always\n",
        "[core \"x\"]\n\tfilemode = true\n",
        "[pull]\n\trebase = true\n",
        "[remote]\n\tpushdefault = origin\n",
        "[branch]\n\tautosetupmerge = always\n",
        "[submodule]\n\tactive = .\n",
        "[safe]\n\tdirectory = *\n",
        "[invented]\n",
    ] {
        let it = fixture();
        let main = it.repository("main");
        append(&main.join(".git/config"), text);
        assert_eq!(
            it.left_to_git(&main),
            Some(at(&main, Some("main"))),
            "{text:?}"
        );
    }
}

#[test]
fn the_variables_git_writes_itself_stay_with_the_files() {
    let it = fixture();
    let main = it.repository("main");
    let worktree = it.worktree(&main, "wt", "feature");
    // what `git init` wrote, in other spellings; what `git clone` and `git submodule`
    // add; a boolean without a value
    fs::write(
        main.join(".git/config"),
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = Yes\n\tbare = off\n\
         \tLogAllRefUpdates = 1\n\tignorecase\n\tprecomposeunicode = ON\n\tsymlinks = false\n\
         [remote \"origin\"]\n\turl = https://example.com/x.git\n\
         \tfetch = +refs/heads/*:refs/remotes/origin/*\n\tpromisor\n\
         [branch \"main\"]\n\tremote = origin\n\tmerge = refs/heads/main\n\trebase = banana\n\
         [submodule \"lib\"]\n\turl = ../lib\n\tactive = true\n",
    )
    .unwrap();
    assert_eq!(it.agreed(&main), at(&main, Some("main")));
    assert_eq!(it.agreed(&worktree), at(&main, Some("feature")));
}

// --- a packed-refs file and a git directory as git itself reads them ---

#[test]
fn a_packed_refs_header_git_does_not_write_goes_to_git() {
    for header in [
        "# garbage header\n",
        "#\n",
        "# pack-refs with\n",
        "#pack-refs with: sorted \n",
    ] {
        let it = fixture();
        let main = it.repository("main");
        fs::write(main.join(".git/packed-refs"), header).unwrap();
        // the branch is there as a loose ref; git still reads the file, and stumbles
        assert_eq!(it.left_to_git(&main), Some(at(&main, None)), "{header:?}");
    }
    // a header with a trait this code does not know is still git's header
    let it = fixture();
    let main = it.repository("main");
    fs::write(
        main.join(".git/packed-refs"),
        "# pack-refs with: unknown-trait \n",
    )
    .unwrap();
    assert_eq!(it.agreed(&main), at(&main, Some("main")));
}

#[test]
fn packed_refs_that_claim_to_be_sorted_and_are_not_go_to_git() {
    let it = fixture();
    let main = it.repository("main");
    for name in ["zz", "aa", "mm"] {
        it.git(&main, &["branch", name]);
    }
    it.git(&main, &["pack-refs", "--all"]);
    let packed = main.join(".git/packed-refs");
    let written = fs::read_to_string(&packed).unwrap();
    let mut lines: Vec<&str> = written.lines().collect();
    assert!(lines[0].contains(" sorted "), "{}", lines[0]);
    // in order: every branch is found by the files as by git
    for name in ["zz", "aa", "mm", "main"] {
        fs::write(main.join(".git/HEAD"), format!("ref: refs/heads/{name}\n")).unwrap();
        assert_eq!(it.agreed(&main), at(&main, Some(name)), "{name}");
    }
    // reversed below a header that says sorted: git searches by halves and misses
    lines[1..].reverse();
    fs::write(&packed, format!("{}\n", lines.join("\n"))).unwrap();
    for name in ["zz", "aa", "mm"] {
        fs::write(main.join(".git/HEAD"), format!("ref: refs/heads/{name}\n")).unwrap();
        assert_eq!(it.left_to_git(&main), Some(at(&main, None)), "{name}");
    }
    // the same line twice is not in order either
    fs::write(
        &packed,
        format!("{written}{}\n", written.lines().last().unwrap()),
    )
    .unwrap();
    assert_eq!(from_files(&main, &it.env), Confirmed::Unsure);
    // without the claim git sorts what it reads, and so any order will do
    let unsorted = format!("{}\n", lines.join("\n")).replace(" sorted ", " ");
    fs::write(&packed, unsorted).unwrap();
    for name in ["zz", "aa", "mm"] {
        fs::write(main.join(".git/HEAD"), format!("ref: refs/heads/{name}\n")).unwrap();
        assert_eq!(it.agreed(&main), at(&main, Some(name)), "{name}");
    }
}

#[test]
fn a_git_directory_whose_objects_cannot_be_searched_goes_to_git() {
    for (directory, mode) in [("objects", 0o000), ("objects", 0o600), ("refs", 0o600)] {
        let it = fixture();
        let main = it.repository("main");
        let worktree = it.worktree(&main, "wt", "feature");
        let locked = main.join(".git").join(directory);
        fs::set_permissions(&locked, fs::Permissions::from_mode(mode)).unwrap();
        let stopped = fs::read_dir(locked.join("x"))
            .is_err_and(|err| err.kind() == std::io::ErrorKind::PermissionDenied);
        let answers = (
            stopped,
            it.left_to_git_or_skip(&main, stopped),
            it.left_to_git_or_skip(&worktree, stopped),
        );
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
        if stopped {
            // without the right to search it, it is no git directory to git
            assert_eq!(answers, (true, None, None), "{directory} {mode:o}");
        }
    }
}
