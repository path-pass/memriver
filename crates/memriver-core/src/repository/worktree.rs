//! Where a directory inside a git repository belongs, and which branch is checked out.
//!
//! A session started in a linked worktree registers under the project of the
//! repository's main working tree, keeping the sub-directory it started in. Git is
//! consulted only when a `.git` entry exists somewhere above the directory.
//!
//! The answer is read from the repository's own files when its layout is one this code
//! can positively confirm (`from_files`), and asked of `git` in every other case
//! (`from_git`). A doubt is never resolved by a guess: the files say "unsure" and git is
//! asked, and when git cannot answer the answer is "unknown".

pub mod config;

use std::collections::HashSet;
use std::ffi::OsStr;
use std::fs::{self, Metadata};
use std::io::{ErrorKind, Read};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use super::directories::{canonical, same_directory};
use crate::models::helpers::py_normpath;
use crate::settings::{Env, GIT_QUERY_TIMEOUT_S};

/// What git knows about a directory.
///
/// `main_tree` is the directory's place in the main working tree: the directory itself
/// outside git and in a main working tree, its twin below the main tree's root inside a
/// linked worktree (a path that need not exist). `branch` is the checked-out branch,
/// `HEAD` when detached, None outside git and when it cannot be told.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitFacts {
    pub main_tree: PathBuf,
    pub branch: Option<String>,
}

/// An answer read from files: confirmed, or left to git. `Unsure` is the only way to
/// say "use git".
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Confirmed<T> {
    Yes(T),
    Unsure,
}

/// The git facts of `dir`; None when it is not an existing directory or when its place
/// cannot be told for sure. A branch that cannot be told leaves the place known.
///
/// Only the canonical spelling is ever used: an alias (a symlink) walks the ancestors of
/// where it points, never of where it is written.
pub fn git_facts(dir: &Path, env: &Env) -> Option<GitFacts> {
    facts(dir, env, Duration::from_secs(GIT_QUERY_TIMEOUT_S))
}

fn facts(dir: &Path, env: &Env, timeout: Duration) -> Option<GitFacts> {
    let dir = canonical(dir)?;
    if !has_git_entry(&dir)? {
        return Some(GitFacts {
            main_tree: dir,
            branch: None,
        });
    }
    match from_files(&dir, env) {
        Confirmed::Yes(facts) => Some(facts),
        Confirmed::Unsure => from_git(&dir, env, timeout),
    }
}

