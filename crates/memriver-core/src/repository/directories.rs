//! Directory facts and rules shared by every store: which bound directory a path lies
//! under, whether a directory would swallow the home directory or the store, and deleting
//! a whole store directory without ever following a symlink.
//!
//! Paths are resolved here by walking them one component at a time, as Python's
//! `os.path.realpath` does, and never by the system's own `realpath`: a spelling is kept
//! as it was written wherever no link is read, so two names for one directory stay two
//! names and are compared by identity, and a failure names the component it happened at.

use std::collections::HashMap;
use std::ffi::{CStr, CString, OsStr, OsString};
use std::fmt;
use std::fs::{self, Metadata};
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use crate::models::errors::named;
use crate::models::helpers::{py_normpath, py_repr};

/// The one file name the database lives at, shared here so a purge target can be
/// recognized as a store.
pub const DATABASE_FILENAME: &str = "memriver.db";

pub const UNRESOLVABLE_START: &str = "working directory could not be resolved";

// --- one failed system call ---

/// A system call that failed on a path: the error number and the path it was asked
/// about. A path the system cannot even address (an embedded NUL) has no number.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OsFailure {
    pub errno: Option<i32>,
    pub path: PathBuf,
}

impl OsFailure {
    fn new(err: &io::Error, path: &Path) -> OsFailure {
        OsFailure {
            errno: err.raw_os_error(),
            path: path.to_path_buf(),
        }
    }

    /// Nothing stands there: a component is missing, or one on the way is no directory.
    fn is_absent(&self) -> bool {
        matches!(self.errno, Some(libc::ENOENT | libc::ENOTDIR))
    }

    fn is_missing(&self) -> bool {
        self.errno == Some(libc::ENOENT)
    }

    /// The system's words for the error, without the number.
    pub fn reason(&self) -> String {
        self.errno
            .map_or_else(|| "embedded null byte".to_string(), strerror)
    }
}

/// `[Errno 13] Permission denied: '/the/path'` -- the line a user is shown.
impl fmt::Display for OsFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.errno {
            Some(errno) => write!(
                f,
                "[Errno {errno}] {}: {}",
                strerror(errno),
                py_repr(&self.path.to_string_lossy())
            ),
            None => f.write_str("embedded null byte"),
        }
    }
}

fn strerror(errno: i32) -> String {
    let text = io::Error::from_raw_os_error(errno).to_string();
    let suffix = format!(" (os error {errno})");
    text.strip_suffix(&suffix).unwrap_or(&text).to_string()
}

#[cfg(test)]
thread_local! {
    /// Failures a test plants: (follow links, the path, the error number, only once).
    static FAULTS: std::cell::RefCell<Vec<(bool, PathBuf, i32, bool)>> =
        const { std::cell::RefCell::new(Vec::new()) };
}

#[cfg(test)]
fn planted(follow: bool, path: &Path) -> Option<i32> {
    FAULTS.with_borrow_mut(|faults| {
        let at = faults
            .iter()
            .position(|(f, p, _, _)| *f == follow && p.as_os_str() == path.as_os_str())?;
        let errno = faults[at].2;
        if faults[at].3 {
            faults.remove(at);
        }
        Some(errno)
    })
}

/// What stands at `path`, following links (`stat`) or not (`lstat`). Every look this
/// module takes at a name goes through here.
fn look(path: &Path, follow: bool) -> Result<Metadata, OsFailure> {
    #[cfg(test)]
    if let Some(errno) = planted(follow, path) {
        return Err(OsFailure {
            errno: Some(errno),
            path: path.to_path_buf(),
        });
    }
    let found = if follow {
        fs::metadata(path)
    } else {
        fs::symlink_metadata(path)
    };
    found.map_err(|err| OsFailure::new(&err, path))
}

fn stat(path: &Path) -> Result<Metadata, OsFailure> {
    look(path, true)
}

fn lstat(path: &Path) -> Result<Metadata, OsFailure> {
    look(path, false)
}

fn path_of(bytes: Vec<u8>) -> PathBuf {
    PathBuf::from(OsString::from_vec(bytes))
}

// --- resolving a path ---

