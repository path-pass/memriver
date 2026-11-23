//! The read-only views through the command line: `list`, `show`, `search`, `sessions`,
//! `export`. Every expected text is what the Python implementation prints.

mod common;

use std::fs;
use std::ops::Deref;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::sync::{PoisonError, RwLock, RwLockReadGuard};

use common::{Done, World};
use memriver::cli::views::session_item;
use memriver_core::models::helpers::{cut, format_micros, now, parse_timestamp};
use memriver_core::models::memory::Memory;
use memriver_core::models::project::Project;
use memriver_core::models::session::{Harness, Session, SessionKey, SessionMove};
use memriver_core::repository::directories::DATABASE_FILENAME;
use serde_json::{Value, json};

const STORE_UNREADABLE: &str = "refused: the memory store could not be read\n";

// One test changes the mask new files are created with, which belongs to the whole
// process: it holds this alone, every other test shares it.
static CREATION_MASK: RwLock<()> = RwLock::new(());

/// A world, and the promise that the creation mask is the usual one while it lives.
struct Shared {
    world: World,
    _mask: RwLockReadGuard<'static, ()>,
}

impl Deref for Shared {
    type Target = World;

    fn deref(&self) -> &World {
        &self.world
    }
}

fn world() -> Shared {
    let mask = CREATION_MASK.read().unwrap_or_else(PoisonError::into_inner);
    Shared {
        world: common::world(),
        _mask: mask,
    }
}

fn text(path: &Path) -> &str {
    path.to_str().unwrap()
}

fn key(harness: Harness, session_id: &str) -> SessionKey {
    SessionKey::new(harness, session_id).unwrap()
}

fn codex(session_id: &str) -> SessionKey {
    key(Harness::Codex, session_id)
}

/// Starts a session the way the SessionStart hook does, through the service.
fn start(world: &World, key: &SessionKey, directory: &Path, source: &str) {
    world
        .services
        .session
        .start_session(key, source, text(directory), None, None)
        .unwrap();
}

fn observe(world: &World, key: &SessionKey, directory: &Path, prompt: &str) {
    world
        .services
        .session
        .observe_prompt(key, Some(&json!(prompt)), text(directory), None, None)
        .unwrap();
}

fn stored(world: &World, key: &SessionKey) -> Session {
    let sessions = world
        .services
        .session
        .list_sessions(None, "", None)
        .unwrap();
    sessions
        .into_iter()
        .find(|session| &session.key == key)
        .unwrap()
}

/// A second project `other`, bound to a directory of its own.
fn other_project(world: &World) -> (Project, PathBuf) {
    let directory = world.tmp.join("other");
    fs::create_dir(&directory).unwrap();
    let plan = world.services.project.plan_root(&directory, None).unwrap();
    let project = world.services.project.init_project("other", &plan).unwrap();
    (project, directory)
}

fn record(world: &World, directory: &Path, content: &str) -> Memory {
    let context = world
        .services
        .project
        .open_project_context(directory)
        .unwrap();
    world
        .services
        .memory
        .record(content, "project", true, "t", "", &context)
        .unwrap()
}

fn delete_the_memory(world: &World) {
    world
        .services
        .memory
        .delete(&world.memory.id, 1, &world.context, "mcp", None)
        .unwrap();
}

fn read_the_memory(world: &World) {
    world
        .services
        .memory
        .read(&world.memory.id, &world.context, "cli")
        .unwrap();
}

fn out(done: &Done) -> (i32, &str, &str) {
    (done.code, done.stdout.as_str(), done.stderr.as_str())
}

fn export(world: &World, target: &Path) -> Done {
    world.run_from(&world.tmp, &["export", text(target)])
}

fn mode(path: &Path) -> u32 {
    fs::symlink_metadata(path).unwrap().permissions().mode() & 0o7777
}

fn garbage_database(world: &World) {
    fs::write(
        world.store.join(DATABASE_FILENAME),
        "not a database, not even close",
    )
    .unwrap();
}

/// The header of an exported memory as its fields, and the body's bytes.
fn exported(path: &Path) -> (Vec<(String, Value)>, Vec<u8>) {
    let raw = fs::read_to_string(path).unwrap();
    let parts: Vec<&str> = raw.split("---\n").collect();
    let fields = parts[1]
        .lines()
        .map(|line| line.split_once(": ").unwrap())
        .map(|(field, value)| (field.to_string(), serde_json::from_str(value).unwrap()))
        .collect();
    (fields, parts[2].as_bytes().to_vec())
}

fn field<'a>(fields: &'a [(String, Value)], name: &str) -> Option<&'a Value> {
    fields
        .iter()
        .find(|(field, _)| field == name)
        .map(|(_, value)| value)
}

fn sessions_json(world: &World, args: &[&str]) -> Vec<Value> {
    let done = world.run(args);
    assert_eq!((done.code, done.stderr.as_str()), (0, ""));
    serde_json::from_str(&done.stdout).unwrap()
}

/// A claude-code session started in the demo project, then moved to a directory no
/// project covers (the way the UserPromptSubmit hook reports it).
fn moved_session(world: &World) -> (SessionKey, SessionMove) {
    let other = world.tmp.join("elsewhere");
    fs::create_dir(&other).unwrap();
    let key = key(Harness::ClaudeCode, "moved-1");
    let (session, work) = (&world.services.session, text(&world.work));
    session
        .start_session(&key, "startup", work, Some("/t/a/moved-1.jsonl"), None)
        .unwrap();
    session
        .observe_prompt(
            &key,
            Some(&json!("go")),
            work,
            Some("/t/b/moved-1.jsonl"),
            Some(text(&other)),
        )
        .unwrap();
    let moved = session.moves(&key).unwrap().remove(0);
    (key, moved)
}

// --- list, show, search ---