/// A `.git` entry at `path` or above; None when the look itself failed.
fn has_git_entry(path: &Path) -> Option<bool> {
    for ancestor in path.ancestors() {
        match fs::symlink_metadata(ancestor.join(".git")) {
            Ok(_) => return Some(true),
            Err(err) if matches!(err.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {}
            Err(_) => return None,
        }
    }
    Some(false)
}

// --- from the repository's files ---

/// The facts of `dir` (canonical, with a `.git` entry at or above it) read from the
/// repository's files, when every one of the conditions holds that make the layout one
/// git would answer for in the same way.
pub(crate) fn from_files(dir: &Path, env: &Env) -> Confirmed<GitFacts> {
    // SAFETY: no arguments, cannot fail
    let user = unsafe { libc::geteuid() };
    match read_files(dir, env, user) {
        Some(facts) => Confirmed::Yes(facts),
        None => Confirmed::Unsure,
    }
}

/// The characters Python's `str.splitlines` breaks a line at. git's answers are read
/// line by line, so a path holding one of them is not an answer git can give.
const LINE_BREAKS: [char; 10] = [
    '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}', '\u{2029}',
];

fn one_line(path: &Path) -> bool {
    path.to_str()
        .is_some_and(|text| !text.contains(LINE_BREAKS))
}

fn is_object_id(text: &[u8]) -> bool {
    text.len() == 40
        && text
            .iter()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// What `HEAD` names.
#[derive(Debug, PartialEq, Eq)]
enum Head {
    Branch(String),
    Detached,
}

/// `HEAD` as git itself writes it: `ref: refs/heads/<name>` or an object id, one line.
/// Anything else -- empty, cut short, another kind of ref, a link -- is not read here.
fn head(git_dir: &Path) -> Option<Head> {
    let content = regular_file(&git_dir.join("HEAD"))?;
    let line = content.strip_suffix(b"\n")?;
    if is_object_id(line) {
        return Some(Head::Detached);
    }
    let name = std::str::from_utf8(line.strip_prefix(b"ref: refs/heads/")?).ok()?;
    plain_branch_name(name).then(|| Head::Branch(name.to_string()))
}

/// A branch name with nothing in it that git reads specially or prints differently.
fn plain_branch_name(name: &str) -> bool {
    let plain_part = |part: &str| {
        !part.is_empty()
            && !part.starts_with('.')
            && !part.ends_with('.')
            && !part.ends_with(".lock")
    };
    name.split('/').all(plain_part)
        && !name.starts_with('-')
        && name != "@"
        && !name.contains("..")
        && !name.contains("@{")
        && !name.contains(|c: char| c <= ' ' || c == '\u{7f}' || "~^:?*[\\".contains(c))
        && !name.contains(LINE_BREAKS)
        && !is_object_id(name.as_bytes())
}

/// The content of a regular file; None for anything else, a link included.
fn regular_file(path: &Path) -> Option<Vec<u8>> {
    fs::symlink_metadata(path).ok()?.is_file().then_some(())?;
    fs::read(path).ok()
}

/// Whether something stands at `path`: `Some(false)` only when it is known to be absent.
fn present(path: &Path) -> Option<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Some(true),
        Err(err) if matches!(err.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {
            Some(false)
        }
        Err(_) => None,
    }
}

/// The ref names of a `packed-refs` file; an empty set when there is none. None for a
/// file that cannot be read or holds a line that is not one git writes.
// ponytail: reads the whole file; a search in the sorted file when repositories with
// hundreds of thousands of refs are slow here
fn packed_refs(common: &Path) -> Option<HashSet<Vec<u8>>> {
    let content = match fs::read(common.join("packed-refs")) {
        Ok(content) => content,
        Err(err) if err.kind() == ErrorKind::NotFound => return Some(HashSet::new()),
        Err(_) => return None,
    };
    let mut names = HashSet::new();
    if content.is_empty() {
        return Some(names);
    }
    let mut after_ref = false;
    for (number, line) in content
        .strip_suffix(b"\n")?
        .split(|byte| *byte == b'\n')
        .enumerate()
    {
        match line {
            [b'#', ..] if number == 0 => {}
            [b'^', peeled @ ..] if after_ref && is_object_id(peeled) => after_ref = false,
            line if line.len() > 41 && line[40] == b' ' && is_object_id(&line[..40]) => {
                let name = &line[41..];
                if name.iter().any(|byte| *byte <= b' ') {
                    return None;
                }
                names.insert(name.to_vec());
                after_ref = true;
            }
            _ => return None,
        }
    }
    Some(names)
}

/// The branch `git rev-parse --abbrev-ref HEAD` prints for this `HEAD`: `Some(None)`
/// when the branch has no commit yet (git has no answer then); None when unsure.
fn branch(head: Head, git_dir: &Path, common: &Path) -> Option<Option<String>> {
    let name = match head {
        Head::Detached => return Some(Some("HEAD".to_string())),
        Head::Branch(name) => name,
    };
    let packed = packed_refs(common)?;
    let full = format!("refs/heads/{name}");
    let loose = common.join(&full);
    let exists = match fs::symlink_metadata(&loose) {
        // read and validated, not just found
        Ok(found) if found.is_file() => {
            let content = fs::read(&loose).ok()?;
            is_object_id(content.strip_suffix(b"\n")?).then_some(true)?
        }
        Ok(_) => return None,
        Err(err) if err.kind() == ErrorKind::NotFound => packed.contains(full.as_bytes()),
        Err(_) => return None,
    };
    if !exists {
        // unborn: both lookups succeeded and found nothing
        return Some(None);
    }
    // git prints a longer spelling when the short name could mean another ref
    for other in [
        name.clone(),
        format!("refs/{name}"),
        format!("refs/tags/{name}"),
        format!("refs/remotes/{name}"),
        format!("refs/remotes/{name}/HEAD"),
    ] {
        if packed.contains(other.as_bytes())
            || present(&git_dir.join(&other))?
            || present(&common.join(&other))?
        {
            return None;
        }
    }
    Some(Some(name))
}

/// Whether the repository's configuration says nothing that changes what a working tree
/// is or how refs are stored: not bare, no `core.worktree`, no include, format version 0
/// and no extension at all.
fn plain_configuration(common: &Path) -> Option<bool> {
    let text = String::from_utf8(fs::read(common.join("config")).ok()?).ok()?;
    let config = config::parse(&text)?;
    Some(
        !["include", "includeif", "extensions"]
            .iter()
            .any(|section| config.has_section(section))
            && config
                .values("core", "bare")
                .all(|value| config::boolean(value) == Some(false))
            && config.values("core", "worktree").next().is_none()
            && config
                .values("core", "repositoryformatversion")
                .all(|value| value == Some("0")),
    )
}

/// The path the system itself gives for the open directory `dir`.
#[cfg(target_os = "macos")]
fn spelled_by_the_system(dir: &Path) -> Option<PathBuf> {
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStringExt;
    let opened = fs::File::open(dir).ok()?;
    let mut buffer = vec![0u8; libc::PATH_MAX as usize];
    // SAFETY: F_GETPATH writes at most PATH_MAX bytes, a terminating NUL included
    let got = unsafe { libc::fcntl(opened.as_raw_fd(), libc::F_GETPATH, buffer.as_mut_ptr()) };
    if got == -1 {
        return None;
    }
    buffer.truncate(buffer.iter().position(|byte| *byte == 0)?);
    Some(PathBuf::from(std::ffi::OsString::from_vec(buffer)))
}

#[cfg(not(target_os = "macos"))]
fn spelled_by_the_system(dir: &Path) -> Option<PathBuf> {
    use std::os::fd::AsRawFd;
    let opened = fs::File::open(dir).ok()?;
    fs::read_link(format!("/proc/self/fd/{}", opened.as_raw_fd())).ok()
}

fn read_files(dir: &Path, env: &Env, user: u32) -> Option<GitFacts> {
    // 1: the hook's environment names no other repository
    if ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"]
        .iter()
        .any(|name| env.contains_key(*name))
    {
        return None;
    }
    // git refuses a repository another user owns
    let owned = |found: &Metadata| found.uid() == user;
    // 2: the nearest `.git`, on the file system `dir` is on and with no git directory on
    // the way up (inside one, git has no working tree to name)
    let device = fs::symlink_metadata(dir).ok()?.dev();
    let (toplevel, entry) = dir.ancestors().find_map(|ancestor| {
        let here = fs::symlink_metadata(ancestor)
            .ok()
            .filter(|here| here.dev() == device);
        match (here, fs::symlink_metadata(ancestor.join(".git"))) {
            (Some(here), Ok(entry)) => Some(owned(&here).then_some((ancestor, entry))),
            (Some(_), Err(err)) if err.kind() == ErrorKind::NotFound => {
                present(&ancestor.join("HEAD"))
                    .is_none_or(|found| found)
                    .then_some(None)
            }
            _ => Some(None),
        }
    })??;
    owned(&entry).then_some(())?;
    let git_dir = if entry.is_dir() {
        toplevel.join(".git")
    } else {
        // a file whose single line is `gitdir: <path>`, naming an existing directory
        let content = String::from_utf8(regular_file(&toplevel.join(".git"))?).ok()?;
        let named = content.strip_prefix("gitdir: ")?;
        let named = named.strip_suffix('\n').unwrap_or(named);
        (!named.is_empty() && !named.contains(LINE_BREAKS)).then_some(())?;
        canonical(&toplevel.join(named))?
    };
    owned(&fs::symlink_metadata(&git_dir).ok()?).then_some(())?;
    // 4: a linked worktree's git directory names the shared one in `commondir`
    let common = match fs::read(git_dir.join("commondir")) {
        Err(err) if err.kind() == ErrorKind::NotFound => None,
        Err(_) => return None,
        Ok(content) => {
            let named = content.strip_suffix(b"\n").unwrap_or(&content);
            (!named.is_empty() && !named.contains(&b'\n') && !named.contains(&0)).then_some(())?;
            Some(canonical(&git_dir.join(OsStr::from_bytes(named)))?)
        }
    };
    let main = match &common {
        None => None,
        Some(common) => {
            let main = common.parent()?;
            let ordinary = common.file_name() == Some(OsStr::new(".git"))
                && git_dir.parent().map(Path::as_os_str)
                    == Some(common.join("worktrees").as_os_str())
                && owned(&fs::symlink_metadata(common).ok()?)
                && owned(&fs::symlink_metadata(main).ok()?)
                // git asks the main working tree too, and answers only where its own
                // HEAD is one it reads
                && head(common).is_some()
                // git names the worktree's root as the system spells it; under another
                // spelling of the same directory no sub-directory can be read off
                && spelled_by_the_system(dir)?.as_os_str() == dir.as_os_str();
            ordinary.then_some(main)?;
            Some(main)
        }
    };
    let common = common.as_deref().unwrap_or(&git_dir);
    // git reads its answers line by line
    (one_line(dir) && one_line(&git_dir) && one_line(common)).then_some(())?;
    // 3, 8: nothing in the configuration that only git can weigh
    plain_configuration(common)?.then_some(())?;
    // what makes a directory a git directory, besides its HEAD
    let is_directory =
        |name: &str| fs::metadata(common.join(name)).is_ok_and(|found| found.is_dir());
    (is_directory("objects") && is_directory("refs")).then_some(())?;
    // 5, 6, 7
    let branch = branch(head(&git_dir)?, &git_dir, common)?;
    let main_tree = match main {
        // the same place below the main working tree's root, which need not exist there
        Some(main) => dir
            .strip_prefix(toplevel)
            .ok()?
            .iter()
            .fold(main.to_path_buf(), |mapped, part| mapped.join(part)),
        None => dir.to_path_buf(),
    };
    Some(GitFacts { main_tree, branch })
}

// --- from git ---

#[cfg(test)]
thread_local! {
    /// How many times this thread started git.
    static GIT_RUNS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// Python's `str.splitlines`.
fn py_splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut rest = text;
    while !rest.is_empty() {
        let Some(at) = rest.find(LINE_BREAKS) else {
            lines.push(rest);
            break;
        };
        lines.push(&rest[..at]);
        let after = &rest[at..];
        let width = if after.starts_with("\r\n") {
            2
        } else {
            after.chars().next().map_or(1, char::len_utf8)
        };
        rest = &after[width..];
    }
    lines
}

/// Stdout of `git -C cwd <args>`; None on any failure: git is not installed, it does not
/// finish in time, it exits with an error, or its answer is not text.
///
/// Every `GIT_*` variable of the caller's environment is dropped: a `GIT_DIR` or
/// `GIT_WORK_TREE` left there would make git answer for another repository.
fn git(args: &[&str], cwd: &Path, env: &Env, timeout: Duration) -> Option<String> {
    #[cfg(test)]
    GIT_RUNS.set(GIT_RUNS.get() + 1);
    let mut child = Command::new("git")
        .arg("-C")
        .arg(cwd)
        .args(args)
        .env_clear()
        .envs(env.iter().filter(|(name, _)| !name.starts_with("GIT_")))
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_TERMINAL_PROMPT", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + timeout;
    let mut stdout = child.stdout.take()?;
    let (send, receive) = mpsc::channel();
    std::thread::spawn(move || {
        let mut output = Vec::new();
        let read = stdout.read_to_end(&mut output);
        let _ = send.send(read.map(|_| output));
    });
    let left = || deadline.saturating_duration_since(Instant::now());
    let output = receive.recv_timeout(left());
    // the output ended; the exit follows at once unless git handed its end to another
    // process
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if output.is_ok() && !left().is_zero() => {
                std::thread::sleep(Duration::from_micros(200));
            }
            _ => break None,
        }
    };
    let Some(status) = status else {
        let _ = child.kill();
        let _ = child.wait();
        return None;
    };
    let text = String::from_utf8(output.ok()?.ok()?).ok()?;
    // as Python reads a text stream: every line end is a line feed
    status
        .success()
        .then(|| text.replace("\r\n", "\n").replace('\r', "\n"))
}