/// One pass of the walk: `rest` resolved from `path`, which is absolute and resolved.
fn join_real(
    mut path: PathBuf,
    mut rest: &[u8],
    strict: bool,
    seen: &mut HashMap<PathBuf, Option<PathBuf>>,
) -> Result<PathBuf, OsFailure> {
    if rest.first() == Some(&b'/') {
        path = PathBuf::from("/");
    }
    while !rest.is_empty() {
        let end = rest.iter().position(|byte| *byte == b'/');
        let name = &rest[..end.unwrap_or(rest.len())];
        rest = end.map_or(&[], |end| &rest[end + 1..]);
        match name {
            b"" | b"." => continue,
            b".." => {
                path.pop();
                continue;
            }
            _ => {}
        }
        let newpath = path.join(OsStr::from_bytes(name));
        let found = match lstat(&newpath) {
            Ok(found) => found,
            Err(failure) if strict => return Err(failure),
            // what cannot be looked at is taken as written
            Err(_) => {
                path = newpath;
                continue;
            }
        };
        if !found.file_type().is_symlink() {
            // nothing lies below what is no directory
            let more = rest.split(|byte| *byte == b'/');
            if strict && !found.is_dir() && more.into_iter().any(|name| !matches!(name, b"" | b"."))
            {
                return Err(OsFailure {
                    errno: Some(libc::ENOTDIR),
                    path: newpath,
                });
            }
            path = newpath;
            continue;
        }
        match seen.get(&newpath) {
            Some(Some(resolved)) => {
                path = resolved.clone();
                continue;
            }
            // the link is being resolved right now: a loop
            Some(None) if strict => {
                return Err(stat(&newpath).err().unwrap_or(OsFailure {
                    errno: Some(libc::ELOOP),
                    path: newpath,
                }));
            }
            Some(None) => {
                path = newpath;
                continue;
            }
            None => {}
        }
        let target = match fs::read_link(&newpath) {
            Ok(target) => target,
            Err(err) if strict => return Err(OsFailure::new(&err, &newpath)),
            Err(_) => {
                path = newpath;
                continue;
            }
        };
        seen.insert(newpath.clone(), None);
        path = join_real(path, target.as_os_str().as_bytes(), strict, seen)?;
        seen.insert(newpath, Some(path.clone()));
    }
    Ok(path)
}

/// Python's `os.path.realpath`: `path` with every link on the way read. Strict, anything
/// that is missing or cannot be looked at is a failure; otherwise the walk goes on with
/// the name as written, and only a path the system cannot address fails.
pub fn realpath(path: &Path, strict: bool) -> Result<PathBuf, OsFailure> {
    let bytes = path.as_os_str().as_bytes();
    if bytes.contains(&0) {
        return Err(OsFailure {
            errno: None,
            path: path.to_path_buf(),
        });
    }
    let start = if bytes.first() == Some(&b'/') {
        PathBuf::from("/")
    } else {
        std::env::current_dir().map_err(|err| OsFailure::new(&err, path))?
    };
    let resolved = join_real(start, bytes, strict, &mut HashMap::new())?;
    Ok(path_of(py_normpath(resolved.as_os_str().as_bytes())))
}

/// The strict real path of an existing directory; None for anything else. Never fails:
/// the hooks take a start directory from the harness payload.
pub fn canonical(path: &Path) -> Option<PathBuf> {
    let canonical = realpath(path, true).ok()?;
    stat(&canonical)
        .is_ok_and(|found| found.is_dir())
        .then_some(canonical)
}

/// An absolute path a harness payload may name.
pub fn usable_directory(value: &str) -> bool {
    value.starts_with('/') && !value.contains('\0')
}

/// Python's `os.path.dirname`.
fn dirname(path: &str) -> &str {
    let head = &path[..path.rfind('/').map_or(0, |at| at + 1)];
    match head.trim_end_matches('/') {
        "" => head,
        trimmed => trimmed,
    }
}

/// A Claude Code /cd: the transcript now lives under another projects directory.
///
/// The only signal that tells /cd from a shell cd, which keeps the transcript where it
/// is. Spellings of one directory (a symlink, `/./`) compare equal.
pub fn transcript_moved(stored: &str, current: &str) -> bool {
    let real = |path: &str| realpath(Path::new(dirname(path)), false);
    usable_directory(stored) && usable_directory(current) && real(stored) != real(current)
}