#[test]
fn list_shows_every_project_and_its_memories() {
    let world = world();
    let done = world.run(&["list"]);
    let (project, memory, work) = (&world.project.id, &world.memory, text(&world.work));
    let day = cut(&memory.updated, 10);
    assert_eq!(done.code, 0);
    assert!(done.stdout.contains(&format!("{project}  demo  ({work})")));
    assert!(
        done.stdout
            .contains(&format!("  {}  [project]  {day}  the cue", memory.id))
    );
    assert!(done.stdout.contains("global") && done.stdout.contains("(no memories)"));
    let all = format!(
        "{project}  demo  ({work})\n  {}  [project]  {day}  the cue\n{}  global  (global)\n  \
         (no memories)\n",
        memory.id, world.global_id
    );
    assert_eq!(out(&done), (0, all.as_str(), ""));
    // one project: the same lines of that project alone
    let done = world.run(&["list", "--project", &world.global_id]);
    let one = format!("{}  global  (global)\n  (no memories)\n", world.global_id);
    assert_eq!(out(&done), (0, one.as_str(), ""));
}

#[test]
fn list_names_a_project_without_a_directory() {
    let world = world();
    let conn = world.sql();
    conn.execute(
        "UPDATE projects SET root = NULL WHERE id = ?1",
        [&world.project.id],
    )
    .unwrap();
    let done = world.run(&["list", "--project", &world.project.id]);
    let first = format!("{}  demo  (no directory)\n", world.project.id);
    assert_eq!(done.code, 0);
    assert!(done.stdout.starts_with(&first), "{}", done.stdout);
}

#[test]
fn show_prints_fields_then_the_body_with_its_newlines() {
    let world = world();
    let done = world.run(&["show", &world.memory.id]);
    assert_eq!(done.code, 0);
    assert!(done.stdout.contains("version: 1") && !done.stdout.contains("deleted:"));
    assert!(done.stdout.ends_with("---\nline one\nline two\n"));
    let memory = &world.memory;
    let all = format!(
        "id: {}\nproject: {}\ntype: project\nsource: t/mcp\ntrust: agent\nsync: true\ncreated: \
         {}\nupdated: {}\nversion: 1\nlast_read_at: never\ndescription: the cue\n---\nline \
         one\nline two\n",
        memory.id, world.project.id, memory.created, memory.updated
    );
    assert_eq!(out(&done), (0, all.as_str(), ""));
}

#[test]
fn show_neutralises_terminal_escapes_in_the_body_but_keeps_newlines() {
    let world = world();
    let memory = record(&world, &world.work, "a\u{1b}[2Jb\nc\td\u{200b}e");
    let done = world.run(&["show", &memory.id]);
    assert!(!done.stdout.contains('\u{1b}'));
    assert!(done.stdout.contains("a [2Jb\nc"));
    assert!(
        done.stdout
            .ends_with("description: \n---\na [2Jb\nc\td e\n"),
        "{}",
        done.stdout
    );
}

#[test]
fn search_takes_keywords_and_ranks_across_projects() {
    let world = world();
    let (_, other_work) = other_project(&world);
    let newer = record(&world, &other_work, "two only");
    let done = world.run(&["search", "one two"]);
    assert_eq!(done.code, 0);
    let ids: Vec<&str> = done
        .stdout
        .lines()
        .map(|line| line.split_whitespace().next().unwrap())
        .collect();
    assert_eq!(ids, [world.memory.id.as_str(), newer.id.as_str()]);
    let lines = format!(
        "  {}  [project]  {}  the cue\n  {}  [project]  {}  two only\n",
        world.memory.id,
        cut(&world.memory.updated, 10),
        newer.id,
        cut(&newer.updated, 10)
    );
    assert_eq!(out(&done), (0, lines.as_str(), ""));
    let done = world.run(&["search", "zebra yak"]);
    assert_eq!(out(&done), (0, "(no matches)\n", ""));
}

#[test]
fn show_of_a_soft_deleted_memory_needs_the_flag() {
    let world = world();
    delete_the_memory(&world);
    let done = world.run(&["show", &world.memory.id]);
    assert!(done.code == 2 && done.stdout.contains("no such memory"));
    let refusal = format!("no such memory: {}\n", world.memory.id);
    assert_eq!(out(&done), (2, refusal.as_str(), ""));
    let done = world.run(&["show", &world.memory.id, "--deleted"]);
    assert!(done.code == 0 && done.stdout.contains("deleted: "));
    let deleted = world.services.memory.show(&world.memory.id, true).unwrap();
    let tail = format!(
        "description: the cue\ndeleted: {}\n---\nline one\nline two\n",
        deleted.deleted_at.unwrap()
    );
    assert!(done.stdout.ends_with(&tail), "{}", done.stdout);
}

#[test]
fn search_finds_across_projects_and_by_project() {
    let world = world();
    let (other, other_work) = other_project(&world);
    let other_memory = record(&world, &other_work, "another line entirely");
    let (mine, theirs) = (world.memory.id.as_str(), other_memory.id.as_str());

    let done = world.run(&["search", "LINE"]);
    assert_eq!(done.code, 0);
    assert!(done.stdout.contains(mine) && done.stdout.contains(theirs));

    let done = world.run(&["search", "LINE", "--project", &world.project.id]);
    assert!(done.stdout.contains(mine) && !done.stdout.contains(theirs));

    let done = world.run(&["search", "LINE", "--project", &other.id]);
    assert!(done.stdout.contains(theirs) && !done.stdout.contains(mine));

    let done = world.run(&["search", "LINE", "--limit", "1"]);
    assert_eq!(done.stdout.lines().count(), 1);

    let done = world.run(&["search", "nothing-like-this"]);
    assert_eq!(out(&done), (0, "(no matches)\n", ""));
}