/// [toplevel, git dir, common dir]: exactly three non-empty absolute paths, else None.
fn locate(cwd: &Path, env: &Env, timeout: Duration) -> Option<[String; 3]> {
    let args = [
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
    ];
    let output = git(&args, cwd, env, timeout)?;
    let lines: [&str; 3] = py_splitlines(&output).try_into().ok()?;
    lines
        .iter()
        .all(|line| line.starts_with('/'))
        .then(|| lines.map(str::to_string))
}

fn parts(path: &[u8]) -> Vec<&[u8]> {
    path.split(|byte| *byte == b'/')
        .filter(|part| !part.is_empty())
        .collect()
}

/// The facts of `dir` (canonical, with a `.git` entry at or above it) as git gives them;
/// None when the layout cannot be told.
pub(crate) fn from_git(dir: &Path, env: &Env, timeout: Duration) -> Option<GitFacts> {
    let [toplevel, git_dir, common_dir] = locate(dir, env, timeout)?;
    // a main working tree, which includes a separate git directory and submodules
    let main_tree = if git_dir == common_dir {
        dir.to_path_buf()
    } else {
        let listing = git(
            &["worktree", "list", "--porcelain", "-z"],
            dir,
            env,
            timeout,
        )?;
        // NUL-separated fields, an empty field ending each record: a path with a line
        // break cannot split a record
        let first: Vec<&str> = listing
            .split('\0')
            .take_while(|field| !field.is_empty())
            .collect();
        let candidates: Vec<&str> = first
            .iter()
            .filter_map(|field| field.strip_prefix("worktree "))
            .collect();
        let [main] = candidates[..] else {
            return None;
        };
        if first.contains(&"bare") {
            return None;
        }
        // the first record of a repository with a separate git directory is its metadata
        // directory, not a working tree: only the same query run there proves it.
        // Known limitation: a metadata directory itself named <X>/.git is listed as <X>,
        // git answers in <X> as a working tree of the same repository, and a linked
        // worktree then maps into <X>
        let confirmed = locate(Path::new(main), env, timeout)?;
        if same_directory(Path::new(&confirmed[0]), Path::new(main)) != Some(true)
            || same_directory(Path::new(&confirmed[2]), Path::new(&common_dir)) != Some(true)
        {
            return None;
        }
        // git spelled the toplevel differently from `dir` (a symlink or a case alias):
        // no sub-directory can be read off the two strings
        let (toplevel, dir) = (
            py_normpath(toplevel.as_bytes()),
            py_normpath(dir.as_os_str().as_bytes()),
        );
        let relative = parts(&dir).strip_prefix(&parts(&toplevel)[..])?.to_vec();
        let mut mapped: PathBuf = Path::new(main).components().collect();
        mapped.extend(relative.into_iter().map(OsStr::from_bytes));
        mapped
    };
    let branch =
        git(&["rev-parse", "--abbrev-ref", "HEAD"], dir, env, timeout).and_then(|output| {
            match py_splitlines(&output)[..] {
                [line] if !line.is_empty() => Some(line.to_string()),
                _ => None,
            }
        });
    Some(GitFacts { main_tree, branch })
}

#[cfg(test)]
mod tests;