// --- one directory under two names ---

/// Whether `a` and `b` are one directory, with "absent" and "could not check" told
/// apart.
///
/// `Some(false)`: both exist and differ, one of them does not exist (an offline root is
/// not a match), or one cannot be addressed at all. `None`: the check itself failed
/// (permission, I/O) -- no caller may read that as "no match". The first path is looked
/// at first, and what happens there decides before the second is looked at.
pub fn same_directory(a: &Path, b: &Path) -> Option<bool> {
    let mut identities = [(0, 0); 2];
    for (path, identity) in [a, b].into_iter().zip(&mut identities) {
        match stat(path) {
            Ok(found) => *identity = (found.dev(), found.ino()),
            Err(failure) if failure.is_absent() || failure.errno.is_none() => return Some(false),
            Err(_) => return None,
        }
    }
    Some(identities[0] == identities[1])
}

/// `outer` is `inner` or one of its ancestors, by its text or by `same_directory`.
/// `None` as soon as one comparison could not be checked.
pub fn covers(outer: &Path, inner: &Path) -> Option<bool> {
    for candidate in inner.ancestors() {
        if outer.as_os_str() == candidate.as_os_str() || same_directory(outer, candidate)? {
            return Some(true);
        }
    }
    Some(false)
}

named! {
    /// How a bound root stands on disk now.
    RootState {
        Ok = "ok",
        Missing = "missing",
        NotCanonical = "not-canonical",
        Unverifiable = "unverifiable",
    }
}

/// How a bound root stands on disk now.
///
/// The nearest existing path among the root and its ancestors is what a redirect would
/// have replaced: checking only the leaf would let a symlinked ancestor with a missing
/// leaf pass as an offline root. A root with no existing component, or whose leaf is
/// absent or not a directory, is offline (`Missing`), which never degrades resolution.
pub fn root_state(root: &str) -> RootState {
    let root = Path::new(root);
    let mut existing = None;
    for candidate in root.ancestors() {
        match lstat(candidate) {
            Ok(_) => {
                existing = Some(candidate);
                break;
            }
            Err(failure) if failure.is_missing() => {}
            // the look itself failed somewhere on the way up: not offline, not checkable
            Err(_) => return RootState::Unverifiable,
        }
    }
    let Some(existing) = existing else {
        return RootState::Missing;
    };
    match realpath(existing, false) {
        Ok(real) if real.as_os_str() == existing.as_os_str() => {}
        Ok(_) => return RootState::NotCanonical,
        Err(_) => return RootState::Unverifiable,
    }
    if existing.as_os_str() != root.as_os_str() {
        return RootState::Missing;
    }
    match stat(root) {
        Ok(found) if found.is_dir() => RootState::Ok,
        Ok(_) => RootState::Missing,
        Err(failure) if failure.is_missing() => RootState::Missing,
        Err(_) => RootState::Unverifiable,
    }
}

/// A diagnostic when any root was re-pointed or cannot be checked; None otherwise.
pub fn integrity_diagnostic(roots: &[&str]) -> Option<String> {
    roots.iter().find_map(|root| match root_state(root) {
        RootState::Unverifiable => Some(format!("{root}: registered root could not be checked")),
        RootState::NotCanonical => Some(format!(
            "{root}: registered root is no longer a canonical path"
        )),
        RootState::Ok | RootState::Missing => None,
    })
}

/// Which project a directory belongs to: its id, none, or why that cannot be told.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Match {
    Registered(String),
    None,
    Degraded(String),
}