#[test]
fn list_and_search_neutralise_control_characters_in_stored_fields() {
    let world = world();
    let conn = world.sql();
    conn.execute(
        "UPDATE memories SET updated = ?1, description = ?2 WHERE id = ?3",
        [
            "\u{1b}[2J2026-09-2",
            "cue\u{1b}[31m",
            world.memory.id.as_str(),
        ],
    )
    .unwrap();
    // the stored time is cut to ten characters after it was neutralised
    let line = format!("  {}  [project]   [2J2026-0  cue [31m\n", world.memory.id);
    let done = world.run(&["list", "--project", &world.project.id]);
    assert!(!done.stdout.contains('\u{1b}'));
    let listed = format!(
        "{}  demo  ({})\n{line}",
        world.project.id,
        text(&world.work)
    );
    assert_eq!(out(&done), (0, listed.as_str(), ""));
    let done = world.run(&["search", "line"]);
    assert!(!done.stdout.contains('\u{1b}'));
    assert_eq!(out(&done), (0, line.as_str(), ""));

    // a project's name and its directory are stored text too: a name may hold a
    // right-to-left override, a directory's name an escape
    let directory = world.tmp.join("es\u{1b}[31mc");
    fs::create_dir(&directory).unwrap();
    let plan = world.services.project.plan_root(&directory, None).unwrap();
    let named = world.services.project.init_project("na\u{202e}me", &plan);
    let hostile = named.unwrap();
    assert_eq!(
        (hostile.name.as_str(), hostile.root.as_deref()),
        ("na\u{202e}me", Some(text(&directory)))
    );
    let shown = format!("{}/es [31mc", text(&world.tmp));
    let done = world.run(&["list", "--project", &hostile.id]);
    let listed = format!("{}  na me  ({shown})\n  (no memories)\n", hostile.id);
    assert_eq!(out(&done), (0, listed.as_str(), ""));
    // the cell of a session that runs there
    start(&world, &codex("s-hostile"), &directory, "startup");
    let done = world.run(&["sessions", "--project", &hostile.id]);
    let cell = format!("\n  project: na me ({})\n", hostile.id);
    assert!(done.stdout.contains(&cell), "{}", done.stdout);
    assert!(
        !done.stdout.contains(['\u{1b}', '\u{202e}']),
        "{}",
        done.stdout
    );
    // the snapshot is a file, not a terminal: the name is kept on one line as it is
    // stored, the directory is shown as everywhere
    let target = world.tmp.join("snapshot");
    assert_eq!(export(&world, &target).code, 0);
    let projects = fs::read_to_string(target.join("projects.md")).unwrap();
    let entry = format!("- {} na\u{202e}me ({shown})\n", hostile.id);
    assert!(projects.contains(&entry), "{projects}");
    assert!(!projects.contains('\u{1b}'));
}

#[test]
fn list_and_search_show_the_first_line_of_a_memory_without_a_description() {
    let world = world();
    let long = record(&world, &world.work, &format!("{}\nsecond", "é".repeat(61)));
    let done = world.run(&["search", "second"]);
    let line = format!(
        "  {}  [project]  {}  {}\n",
        long.id,
        cut(&long.updated, 10),
        "é".repeat(60)
    );
    assert_eq!(out(&done), (0, line.as_str(), ""));
    assert!(world.run(&["list"]).stdout.contains(&line));
}

#[test]
fn list_reports_a_fixed_sentence_when_the_service_cannot_be_built() {
    let world = world();
    garbage_database(&world);
    assert_eq!(out(&world.run(&["list"])), (2, STORE_UNREADABLE, ""));
    assert_eq!(
        out(&world.run(&["search", "line"])),
        (2, STORE_UNREADABLE, "")
    );
}

#[test]
fn show_reports_a_fixed_sentence_when_the_service_cannot_be_built() {
    let world = world();
    garbage_database(&world);
    let done = world.run(&["show", &world.memory.id]);
    assert_eq!(out(&done), (2, STORE_UNREADABLE, ""));
}

#[test]
fn show_neutralises_an_escape_planted_in_the_description() {
    let world = world();
    let conn = world.sql();
    conn.execute(
        "UPDATE memories SET description = ?1 WHERE id = ?2",
        ["cue\u{1b}[31m  two\u{200b}x", world.memory.id.as_str()],
    )
    .unwrap();
    let done = world.run(&["show", &world.memory.id]);
    assert_eq!(done.code, 0);
    assert!(!done.stdout.contains('\u{1b}'));
    assert!(done.stdout.contains("description: cue [31m two x\n"));
}

#[test]
fn list_with_an_unknown_project_id_is_refused() {
    let world = world();
    let done = world.run(&["list", "--project", "zzzzzzzzzz"]);
    assert!(done.code == 2 && done.stdout.contains("no such project"));
    assert_eq!(out(&done), (2, "no such project: zzzzzzzzzz\n", ""));
    let done = world.run(&["search", "line", "--project", "zzzzzzzzzz"]);
    assert_eq!(out(&done), (2, "no such project: zzzzzzzzzz\n", ""));
}

#[test]
fn show_reports_never_read_then_the_timestamp_once_read() {
    let world = world();
    let done = world.run(&["show", &world.memory.id]);
    assert!(done.code == 0 && done.stdout.contains("last_read_at: never\n"));

    read_the_memory(&world);
    let reread = world.services.memory.show(&world.memory.id, false).unwrap();
    let done = world.run(&["show", &world.memory.id]);
    assert_eq!(done.code, 0);
    assert!(!done.stdout.contains("last_read_at: never"));
    let line = format!("\nlast_read_at: {}\n", reread.last_read_at.unwrap());
    assert!(done.stdout.contains(&line), "{}", done.stdout);
}

#[test]
fn caller_id_is_cut_to_255_scalars() {
    let world = world();
    // 300 characters of four bytes each, three of them invisible ones
    let face = "\u{1f600}";
    let id = format!("{}\u{1b}\n\u{202e}{}", face.repeat(100), face.repeat(197));
    assert_eq!(id.chars().count(), 300);
    let echoed = format!("{}   {}", face.repeat(100), face.repeat(152));
    assert_eq!(echoed.chars().count(), 255);

    let done = world.run(&["show", &id]);
    assert_eq!(
        out(&done),
        (2, format!("no such memory: {echoed}\n").as_str(), "")
    );
    let refusal = format!("no such project: {echoed}\n");
    for line in [
        vec!["list", "--project", &id],
        vec!["search", "line", "--project", &id],
        vec!["sessions", "--project", &id],
        vec!["sessions", "--json", "--project", &id],
    ] {
        assert_eq!(
            out(&world.run(&line)),
            (2, refusal.as_str(), ""),
            "{:?}",
            line[0]
        );
    }
}

// --- export ---

#[test]
fn export_writes_a_private_snapshot_and_never_reads_it_back() {
    let world = world();
    let target = world.tmp.join("snap");
    let done = export(&world, &target);
    let said = format!("exported 1 memories to {}\n", text(&target));
    assert_eq!(out(&done), (0, said.as_str(), ""));
    assert_eq!(mode(&target), 0o700);
    let path = target
        .join(&world.project.id)
        .join(format!("{}.md", world.memory.id));
    assert_eq!(mode(&path), 0o600);
    let (fields, body) = exported(&path);
    assert_eq!(field(&fields, "version"), Some(&json!(1)));
    assert_eq!(field(&fields, "deleted_at"), None);
    assert_eq!(field(&fields, "source_harness"), Some(&json!("t")));
    assert_eq!(field(&fields, "source_method"), Some(&json!("mcp")));
    assert_eq!(field(&fields, "source"), None);
    // byte for byte: no line break is added after the body
    assert_eq!(body, world.memory.body.as_bytes());
    let projects = fs::read_to_string(target.join("projects.md")).unwrap();
    assert!(projects.contains(&world.project.id));
}

#[test]
fn export_takes_a_relative_directory_from_the_working_directory() {
    let world = world();
    let done = world.run_from(&world.tmp, &["export", "a//./snap/"]);
    let said = format!(
        "refused: {}/a/snap could not be created (No such file or directory)\n",
        text(&world.tmp)
    );
    assert_eq!(out(&done), (2, said.as_str(), ""));
    // `..` is kept as it was written
    let done = world.run_from(&world.work, &["export", "../snap"]);
    let said = format!("exported 1 memories to {}/../snap\n", text(&world.work));
    assert_eq!(out(&done), (0, said.as_str(), ""));
    assert!(world.tmp.join("snap/projects.md").is_file());
}

#[test]
fn export_shows_a_directory_name_with_its_invisible_characters_replaced() {
    let world = world();
    let target = world.tmp.join("we\u{1b}ird\u{202e}");
    let done = export(&world, &target);
    let said = format!("exported 1 memories to {}/we ird \n", text(&world.tmp));
    assert_eq!(out(&done), (0, said.as_str(), ""));
    assert!(target.join("projects.md").is_file());
    let done = export(&world, &target);
    let said = format!("refused: {}/we ird  already exists\n", text(&world.tmp));
    assert_eq!(out(&done), (2, said.as_str(), ""));
}

#[test]
fn export_into_a_missing_parent_is_a_fixed_refusal() {
    let world = world();
    let target = world.tmp.join("missing").join("snap");
    let done = export(&world, &target);
    assert!(done.code == 2 && done.stdout.contains("could not be created"));
    let said = format!(
        "refused: {} could not be created (No such file or directory)\n",
        text(&target)
    );
    assert_eq!(out(&done), (2, said.as_str(), ""));
    assert!(!world.tmp.join("missing").exists());
}

#[test]
fn a_failure_mid_export_says_the_snapshot_is_partial() {
    // SAFETY: reading the effective user id has no preconditions
    if unsafe { libc::geteuid() } == 0 {
        return; // nothing is refused to the superuser
    }
    let _alone = CREATION_MASK
        .write()
        .unwrap_or_else(PoisonError::into_inner);
    let world = common::world();
    let target = world.tmp.join("snap");
    // the directory the export makes for itself comes out read-only, so its first write
    // fails
    // SAFETY: the mask is a plain number of the process; the lock above keeps every
    // other test of this file from creating a file meanwhile
    let before = unsafe { libc::umask(0o300) };
    let done = export(&world, &target);
    // SAFETY: as above
    unsafe { libc::umask(before) };
    let said = format!(
        "refused: the export stopped after 0 memories (Permission denied); {} holds a partial \
         snapshot -- delete it and run memriver export again\n",
        text(&target)
    );
    assert_eq!(out(&done), (2, said.as_str(), ""));
    assert!(done.stdout.contains("partial snapshot") && !done.stdout.contains("exported"));
    assert_eq!(fs::read_dir(&target).unwrap().count(), 0);
}

#[test]
fn export_refuses_an_existing_directory() {
    let world = world();
    let target = world.tmp.join("snap");
    fs::create_dir(&target).unwrap();
    let done = export(&world, &target);
    assert!(done.code == 2 && done.stdout.contains("already exists"));
    assert_eq!(fs::read_dir(&target).unwrap().count(), 0);
}

#[test]
fn export_refuses_an_existing_path_and_creates_0700_0600() {
    let world = world();
    let (directory, file, dangling) = (
        world.tmp.join("directory"),
        world.tmp.join("file"),
        world.tmp.join("dangling"),
    );
    fs::create_dir(&directory).unwrap();
    fs::write(&file, "kept").unwrap();
    symlink(world.tmp.join("nowhere"), &dangling).unwrap();
    for taken in [&directory, &file, &dangling] {
        let done = export(&world, taken);
        let said = format!("refused: {} already exists\n", text(taken));
        assert_eq!(out(&done), (2, said.as_str(), ""));
    }
    assert_eq!(fs::read_dir(&directory).unwrap().count(), 0);
    assert_eq!(fs::read(&file).unwrap(), b"kept");
    assert!(!world.tmp.join("nowhere").exists());
    // a store that cannot be read is not looked at before the target is
    garbage_database(&world);
    let done = export(&world, &directory);
    let said = format!("refused: {} already exists\n", text(&directory));
    assert_eq!(out(&done), (2, said.as_str(), ""));
    drop(world);

    let world = self::world();
    let (_, other_work) = other_project(&world);
    let second = record(&world, &other_work, "caf\u{e9} \u{1f600}\n\nlast line\n");
    let other_id = second.project_id.clone();
    let target = world.tmp.join("fresh");
    let done = export(&world, &target);
    let said = format!("exported 2 memories to {}\n", text(&target));
    assert_eq!(out(&done), (0, said.as_str(), ""));

    let mut names: Vec<String> = fs::read_dir(&target)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    let mut expected = vec![
        world.project.id.clone(),
        other_id.clone(),
        "projects.md".to_string(),
    ];
    expected.sort();
    assert_eq!(names, expected, "global has no memory and no directory");
    for directory in [
        target.clone(),
        target.join(&world.project.id),
        target.join(&other_id),
    ] {
        assert_eq!(mode(&directory), 0o700, "{directory:?}");
    }
    let first_file = target
        .join(&world.project.id)
        .join(format!("{}.md", world.memory.id));
    let second_file = target.join(&other_id).join(format!("{}.md", second.id));
    for file in [&target.join("projects.md"), &first_file, &second_file] {
        assert_eq!(mode(file), 0o600, "{file:?}");
    }
    for folder in [&world.project.id, &other_id] {
        assert_eq!(fs::read_dir(target.join(folder)).unwrap().count(), 1);
    }

    let projects = fs::read_to_string(target.join("projects.md")).unwrap();
    let mut lines: Vec<String> = projects.lines().map(str::to_string).collect();
    lines.sort();
    let mut expected = vec![
        format!("- {} demo ({})", world.project.id, text(&world.work)),
        format!("- {} global (global)", world.global_id),
        format!("- {other_id} other ({})", text(&other_work)),
    ];
    expected.sort();
    assert_eq!(lines, expected);
    assert!(projects.ends_with(")\n") && !projects.contains("\n\n"));
    // the order is the order `list` has
    let listed: Vec<String> = world
        .run(&["list"])
        .stdout
        .lines()
        .filter(|line| !line.starts_with(' '))
        .map(|line| cut(line, 10).to_string())
        .collect();
    let written: Vec<String> = projects
        .lines()
        .map(|line| line[2..12].to_string())
        .collect();
    assert_eq!(written, listed);

    let memory = &world.memory;
    let first = format!(
        "---\nid: \"{}\"\nproject_id: \"{}\"\ntype: \"project\"\nsource_harness: \
         \"t\"\nsource_method: \"mcp\"\ntrust: \"agent\"\nsync: true\ncreated: \"{}\"\nupdated: \
         \"{}\"\nversion: 1\nlast_read_at: null\ndescription: \"the cue\"\n---\nline one\nline two",
        memory.id, world.project.id, memory.created, memory.updated
    );
    assert_eq!(fs::read(&first_file).unwrap(), first.as_bytes());
    let written = fs::read_to_string(&second_file).unwrap();
    assert!(
        written.ends_with("description: \"\"\n---\ncaf\u{e9} \u{1f600}\n\nlast line"),
        "{written}"
    );
}