/// The project `start` belongs to among `(project id, root)` pairs.
///
/// Integrity first: one re-pointed or uncheckable root degrades everything, never a
/// silent fall-through to a parent project or to none. Then the nearest ancestor wins,
/// with exact and same-directory matches pooled per level: roots that differ as text
/// can still be one directory.
///
/// `logical` (a linked worktree's path mapped onto its main tree): `start` must still be
/// a real directory, but the walk runs over `logical`, which need not exist --
/// `same_directory` already answers "no" for an absent level and "could not check" for
/// one it cannot look at.
pub fn nearest_bound(start: &Path, bound: &[(String, String)], logical: Option<&Path>) -> Match {
    let Some(canonical) = canonical(start) else {
        return Match::Degraded(UNRESOLVABLE_START.to_string());
    };
    let roots: Vec<&str> = bound.iter().map(|(_, root)| root.as_str()).collect();
    if let Some(diagnostic) = integrity_diagnostic(&roots) {
        return Match::Degraded(diagnostic);
    }
    for key in logical.unwrap_or(&canonical).ancestors() {
        let mut ids: Vec<&str> = Vec::new();
        for (project_id, root) in bound {
            let same = root.as_bytes() == key.as_os_str().as_bytes()
                || match same_directory(Path::new(root), key) {
                    Some(same) => same,
                    None => {
                        return Match::Degraded(format!(
                            "{root}: registered root could not be checked"
                        ));
                    }
                };
            if same && !ids.contains(&project_id.as_str()) {
                ids.push(project_id);
            }
        }
        match ids[..] {
            [] => {}
            [project_id] => return Match::Registered(project_id.to_string()),
            _ => {
                return Match::Degraded(format!(
                    "{}: matched by more than one project",
                    key.display()
                ));
            }
        }
    }
    Match::None
}

// --- deleting a whole store ---

named! {
    PurgeRefusalKind {
        Unresolvable = "unresolvable",
        Symlink = "symlink",
        TooBroad = "too-broad",
        NotDirectory = "not-directory",
        Unopenable = "unopenable",
        NotAStore = "not-a-store",
    }
}

/// Why a purge target is refused; nothing was removed.
///
/// `path` is what the refusal is about (the given link, the base that failed to resolve,
/// or the canonical target); `canonical` is the target once it resolved, None when it
/// never did; `detail` is the system's reason for `Unresolvable` and `Unopenable`, for
/// the caller's message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PurgeRefusal {
    pub kind: PurgeRefusalKind,
    pub path: PathBuf,
    pub canonical: Option<PathBuf>,
    pub detail: String,
}

impl PurgeRefusal {
    fn at(kind: PurgeRefusalKind, canonical: &Path, detail: String) -> PurgeRefusal {
        PurgeRefusal {
            kind,
            path: canonical.to_path_buf(),
            canonical: Some(canonical.to_path_buf()),
            detail,
        }
    }
}

/// A directory as an object: what a name must still lead to for it to be the same one.
type Identity = (u64, u64);

/// A purge target that passed every check, held open while the user decides.
///
/// The open directory binds the deletion to the object shown, so the prompt is not a
/// window for swapping the target. Dropping the plan closes it, whatever the answer.
#[derive(Debug)]
pub struct PurgePlan {
    pub given: PathBuf,
    pub canonical: PathBuf,
    pub home: PathBuf,
    pub cwd: PathBuf,
    pub exists: bool,
    opened: Option<(OwnedFd, Identity)>,
}

impl PurgePlan {
    /// Whether the target is held open for deletion: not on a dry run, not when there is
    /// nothing to remove.
    pub fn is_open(&self) -> bool {
        self.opened.is_some()
    }
}