#[test]
fn export_header_is_ascii_and_one_line_a_field() {
    let world = world();
    let conn = world.sql();
    conn.execute(
        "UPDATE memories SET description = ?1 WHERE id = ?2",
        [
            "caf\u{e9} \"q\" \\ \u{1f600}\ttab",
            world.memory.id.as_str(),
        ],
    )
    .unwrap();
    let target = world.tmp.join("snap");
    assert_eq!(export(&world, &target).code, 0);
    let path = target
        .join(&world.project.id)
        .join(format!("{}.md", world.memory.id));
    let written = fs::read_to_string(path).unwrap();
    let line = "\ndescription: \"caf\\u00e9 \\\"q\\\" \\\\ \\ud83d\\ude00\\ttab\"\n---\n";
    assert!(written.contains(line), "{written}");
}

#[test]
fn export_skips_a_soft_deleted_memory() {
    let world = world();
    delete_the_memory(&world);
    let target = world.tmp.join("snap");
    let done = export(&world, &target);
    let said = format!("exported 0 memories to {}\n", text(&target));
    assert_eq!(out(&done), (0, said.as_str(), ""));
    assert!(!target.join(&world.project.id).exists());
}

#[test]
fn export_header_includes_last_read_at() {
    let world = world();
    read_the_memory(&world);
    let target = world.tmp.join("snap");
    assert_eq!(export(&world, &target).code, 0);
    let path = target
        .join(&world.project.id)
        .join(format!("{}.md", world.memory.id));
    let (fields, _) = exported(&path);
    let reread = world.services.memory.show(&world.memory.id, false).unwrap();
    assert!(reread.last_read_at.is_some());
    assert_eq!(
        field(&fields, "last_read_at"),
        Some(&json!(reread.last_read_at))
    );
    let names: Vec<&str> = fields.iter().map(|(field, _)| field.as_str()).collect();
    assert_eq!(
        names,
        [
            "id",
            "project_id",
            "type",
            "source_harness",
            "source_method",
            "trust",
            "sync",
            "created",
            "updated",
            "version",
            "last_read_at",
            "description"
        ]
    );
}

#[test]
fn export_of_a_store_that_cannot_be_read_makes_no_directory() {
    let world = world();
    garbage_database(&world);
    let target = world.tmp.join("snap");
    assert_eq!(out(&export(&world, &target)), (2, STORE_UNREADABLE, ""));
    assert!(!target.exists());
}

// --- sessions ---

#[test]
fn sessions_lists_harness_project_branch_and_prompts() {
    let world = world();
    let key = codex("sess-1");
    start(&world, &key, &world.work, "startup");
    observe(&world, &key, &world.work, "first task");
    observe(&world, &key, &world.work, "second task");

    let done = world.run(&["sessions"]);
    assert_eq!(done.code, 0);
    for part in [
        "codex",
        "sess-1",
        "demo",
        world.project.id.as_str(),
        "first task",
        "second task",
        "codex resume sess-1",
    ] {
        assert!(done.stdout.contains(part), "{part}");
    }
    let session = stored(&world, &key);
    let block = format!(
        "codex sess-1\n  last active: {} (just now)\n  project: demo ({})\n  branch: -\n  first \
         recorded: {}\n  last end event: -\n  first prompt: first task\n  latest prompt: second \
         task\n  summary: -\n  resume: codex resume sess-1\n",
        session.last_active_at, world.project.id, session.started_at
    );
    assert_eq!(out(&done), (0, block.as_str(), ""));
}

#[test]
fn sessions_are_listed_one_block_under_the_other() {
    let world = world();
    let (first, second) = (codex("first"), key(Harness::ClaudeCode, "second"));
    start(&world, &first, &world.work, "startup");
    start(&world, &second, &world.tmp, "startup");
    world.services.session.end_session(&second).unwrap();
    let (first, second) = (stored(&world, &first), stored(&world, &second));
    let done = world.run(&["sessions"]);
    let blocks = format!(
        "claude-code second\n  last active: {} (just now)\n  project: (unbound)\n  branch: -\n  \
         first recorded: {}\n  last end event: {}\n  first prompt: -\n  latest prompt: -\n  \
         summary: -\n  resume: claude --resume second\ncodex first\n  last active: {} (just \
         now)\n  project: demo ({})\n  branch: -\n  first recorded: {}\n  last end event: -\n  \
         first prompt: -\n  latest prompt: -\n  summary: -\n  resume: codex resume first\n",
        second.last_active_at,
        second.started_at,
        second.ended_at.as_deref().unwrap(),
        first.last_active_at,
        world.project.id,
        first.started_at
    );
    assert_eq!(out(&done), (0, blocks.as_str(), ""));
}