named! {
    PurgeOutcome {
        Removed = "removed",
        UnopenableParent = "unopenable-parent",
        Uncheckable = "uncheckable",
        NotConfirmed = "not-confirmed",
        Refused = "refused",
        PartlyRemoved = "partly-removed",
        ReplacedRoot = "replaced-root",
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PurgeResult {
    pub outcome: PurgeOutcome,
    pub path: PathBuf,
    pub detail: String,
    pub replaced: Vec<PathBuf>,
    pub refusal: Option<PurgeRefusal>,
}

impl PurgeResult {
    fn new(outcome: PurgeOutcome, path: &Path) -> PurgeResult {
        PurgeResult {
            outcome,
            path: path.to_path_buf(),
            detail: String::new(),
            replaced: Vec::new(),
            refusal: None,
        }
    }

    fn because(outcome: PurgeOutcome, path: &Path, errno: i32) -> PurgeResult {
        PurgeResult {
            detail: strerror(errno),
            ..PurgeResult::new(outcome, path)
        }
    }
}

/// The strict real path of `path`, or of as much of it as exists.
///
/// A purge target that does not exist yet is legal, so a missing component falls back
/// to the walk that takes missing names as written. Its result is resolved strictly once
/// more: `<missing>/..` collapses in that walk, so what it returns can still end in the
/// loop the missing component hid. Only "still missing" is accepted there; when the
/// second look succeeds its result is the answer, since the fallback may have read a link
/// it could not look at as a plain component.
fn resolve(path: &Path) -> Result<PathBuf, OsFailure> {
    match realpath(path, true) {
        Err(failure) if failure.is_absent() => {}
        resolved => return resolved,
    }
    let resolved = realpath(path, false)?;
    match realpath(&resolved, true) {
        Err(failure) if failure.is_absent() => Ok(resolved),
        checked => checked,
    }
}

/// The refusal for a target too dangerous to delete, or None.
///
/// Both checks run against `canonical` -- the destination the deletion would actually
/// walk -- because a symlinked component anywhere above the leaf redirects the deletion
/// somewhere the given spelling never named, and a relative path or a `..` chain names it
/// only after resolution.
///
/// A leaf that is itself a symlink is still refused outright rather than followed: the
/// root names the store, and a link standing in for it is an arrangement memriver will
/// not delete through.
fn refuse_purge_target(
    given: &Path,
    canonical: &Path,
    home: &Path,
    cwd: &Path,
) -> Option<PurgeRefusal> {
    let refusal = |kind, path: &Path, detail: String| PurgeRefusal {
        kind,
        path: path.to_path_buf(),
        canonical: Some(canonical.to_path_buf()),
        detail,
    };
    match lstat(given) {
        Ok(found) if found.file_type().is_symlink() => {
            return Some(refusal(PurgeRefusalKind::Symlink, given, String::new()));
        }
        Ok(_) => {}
        Err(failure) if failure.is_absent() => {}
        // "could not look" is not "not a link"
        Err(failure) => {
            return Some(refusal(
                PurgeRefusalKind::Unresolvable,
                given,
                failure.to_string(),
            ));
        }
    }
    // `home`/`cwd` lying at or below the target covers the target *being* one of them
    // and the target being any ancestor of one -- the filesystem root included
    for base in [home, cwd] {
        match resolve(base) {
            Ok(resolved) if resolved.starts_with(canonical) => {
                return Some(refusal(
                    PurgeRefusalKind::TooBroad,
                    canonical,
                    String::new(),
                ));
            }
            Ok(_) => {}
            Err(failure) => {
                return Some(refusal(
                    PurgeRefusalKind::Unresolvable,
                    base,
                    failure.to_string(),
                ));
            }
        }
    }
    None
}

fn errno() -> i32 {
    io::Error::last_os_error()
        .raw_os_error()
        .unwrap_or(libc::EIO)
}

fn c_name(name: &OsStr) -> Result<CString, i32> {
    CString::new(name.as_bytes()).map_err(|_| libc::EINVAL)
}

const DIRECTORY: libc::c_int = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC;

/// `name` opened as a directory, below `parent` when one is given; a link is refused.
fn open_directory(parent: Option<&OwnedFd>, name: &OsStr) -> Result<OwnedFd, i32> {
    let name = c_name(name)?;
    let (at, flags) = match parent {
        Some(parent) => (parent.as_raw_fd(), DIRECTORY | libc::O_NOFOLLOW),
        None => (libc::AT_FDCWD, DIRECTORY),
    };
    // SAFETY: `name` is a C string that outlives the call; a result that is not negative
    // is a new descriptor nobody else owns
    unsafe {
        let fd = libc::openat(at, name.as_ptr(), flags);
        if fd < 0 {
            Err(errno())
        } else {
            Ok(OwnedFd::from_raw_fd(fd))
        }
    }
}

/// `path` opened as a directory, refusing a symlink at *any* level.
///
/// `path` must be absolute and already resolved. Refusing a link at the leaf alone would
/// still follow an ancestor swapped for a symlink between a guard and the open, onto a
/// different real directory. So the path is walked component by component from the
/// filesystem root, every component opened without following: a symlink anywhere along
/// the way fails closed rather than redirecting.
fn open_without_following(path: &Path) -> Result<OwnedFd, i32> {
    let mut fd = open_directory(None, OsStr::new("/"))?;
    for component in path.iter().skip(1) {
        fd = open_directory(Some(&fd), component)?;
    }
    Ok(fd)
}

#[allow(clippy::unnecessary_cast)] // the fields are narrower on some systems
fn identity(fd: &OwnedFd) -> Result<Identity, i32> {
    // SAFETY: `found` is written by a successful call before it is read
    unsafe {
        let mut found: libc::stat = std::mem::zeroed();
        if libc::fstat(fd.as_raw_fd(), &mut found) != 0 {
            return Err(errno());
        }
        Ok((found.st_dev as u64, found.st_ino as u64))
    }
}

/// What stands at `name` below `parent`, a link not followed: its identity and whether it
/// is a directory.
#[allow(clippy::unnecessary_cast)] // the fields are narrower on some systems
fn identity_at(parent: &OwnedFd, name: &OsStr) -> Result<(Identity, bool), i32> {
    let name = c_name(name)?;
    // SAFETY: as in `identity`; `name` outlives the call
    unsafe {
        let mut found: libc::stat = std::mem::zeroed();
        let flags = libc::AT_SYMLINK_NOFOLLOW;
        if libc::fstatat(parent.as_raw_fd(), name.as_ptr(), &mut found, flags) != 0 {
            return Err(errno());
        }
        let is_directory = found.st_mode & libc::S_IFMT == libc::S_IFDIR;
        Ok(((found.st_dev as u64, found.st_ino as u64), is_directory))
    }
}

fn remove_at(parent: &OwnedFd, name: &OsStr, directory: bool) -> Result<(), i32> {
    let name = c_name(name)?;
    let flags = if directory { libc::AT_REMOVEDIR } else { 0 };
    // SAFETY: `name` outlives the call
    match unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), flags) } {
        0 => Ok(()),
        _ => Err(errno()),
    }
}