#[test]
fn sessions_take_keywords_and_rank_by_how_many_match() {
    let world = world();
    let (older, newer) = (codex("older"), codex("newer"));
    start(&world, &older, &world.work, "startup");
    observe(&world, &older, &world.work, "fix the login form");
    start(&world, &newer, &world.work, "startup");
    observe(&world, &newer, &world.work, "login page");
    let items = sessions_json(&world, &["sessions", "login form", "--json"]);
    let ids: Vec<&Value> = items.iter().map(|item| &item["session_id"]).collect();
    assert_eq!(ids, [&json!("older"), &json!("newer")]);
}

#[test]
fn sessions_shows_the_relative_age_from_the_injected_now() {
    let world = world();
    let key = codex("sess-2");
    start(&world, &key, &world.work, "startup");
    // the command reads the clock itself: the session is set back instead
    let day = 86_400_000_000;
    let long_ago = format_micros(parse_timestamp(&now()).unwrap() - 42 * day - day / 2).unwrap();
    let conn = world.sql();
    conn.execute(
        "UPDATE sessions SET started_at = ?1, last_active_at = ?1",
        [&long_ago],
    )
    .unwrap();
    let done = world.run(&["sessions"]);
    assert_eq!(done.code, 0);
    assert!(done.stdout.contains("42 days ago"));
    let line = format!("\n  last active: {long_ago} (42 days ago)\n");
    assert!(done.stdout.contains(&line), "{}", done.stdout);
}

#[test]
fn sessions_lists_pending_sessions_with_their_status() {
    let world = world();
    let key = key(Harness::ClaudeCode, "resumed-1");
    start(&world, &key, &world.work, "resume");

    let done = world.run(&["sessions"]);
    assert_eq!(done.code, 0);
    assert!(done.stdout.starts_with("claude-code resumed-1\n"));
    let cell = format!("\n  project: pending -> demo ({})\n", world.project.id);
    assert!(done.stdout.contains(&cell), "{}", done.stdout);
    assert!(
        done.stdout
            .ends_with("\n  resume: claude --resume resumed-1\n")
    );
}

#[test]
fn sessions_with_an_unknown_project_id_is_refused() {
    let world = world();
    let done = world.run(&["sessions", "--project", "zzzzzzzzzz"]);
    assert_eq!(out(&done), (2, "no such project: zzzzzzzzzz\n", ""));
}

#[test]
fn sessions_json_matches_the_session_search_item_shape() {
    let world = world();
    let key = codex("sess-3");
    start(&world, &key, &world.work, "startup");
    observe(&world, &key, &world.work, "task one");

    let items = sessions_json(&world, &["sessions", "--json"]);
    assert_eq!(items.len(), 1);
    let item = items[0].as_object().unwrap();
    let keys: Vec<&str> = item.keys().map(String::as_str).collect();
    assert_eq!(
        keys,
        [
            "harness",
            "session_id",
            "project",
            "branch",
            "entry_cwd",
            "first_recorded",
            "last_active_at",
            "last_end_event_at",
            "first_prompt",
            "recent_prompts",
            "resume_command",
            "summary",
            "summary_at",
            "moves"
        ]
    );
    assert_eq!(item["moves"], json!([]));
    assert_eq!(
        (&item["summary"], &item["summary_at"]),
        (&Value::Null, &Value::Null)
    );
    assert_eq!(item["resume_command"], json!("codex resume sess-3"));
    assert_eq!(item["project"], json!(world.project.id));
    assert_eq!(item["first_prompt"]["text"], json!("task one"));
}

#[test]
fn sessions_json_is_ascii_escaped_indent_2() {
    let world = world();
    let key = codex("sess-1");
    start(&world, &key, &world.work, "startup");
    observe(&world, &key, &world.work, "修复 login 😀 \"q\" \\ é");
    let session = stored(&world, &key);
    let at = session.first_prompt.as_ref().unwrap().at();

    let done = world.run(&["sessions", "--json"]);
    let expected = [
        r#"["#.to_string(),
        r#"  {"#.to_string(),
        r#"    "harness": "codex","#.to_string(),
        r#"    "session_id": "sess-1","#.to_string(),
        format!(r#"    "project": "{}","#, world.project.id),
        r#"    "branch": null,"#.to_string(),
        format!(r#"    "entry_cwd": "{}","#, text(&world.work)),
        format!(r#"    "first_recorded": "{}","#, session.started_at),
        format!(r#"    "last_active_at": "{}","#, session.last_active_at),
        r#"    "last_end_event_at": null,"#.to_string(),
        r#"    "first_prompt": {"#.to_string(),
        format!(r#"      "at": "{at}","#),
        r#"      "text": "\u4fee\u590d login \ud83d\ude00 \"q\" \\ \u00e9""#.to_string(),
        r#"    },"#.to_string(),
        r#"    "recent_prompts": ["#.to_string(),
        r#"      {"#.to_string(),
        format!(r#"        "at": "{at}","#),
        r#"        "text": "\u4fee\u590d login \ud83d\ude00 \"q\" \\ \u00e9""#.to_string(),
        r#"      }"#.to_string(),
        r#"    ],"#.to_string(),
        r#"    "resume_command": "codex resume sess-1","#.to_string(),
        r#"    "summary": null,"#.to_string(),
        r#"    "summary_at": null,"#.to_string(),
        r#"    "moves": []"#.to_string(),
        r#"  }"#.to_string(),
        r#"]"#.to_string(),
        String::new(),
    ]
    .join("\n");
    assert_eq!(out(&done), (0, expected.as_str(), ""));
    assert!(done.stdout.is_ascii());
    drop(world);

    // no session: the empty list on one line
    let world = self::world();
    assert_eq!(out(&world.run(&["sessions", "--json"])), (0, "[]\n", ""));
}

#[test]
fn sessions_filters_by_project_and_query_and_limit() {
    let world = world();
    let (other, other_work) = other_project(&world);
    let (mine, theirs) = (codex("mine"), codex("theirs"));
    start(&world, &mine, &world.work, "startup");
    observe(&world, &mine, &world.work, "fix the login bug");
    start(&world, &theirs, &other_work, "startup");
    observe(&world, &theirs, &other_work, "unrelated task");

    let done = world.run(&["sessions", "--project", &world.project.id]);
    assert!(done.stdout.contains("mine") && !done.stdout.contains("theirs"));

    let done = world.run(&["sessions", "--project", &other.id]);
    assert!(done.stdout.contains("theirs") && !done.stdout.contains("mine"));

    let done = world.run(&["sessions", "login"]);
    assert!(done.stdout.contains("mine") && !done.stdout.contains("theirs"));

    let done = world.run(&["sessions", "--limit", "1"]);
    assert_eq!(done.stdout.matches("resume:").count(), 1);
}

#[test]
fn sessions_with_no_rows_says_so() {
    let world = world();
    assert_eq!(out(&world.run(&["sessions"])), (0, "(no sessions)\n", ""));
    assert_eq!(
        out(&world.run(&["sessions", "nothing"])),
        (0, "(no sessions)\n", "")
    );
}

#[test]
fn sessions_reports_a_fixed_sentence_when_the_service_cannot_be_built() {
    let world = world();
    start(&world, &codex("sess-1"), &world.work, "startup");
    garbage_database(&world);
    assert_eq!(out(&world.run(&["sessions"])), (2, STORE_UNREADABLE, ""));
    assert_eq!(
        out(&world.run(&["sessions", "--json"])),
        (2, STORE_UNREADABLE, "")
    );
}

#[test]
fn sessions_neutralises_an_invisible_character_in_a_prompt() {
    let world = world();
    let key = codex("hostile-1");
    start(&world, &key, &world.work, "startup");
    observe(&world, &key, &world.work, "task\u{200b}one");

    let done = world.run(&["sessions"]);
    assert_eq!(done.code, 0);
    assert!(!done.stdout.contains('\u{200b}'));
    assert!(
        done.stdout
            .contains("\n  first prompt: task one\n  latest prompt: task one\n")
    );
    // the script's form keeps the prompt as it was recorded
    let items = sessions_json(&world, &["sessions", "--json"]);
    assert_eq!(items[0]["first_prompt"]["text"], json!("task\u{200b}one"));
}

#[test]
fn sessions_show_a_published_summary_as_one_line() {
    let world = world();
    let key = codex("sess-summary");
    start(&world, &key, &world.work, "startup");
    let read = stored(&world, &key).last_active_at;
    world
        .services
        .session
        .publish_summary(&key, "Fixed the build.\nThen the docs.", &read)
        .unwrap();
    let done = world.run(&["sessions"]);
    assert_eq!(done.code, 0);
    assert!(
        done.stdout
            .contains("\n  summary: Fixed the build. Then the docs.\n")
    );
    let items = sessions_json(&world, &["sessions", "--json"]);
    assert_eq!(
        items[0]["summary"],
        json!("Fixed the build.\nThen the docs.")
    );
    assert_eq!(
        items[0]["summary_at"],
        json!(stored(&world, &key).summary_at)
    );
}

#[test]
fn sessions_text_shows_moves() {
    let world = world();
    let (_, moved) = moved_session(&world);
    let done = world.run(&["sessions"]);
    assert_eq!(done.code, 0);
    let line = format!(
        "  moved: {} from {} (demo ({})) to {} (-)\n",
        moved.moved_at, moved.from_entry_cwd, world.project.id, moved.to_entry_cwd
    );
    assert_eq!(moved.from_entry_cwd, text(&world.work));
    assert!(done.stdout.contains(&line), "{}", done.stdout);
    let at = |part: &str| done.stdout.find(part).unwrap();
    assert!(at("  branch:") < at("  moved:") && at("  moved:") < at("  first recorded:"));
}

#[test]
fn sessions_json_has_moves_list() {
    let world = world();
    let (_, moved) = moved_session(&world);
    start(&world, &codex("still"), &world.work, "startup");
    let items = sessions_json(&world, &["sessions", "--json"]);
    let of = |id: &str| {
        let found = items.iter().find(|item| item["session_id"] == json!(id));
        &found.unwrap()["moves"]
    };
    assert_eq!(of("still"), &json!([]));
    let expected = json!([{
        "moved_at": moved.moved_at,
        "from_entry_cwd": moved.from_entry_cwd,
        "from_project_id": world.project.id,
        "to_entry_cwd": moved.to_entry_cwd,
        "to_project_id": null,
    }]);
    assert_eq!(of("moved-1"), &expected);
    // the keys in the order they are written
    let keys: Vec<&str> = of("moved-1")[0]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        [
            "moved_at",
            "from_entry_cwd",
            "from_project_id",
            "to_entry_cwd",
            "to_project_id"
        ]
    );
}

#[test]
fn session_search_item_has_no_moves_key() {
    let world = world();
    let (key, _) = moved_session(&world);
    let item = session_item(&stored(&world, &key));
    assert!(!item.contains_key("moves"));
    assert_eq!(item.len(), 13);
}

#[test]
fn sessions_report_an_unreadable_move_record_as_store_unreadable() {
    for json_output in [false, true] {
        let world = world();
        moved_session(&world);
        // the sessions can still be read, their moves cannot
        world.sql().execute("DROP TABLE session_moves", []).unwrap();
        assert!(world.services.session.list_sessions(None, "", None).is_ok());
        let line: &[&str] = if json_output {
            &["sessions", "--json"]
        } else {
            &["sessions"]
        };
        assert_eq!(out(&world.run(line)), (2, STORE_UNREADABLE, ""), "{line:?}");
    }
}

// --- rows that cannot be read ---

#[test]
fn a_memory_that_cannot_be_read_is_left_out_of_every_list_and_refused_alone() {
    let world = world();
    let conn = world.sql();
    conn.execute(
        "UPDATE memories SET source_harness = ?1, source_method = ?2, created = ?3, \
         last_read_at = ?4 WHERE id = ?5",
        [
            "h\u{1b}x",
            "m\ny",
            "cr\u{1b}eated",
            "re\u{1b}ad",
            world.memory.id.as_str(),
        ],
    )
    .unwrap();
    let listed = format!(
        "{}  demo  ({})\n  (no memories)\n{}  global  (global)\n  (no memories)\n",
        world.project.id,
        text(&world.work),
        world.global_id
    );
    assert_eq!(out(&world.run(&["list"])), (0, listed.as_str(), ""));
    assert_eq!(
        out(&world.run(&["show", &world.memory.id])),
        (2, STORE_UNREADABLE, "")
    );
    assert_eq!(
        out(&world.run(&["search", "line"])),
        (0, "(no matches)\n", "")
    );
    let target = world.tmp.join("snap");
    let said = format!("exported 0 memories to {}\n", text(&target));
    assert_eq!(out(&export(&world, &target)), (0, said.as_str(), ""));
    let projects = format!(
        "- {} demo ({})\n- {} global (global)\n",
        world.project.id,
        text(&world.work),
        world.global_id
    );
    assert_eq!(
        fs::read_to_string(target.join("projects.md")).unwrap(),
        projects
    );
    assert_eq!(fs::read_dir(&target).unwrap().count(), 1);
}

#[test]
fn a_project_that_cannot_be_read_is_left_out_with_its_memories() {
    let world = world();
    let conn = world.sql();
    conn.execute(
        "UPDATE projects SET name = ?1 WHERE id = ?2",
        ["de\u{1b}mo  \u{e9}", world.project.id.as_str()],
    )
    .unwrap();
    let listed = format!("{}  global  (global)\n  (no memories)\n", world.global_id);
    assert_eq!(out(&world.run(&["list"])), (0, listed.as_str(), ""));
    assert_eq!(
        out(&world.run(&["search", "line"])),
        (0, "(no matches)\n", "")
    );
    // the memory itself is read by its id
    let done = world.run(&["show", &world.memory.id]);
    assert!(done.code == 0 && done.stdout.ends_with("---\nline one\nline two\n"));
    let target = world.tmp.join("snap");
    let said = format!("exported 0 memories to {}\n", text(&target));
    assert_eq!(out(&export(&world, &target)), (0, said.as_str(), ""));
    let projects = format!("- {} global (global)\n", world.global_id);
    assert_eq!(
        fs::read_to_string(target.join("projects.md")).unwrap(),
        projects
    );
}

#[test]
fn show_neutralises_a_time_that_is_none() {
    let world = world();
    let conn = world.sql();
    conn.execute(
        "UPDATE memories SET updated = ?1 WHERE id = ?2",
        ["\u{1b}[2J2026-09-2", world.memory.id.as_str()],
    )
    .unwrap();
    let done = world.run(&["show", &world.memory.id]);
    assert_eq!(done.code, 0);
    assert!(
        done.stdout
            .contains("\nupdated:  [2J2026-09-2\nversion: 1\n"),
        "{}",
        done.stdout
    );
    // the snapshot keeps it as it is stored, escaped
    let target = world.tmp.join("snap");
    assert_eq!(export(&world, &target).code, 0);
    let path = target
        .join(&world.project.id)
        .join(format!("{}.md", world.memory.id));
    let written = fs::read_to_string(path).unwrap();
    assert!(
        written.contains("\nupdated: \"\\u001b[2J2026-09-2\"\nversion: 1\n"),
        "{written}"
    );
}

#[test]
fn export_names_a_project_without_a_directory() {
    let world = world();
    let conn = world.sql();
    conn.execute(
        "UPDATE projects SET root = NULL WHERE id = ?1",
        [&world.project.id],
    )
    .unwrap();
    let target = world.tmp.join("snap");
    assert_eq!(export(&world, &target).code, 0);
    let projects = format!(
        "- {} demo (no directory)\n- {} global (global)\n",
        world.project.id, world.global_id
    );
    assert_eq!(
        fs::read_to_string(target.join("projects.md")).unwrap(),
        projects
    );
}

// --- a store that is not there ---

#[test]
fn a_store_that_does_not_exist_is_read_as_empty_and_not_created() {
    let world = world();
    let none = world.tmp.join("none");
    let root = text(&none);
    let table: [(&[&str], i32, &str); 7] = [
        (&["list"], 0, ""),
        (
            &["list", "--project", "zzzzzzzzzz"],
            2,
            "no such project: zzzzzzzzzz\n",
        ),
        (&["show", "zzzzzzzzzz"], 2, "no such memory: zzzzzzzzzz\n"),
        (&["search", "q"], 0, "(no matches)\n"),
        (&["sessions"], 0, "(no sessions)\n"),
        (&["sessions", "--json"], 0, "[]\n"),
        (
            &["sessions", "--json", "--project", "zzzzzzzzzz"],
            2,
            "no such project: zzzzzzzzzz\n",
        ),
    ];
    for (line, code, said) in table {
        let line = [line, &["--root", root]].concat();
        assert_eq!(out(&world.run(&line)), (code, said, ""), "{line:?}");
        assert!(!none.exists(), "{line:?}");
    }
}

#[test]
fn export_of_a_store_that_does_not_exist_writes_an_empty_snapshot() {
    let world = world();
    let new = world.tmp.join("new");
    let run =
        |target: &Path| world.run_from(&world.tmp, &["export", text(target), "--root", "new"]);
    // the place of the store itself: nothing is there before, and no read puts it there
    let said = format!("exported 0 memories to {}\n", text(&new));
    assert_eq!(out(&run(&new)), (0, said.as_str(), ""));
    assert_eq!(fs::read(new.join("projects.md")).unwrap(), b"");
    assert_eq!(fs::read_dir(&new).unwrap().count(), 1);
    let database = new.join(DATABASE_FILENAME);
    let said = format!("exported 0 memories to {}\n", text(&database));
    assert_eq!(out(&run(&database)), (0, said.as_str(), ""));
    // and now the database is a directory
    assert_eq!(out(&run(&new.join("sub"))), (2, STORE_UNREADABLE, ""));
    assert!(!new.join("sub").exists());
}