/// The names in the open directory `fd`.
fn names_in(fd: &OwnedFd) -> Result<Vec<OsString>, i32> {
    let copy = fd
        .try_clone()
        .map_err(|err| err.raw_os_error().unwrap_or(libc::EIO))?;
    // SAFETY: the stream takes over `copy`'s descriptor and closes it; every entry is
    // copied out before the next read
    unsafe {
        let raw = copy.as_raw_fd();
        let stream = libc::fdopendir(raw);
        if stream.is_null() {
            return Err(errno());
        }
        std::mem::forget(copy);
        libc::rewinddir(stream);
        let mut names = Vec::new();
        loop {
            *errno_location() = 0;
            let entry = libc::readdir(stream);
            if entry.is_null() {
                let failed = *errno_location();
                libc::closedir(stream);
                return if failed == 0 { Ok(names) } else { Err(failed) };
            }
            let name = CStr::from_ptr((*entry).d_name.as_ptr()).to_bytes();
            if name != b"." && name != b".." {
                names.push(OsString::from_vec(name.to_vec()));
            }
        }
    }
}

#[cfg(any(target_os = "macos", target_os = "ios", target_os = "freebsd"))]
unsafe fn errno_location() -> *mut libc::c_int {
    unsafe { libc::__error() }
}

#[cfg(not(any(target_os = "macos", target_os = "ios", target_os = "freebsd")))]
unsafe fn errno_location() -> *mut libc::c_int {
    unsafe { libc::__errno_location() }
}

/// Refuse unless the directory with these names is empty or holds the database directly.
fn store_refusal(names: Result<Vec<OsString>, i32>, canonical: &Path) -> Option<PurgeRefusal> {
    match names {
        // fails closed: a directory that cannot even be listed is not safe to delete
        Err(errno) => Some(PurgeRefusal::at(
            PurgeRefusalKind::Unopenable,
            canonical,
            strerror(errno),
        )),
        Ok(names) if !names.is_empty() && !names.iter().any(|name| name == DATABASE_FILENAME) => {
            Some(PurgeRefusal::at(
                PurgeRefusalKind::NotAStore,
                canonical,
                String::new(),
            ))
        }
        Ok(_) => None,
    }
}

/// Canonicalize and check a purge target; open it unless this is a dry run.
///
/// The canonical path -- not the spelling that produced it -- is what every guard runs
/// against and what the caller shows.
pub fn plan_purge(
    given: &Path,
    home: &Path,
    cwd: &Path,
    dry_run: bool,
) -> Result<PurgePlan, PurgeRefusal> {
    plan_purge_with(given, home, cwd, dry_run, &mut || {})
}

/// `plan_purge`, telling `before_open` when the names are checked and the target is
/// about to be opened.
fn plan_purge_with(
    given: &Path,
    home: &Path,
    cwd: &Path,
    dry_run: bool,
    before_open: &mut dyn FnMut(),
) -> Result<PurgePlan, PurgeRefusal> {
    let given = cwd.join(given);
    let canonical = resolve(&given).map_err(|failure| PurgeRefusal {
        kind: PurgeRefusalKind::Unresolvable,
        path: given.clone(),
        canonical: None,
        detail: failure.to_string(),
    })?;
    if let Some(refusal) = refuse_purge_target(&given, &canonical, home, cwd) {
        return Err(refusal);
    }
    let plan = |exists, opened| PurgePlan {
        given: given.clone(),
        canonical: canonical.clone(),
        home: home.to_path_buf(),
        cwd: cwd.to_path_buf(),
        exists,
        opened,
    };
    match stat(&canonical) {
        Ok(found) if found.is_dir() => {}
        Ok(_) => {
            return Err(PurgeRefusal::at(
                PurgeRefusalKind::NotDirectory,
                &canonical,
                String::new(),
            ));
        }
        Err(failure) if failure.is_absent() => return Ok(plan(false, None)),
        // a store that cannot be checked is not "no data to remove"
        Err(failure) => {
            return Err(PurgeRefusal::at(
                PurgeRefusalKind::Unresolvable,
                &canonical,
                failure.to_string(),
            ));
        }
    }
    let unopenable =
        |errno| PurgeRefusal::at(PurgeRefusalKind::Unopenable, &canonical, strerror(errno));
    if dry_run {
        // nothing is opened on a dry run, so this is the only check the path itself gets
        let names = fs::read_dir(&canonical)
            .and_then(|entries| {
                entries
                    .map(|entry| entry.map(|entry| entry.file_name()))
                    .collect()
            })
            .map_err(|err: io::Error| err.raw_os_error().unwrap_or(libc::EIO));
        return match store_refusal(names, &canonical) {
            Some(refusal) => Err(refusal),
            None => Ok(plan(true, None)),
        };
    }
    before_open();
    // the walk that follows no link is what makes any component -- leaf or ancestor --
    // turned into a symlink between the guards above and here fail closed
    let fd = open_without_following(&canonical).map_err(unopenable)?;
    // checked on the open directory, not on the name: a target swapped in between the
    // guards and the open would otherwise be purged unchecked
    if let Some(refusal) = store_refusal(names_in(&fd), &canonical) {
        return Err(refusal);
    }
    let confirmed = identity(&fd).map_err(unopenable)?;
    Ok(plan(true, Some((fd, confirmed))))
}

/// Delete the one directory the plan holds open, or nothing at all.
///
/// Comparing path strings a second time only proves that two moments resolved to the
/// same spelling; it does not prove the directory standing there is still the object the
/// user was shown. Neither does checking the object and then handing the *name* to a
/// remover: whatever re-opens that name can be handed a different directory than the one
/// that was checked.
///
/// So the deletion is anchored to descriptors instead. The canonical parent is opened
/// once; the leaf is opened through it and its identity compared against the confirmed
/// directory's; and the walk that empties it runs on *that* descriptor, never on a name.
/// A leaf renamed away mid-walk takes its descriptor with it -- deleting its contents is
/// still the action the user confirmed -- and a replacement standing at the old name is
/// neither entered nor removed.
///
/// Only the emptied root itself has to be detached by name, because a directory cannot
/// be unlinked by descriptor; that one step re-checks the identity immediately beforehand
/// and gives up if the name has changed hands.
///
/// Panics for a plan that holds nothing open (a dry run, a target that does not exist):
/// calling it then is a mistake in the caller.
pub fn purge(plan: PurgePlan) -> PurgeResult {
    purge_with(plan, &mut |_| {})
}

/// `purge`, telling `emptied` about every directory once it is empty and before its name
/// is checked again and removed.
fn purge_with(plan: PurgePlan, emptied: &mut dyn FnMut(&Path)) -> PurgeResult {
    let Some((_held, confirmed)) = &plan.opened else {
        panic!("purge needs an open plan made for deletion (not a dry run, not closed)");
    };
    let canonical = plan.canonical.as_path();
    let (Some(parent), Some(name)) = (canonical.parent(), canonical.file_name()) else {
        // the filesystem root: no plan is made for it
        return PurgeResult::new(PurgeOutcome::NotConfirmed, canonical);
    };
    let parent_fd = match open_without_following(parent) {
        Ok(fd) => fd,
        Err(errno) => return PurgeResult::because(PurgeOutcome::UnopenableParent, parent, errno),
    };
    let leaf_fd = match open_directory(Some(&parent_fd), name) {
        Ok(fd) => fd,
        Err(errno) => return PurgeResult::because(PurgeOutcome::Uncheckable, canonical, errno),
    };
    if identity(&leaf_fd) != Ok(*confirmed) {
        return PurgeResult::new(PurgeOutcome::NotConfirmed, canonical);
    }
    // the object is the confirmed one; the last question is whether the protected bases
    // moved into it while the prompt was open
    if let Some(refusal) = refuse_purge_target(&plan.given, canonical, &plan.home, &plan.cwd) {
        return PurgeResult {
            refusal: Some(refusal),
            ..PurgeResult::new(PurgeOutcome::Refused, canonical)
        };
    }
    let mut replaced = Vec::new();
    let partly = |errno, replaced| PurgeResult {
        replaced,
        ..PurgeResult::because(PurgeOutcome::PartlyRemoved, canonical, errno)
    };
    if let Err(errno) = empty_directory(&leaf_fd, canonical, &mut replaced, emptied) {
        return partly(errno, replaced);
    }
    emptied(canonical);
    match identity_at(&parent_fd, name) {
        Ok((still_there, _)) if still_there != *confirmed => PurgeResult {
            replaced,
            ..PurgeResult::new(PurgeOutcome::ReplacedRoot, canonical)
        },
        Ok(_) => match remove_at(&parent_fd, name, true) {
            Ok(()) => PurgeResult::new(PurgeOutcome::Removed, canonical),
            Err(errno) => partly(errno, replaced),
        },
        Err(errno) => partly(errno, replaced),
    }
}

/// Delete everything inside the open directory `fd`, not the directory itself.
///
/// Every step is relative to a descriptor this walk opened itself, so nothing a
/// concurrent rename does to the path above can redirect a single unlink. Child
/// directories are opened without following, which keeps a symlink a symlink: it is
/// unlinked where it stands, never followed into.
///
/// Detaching an emptied child is the one step no descriptor can carry out, so it names
/// the child, and a name can change hands while the walk beneath it runs. The child's
/// identity is captured when it is opened and re-checked immediately before it is
/// removed: a stranger standing at that name is appended to `replaced`, which the report
/// of a partial removal names. `directory` is carried only to spell those paths out.
///
/// The descriptor is held open across that comparison and across the removal itself. An
/// open descriptor keeps the original inode allocated, so a replacement created after the
/// original is renamed away *and* unlinked cannot be handed the same inode and pass the
/// comparison by wearing the confirmed identity.
///
/// What remains is best-effort, not a guarantee: there is no atomic "look and remove", so
/// a stranger that takes the name in the instant between the two is still removed. The
/// window needs exact, very short concurrent timing, and removing a directory never
/// touches one that is not empty, but it is not closed.
fn empty_directory(
    fd: &OwnedFd,
    directory: &Path,
    replaced: &mut Vec<PathBuf>,
    emptied: &mut dyn FnMut(&Path),
) -> Result<(), i32> {
    for name in names_in(fd)? {
        if !identity_at(fd, &name).is_ok_and(|(_, is_directory)| is_directory) {
            remove_at(fd, &name, false)?;
            continue;
        }
        let child_fd = open_directory(Some(fd), &name)?;
        let opened = identity(&child_fd)?;
        let child = directory.join(&name);
        empty_directory(&child_fd, &child, replaced, emptied)?;
        emptied(&child);
        if identity_at(fd, &name)?.0 != opened {
            replaced.push(child);
            continue;
        }
        remove_at(fd, &name, true)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
