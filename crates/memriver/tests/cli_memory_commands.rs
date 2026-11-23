//! `memriver history`, `restore`, `undo` and `delete` through the command line entry, over
//! real services on a temporary store. Where the store has to change while a command
//! waits, the change is made by the terminal it reads its answer from, or by the stream
//! it prints its plan to.

mod common;

use std::collections::BTreeSet;
use std::fs;
use std::io::{self, BufRead, Read, Write};
use std::path::Path;

use common::{Done, World, run_in, world};
use memriver::cli::{Io, run};
use memriver_core::models::changes::{Change, Confirmation, MemoryVersion, Op, SourceRef};
use memriver_core::models::errors::CoreError;
use memriver_core::models::helpers::new_id;
use memriver_core::models::memory::Memory;

const PROMPT: &str = "Proceed? [y/N] ";
const NOT_A_TERMINAL: &str =
    "refused: stdin is not a terminal; pass --yes to confirm non-interactively\n";
const DECLINED: &str = "aborted; nothing was changed\n";
const UNREADABLE: &str = "refused: the memory store could not be read\n";
const UNWRITABLE: &str = "refused: the memory store could not be written\n";
const STORE_FAILED: &str = "refused: the memory store failed; nothing was deleted\n";
const UNDOABLE_NO_MORE: &str =
    "  changes that touched them stay in the log but can no longer be undone\n";

/// Text the content policy refuses, and the rule that refuses it.
const SECRET: &str = "aws key AKIAIOSFODNN7EXAMPLE ok";
const SECRET_RULE: &str = "aws-access-token";

// --- the terminal and the stream that change the store ---

/// A terminal where `y` is typed to every question. The step runs while the first
/// question is open: a change another writer makes meanwhile.
struct Typing<'a> {
    step: Option<&'a mut dyn FnMut()>,
    line: &'static [u8],
}

impl Read for Typing<'_> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.fill_buf()?;
        let line = self.line;
        let count = line.len().min(buf.len());
        buf[..count].copy_from_slice(&line[..count]);
        self.consume(count);
        Ok(count)
    }
}

impl BufRead for Typing<'_> {
    fn fill_buf(&mut self) -> io::Result<&[u8]> {
        if self.line.is_empty() {
            if let Some(step) = self.step.take() {
                step();
            }
            self.line = b"y\n";
        }
        Ok(self.line)
    }

    fn consume(&mut self, count: usize) {
        self.line = &self.line[count..];
    }
}

/// What a command printed. The step runs once the first text is out: a change another
/// writer makes after the plan was read and shown.
struct Printed<'a> {
    text: Vec<u8>,
    step: Option<&'a mut dyn FnMut()>,
}

impl Write for Printed<'_> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.text.extend_from_slice(buf);
        if let Some(step) = self.step.take() {
            step();
        }
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn drive(
    world: &World,
    cwd: &Path,
    args: &[&str],
    stdin: &mut dyn BufRead,
    tty: bool,
    stdout: &mut Printed,
) -> Done {
    let argv: Vec<String> = std::iter::once("memriver")
        .chain(args.iter().copied())
        .map(str::to_string)
        .collect();
    let mut stderr = Vec::new();
    let mut io = Io {
        stdin,
        stdin_is_tty: tty,
        stdout: &mut *stdout,
        stderr: &mut stderr,
    };
    let code = run(argv, &world.env, &world.home, cwd, &mut io);
    Done {
        code,
        stdout: String::from_utf8(std::mem::take(&mut stdout.text)).unwrap(),
        stderr: String::from_utf8(stderr).unwrap(),
    }
}

/// At a terminal in `cwd` where `y` is typed to every question, `step` running while the
/// first one is open.
fn ask_meanwhile(world: &World, cwd: &Path, args: &[&str], step: &mut dyn FnMut()) -> Done {
    let mut stdin = Typing {
        step: Some(step),
        line: b"",
    };
    let mut stdout = Printed {
        text: Vec::new(),
        step: None,
    };
    drive(world, cwd, args, &mut stdin, true, &mut stdout)
}

/// With no terminal, `step` running right after the first text was printed.
fn run_meanwhile(world: &World, args: &[&str], step: &mut dyn FnMut()) -> Done {
    let mut stdout = Printed {
        text: Vec::new(),
        step: Some(step),
    };
    drive(
        world,
        &world.work,
        args,
        &mut "".as_bytes(),
        false,
        &mut stdout,
    )
}

/// At a terminal in `cwd` where `answer` is typed.
fn ask_from(world: &World, cwd: &Path, args: &[&str], answer: &str) -> Done {
    run_in(&world.env, &world.home, cwd, args, answer, true)
}

fn said(done: &Done) -> (i32, &str, &str) {
    (done.code, done.stdout.as_str(), done.stderr.as_str())
}

// --- the scenarios ---

fn versions(world: &World, memory_id: &str) -> Vec<MemoryVersion> {
    let mut versions = world.services.memory.versions(memory_id).unwrap();
    versions.sort_by_key(|version| version.version);
    versions
}

fn change_of(version: &MemoryVersion) -> &Change {
    version.change.as_ref().unwrap()
}

/// The change that made the newest version.
fn last_change(world: &World, memory_id: &str) -> Change {
    change_of(versions(world, memory_id).last().unwrap()).clone()
}

fn refs(sources: &[(&str, i64)]) -> Vec<SourceRef> {
    sources
        .iter()
        .map(|(memory_id, version)| SourceRef {
            memory_id: memory_id.to_string(),
            version: *version,
        })
        .collect()
}

fn create(
    project_id: &str,
    type_: &str,
    description: &str,
    body: &str,
    cited: &[(&str, i64)],
    standing: Option<i64>,
) -> Op {
    let mut op = Op::create(project_id, type_, description, body);
    if let Op::Create { sources, level, .. } = &mut op {
        *sources = refs(cited);
        *level = standing;
    }
    op
}

fn edit(
    memory_id: &str,
    expected_version: i64,
    description: Option<&str>,
    body: Option<&str>,
    sources: Option<&[(&str, i64)]>,
    level: Option<i64>,
) -> Op {
    Op::Update {
        memory_id: memory_id.to_string(),
        expected_version,
        description: description.map(str::to_string),
        body: body.map(str::to_string),
        sources: sources.map(refs),
        level,
        tags: None,
    }
}

fn dream_update(world: &World, memory_id: &str, expected_version: i64, body: &str) -> Change {
    let update = edit(memory_id, expected_version, None, Some(body), None, None);
    world
        .services
        .memory
        .apply(&[update], "dream", Some("codex"))
        .unwrap()
}

/// A global memory citing `sources`; its id.
fn global_memory(world: &World, sources: &[(&str, i64)]) -> String {
    let shared = create(
        &world.global_id,
        "project",
        "a shared rule",
        "shared body",
        sources,
        None,
    );
    let change = world
        .services
        .memory
        .apply(&[shared], "dream", Some("codex"))
        .unwrap();
    change.steps[0].memory_id.clone()
}

fn record(world: &World, content: &str, description: &str) -> Memory {
    world
        .services
        .memory
        .record(content, "project", true, "t", description, &world.context)
        .unwrap()
}

fn soft_delete(world: &World, memory_id: &str, expected_version: i64) {
    let delete = Op::soft_delete(memory_id, expected_version);
    world
        .services
        .memory
        .apply(&[delete], "human", None)
        .unwrap();
}

fn gone(world: &World, memory_id: &str) -> bool {
    matches!(
        world.services.memory.show(memory_id, true),
        Err(CoreError::MemoryNotFound(_))
    )
}

fn current(world: &World, memory_id: &str) -> Memory {
    world.services.memory.show(memory_id, false).unwrap()
}

fn plan_code(world: &World, memory_id: &str) -> String {
    let plan = world.services.maintenance.plan_hard_delete(memory_id);
    plan.unwrap().code
}

fn purge(world: &World, memory_id: &str) {
    let plan = world
        .services
        .maintenance
        .plan_hard_delete(memory_id)
        .unwrap();
    let expected = plan.expected();
    world
        .services
        .maintenance
        .hard_delete(memory_id, Confirmation::Expected(&expected))
        .unwrap();
}

/// Overwrites the stored text of `memory_id` so that its state now fails the content
/// policy: one statement behind the services' backs.
fn plant_policy_hit(world: &World, memory_id: &str) {
    let conn = world.sql();
    conn.execute(
        "UPDATE memories SET body = ?1 WHERE id = ?2",
        (SECRET, memory_id),
    )
    .unwrap();
    conn.execute(
        "UPDATE memory_versions SET body = ?1 WHERE memory_id = ?2",
        (SECRET, memory_id),
    )
    .unwrap();
}

/// The same for one recorded version only.
fn plant_policy_hit_in_version(world: &World, memory_id: &str, version: i64) {
    world
        .sql()
        .execute(
            "UPDATE memory_versions SET body = ?1 WHERE memory_id = ?2 AND version = ?3",
            (SECRET, memory_id, version),
        )
        .unwrap();
}

/// A connection that plants rows the references between tables would refuse.
fn unkeyed(world: &World) -> rusqlite::Connection {
    let conn = world.sql();
    conn.pragma_update(None, "foreign_keys", false).unwrap();
    conn
}

/// A string as a shell takes it as one word.
fn shell_word(text: &str) -> String {
    let plain = |c: char| c.is_ascii_alphanumeric() || "@%+=:,./-_".contains(c);
    if !text.is_empty() && text.chars().all(plain) {
        return text.to_string();
    }
    format!("'{}'", text.replace('\'', "'\"'\"'"))
}

fn store_of(world: &World) -> &str {
    world.store.to_str().unwrap()
}

fn command_tail(world: &World, code: &str) -> String {
    format!("--confirm {code} --root {}\n", shell_word(store_of(world)))
}

fn restore_plan(
    memory_id: &str,
    from: i64,
    to: i64,
    now: &str,
    target: &str,
    changes: &str,
) -> String {
    format!(
        "memriver restore: {memory_id} from v{from} to the state of v{to}\n  now (v{from}): \
         {now}\n  target (v{to}): {target}\n  changes: {changes}\n"
    )
}

// --- history ---

#[test]
fn history_lists_every_version_with_its_change_and_state() {
    let world = world();
    let id = world.memory.id.as_str();
    let update = dream_update(&world, id, 1, "line three");
    let shared = global_memory(&world, &[(id, 2)]);
    soft_delete(&world, id, 2);

    let done = world.run(&["history", id]);
    let stored = versions(&world, id);
    let (first, last) = (change_of(&stored[0]), change_of(&stored[2]));
    let expected = format!(
        "v1  {}  mcp (t)  change {}  the cue\nv2  {}  dream (codex)  change {}  the cue\nv3  {}  \
         human  change {}  [deleted]  the cue\n",
        first.at, first.change_id, update.at, update.change_id, last.at, last.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));

    let done = world.run(&["history", &shared]);
    let made = last_change(&world, &shared);
    let expected = format!(
        "v1  {}  dream (codex)  change {}  a shared rule\n    sources: {id} v2\n",
        made.at, made.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
}

#[test]
fn history_show_prints_one_version_in_full() {
    let world = world();
    let id = world.memory.id.as_str();
    dream_update(&world, id, 1, "line three");
    let done = world.run(&["history", id, "--show", "1"]);
    let made = versions(&world, id)[0].change.clone().unwrap();
    let expected = format!(
        "id: {id}\nversion: 1\ntype: project\ntrust: agent\nsync: true\ndeleted: false\nchange: \
         {}  mcp (t)  change {}\nsources: -\ndescription: the cue\n---\nline one\nline two\n",
        made.at, made.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
}

#[test]
fn history_show_names_the_sources_and_the_deleted_state_of_a_version() {
    let world = world();
    let id = world.memory.id.as_str();
    let shared = global_memory(&world, &[(id, 1)]);
    soft_delete(&world, &shared, 1);
    let done = world.run(&["history", &shared, "--show", "2"]);
    let made = last_change(&world, &shared);
    let expected = format!(
        "id: {shared}\nversion: 2\ntype: project\ntrust: agent\nsync: true\ndeleted: \
         true\nchange: {}  human  change {}\nsources: {id} v1\ndescription: a shared \
         rule\n---\nshared body\n",
        made.at, made.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
}

#[test]
fn history_refuses_an_unknown_memory_or_version() {
    let world = world();
    let id = world.memory.id.as_str();
    let unknown = new_id();
    let done = world.run(&["history", &unknown]);
    assert_eq!(
        said(&done),
        (2, format!("no such memory: {unknown}\n").as_str(), "")
    );
    let done = world.run(&["history", id, "--show", "7"]);
    assert_eq!(
        said(&done),
        (2, format!("no such version: {id} v7\n").as_str(), "")
    );
}

// a source id no write could have produced never reaches the terminal as bytes: the
// store is answered as one that cannot be read
#[test]
fn history_of_a_version_with_an_undecodable_source_id_is_store_unreadable() {
    let world = world();
    let id = world.memory.id.as_str();
    let source = record(&world, "source", "src");
    let cite = edit(id, 1, None, None, Some(&[(&source.id, 1)]), None);
    world.services.memory.apply(&[cite], "human", None).unwrap();
    unkeyed(&world)
        .execute(
            "UPDATE memory_sources SET source_id = CAST(X'80616161616161616161' AS TEXT) WHERE \
             memory_id = ?1",
            [id],
        )
        .unwrap();
    assert_eq!(said(&world.run(&["history", id])), (2, UNREADABLE, ""));
}

// a row carried over from an older store has no change
#[test]
fn a_version_with_no_recorded_change_shows_imported() {
    let world = world();
    let id = world.memory.id.as_str();
    world
        .sql()
        .execute(
            "UPDATE memory_versions SET change_id = NULL WHERE memory_id = ?1",
            [id],
        )
        .unwrap();
    assert_eq!(
        said(&world.run(&["history", id])),
        (0, "v1  imported  the cue\n", "")
    );
    let done = world.run(&["history", id, "--show", "1"]);
    let expected = format!(
        "id: {id}\nversion: 1\ntype: project\ntrust: agent\nsync: true\ndeleted: false\nchange: \
         imported\nsources: -\ndescription: the cue\n---\nline one\nline two\n"
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
}

// --- restore ---

#[test]
fn restore_shows_both_states_then_records_a_human_change() {
    let world = world();
    let id = world.memory.id.as_str();
    let rewrite = edit(id, 1, Some("new cue"), Some("rewritten"), None, None);
    world
        .services
        .memory
        .apply(&[rewrite], "dream", None)
        .unwrap();

    let done = world.ask(&["restore", id, "--to", "1"], "y\n");
    let stored = versions(&world, id);
    let latest = stored.last().unwrap();
    let made = change_of(latest);
    let expected = format!(
        "{}{PROMPT}restored {id} to the state of v1 as v3 (change {})\n",
        restore_plan(id, 2, 1, "new cue", "the cue", "content"),
        made.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    assert_eq!(
        (
            latest.version,
            latest.description.as_str(),
            latest.body.as_str()
        ),
        (3, "the cue", "line one\nline two")
    );
    assert_eq!(
        (made.changed_by.as_str(), made.changed_via.as_deref()),
        ("human", None)
    );
}

#[test]
fn restoring_a_live_version_undeletes() {
    let world = world();
    let id = world.memory.id.as_str();
    world
        .services
        .memory
        .delete(id, 1, &world.context, "mcp", None)
        .unwrap();
    let done = world.ask(&["restore", id, "--to", "1"], "y\n");
    let expected = format!(
        "{}{PROMPT}restored {id} to the state of v1 as v3 (change {})\n",
        restore_plan(id, 2, 1, "the cue  [deleted]", "the cue", "deleted state"),
        last_change(&world, id).change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    assert_eq!(current(&world, id).version, 3);
}

#[test]
fn restore_declined_or_without_a_terminal_changes_nothing() {
    let world = world();
    let id = world.memory.id.as_str();
    dream_update(&world, id, 1, "line three");
    let done = world.ask(&["restore", id, "--to", "1"], "n\n");
    assert_eq!(done.code, 1);
    assert!(done.stdout.ends_with(DECLINED), "{}", done.stdout);
    let done = world.run(&["restore", id, "--to", "1"]);
    assert_eq!(done.code, 2);
    assert!(
        done.stdout
            .contains("pass --yes to confirm non-interactively")
    );
    assert!(!done.stdout.contains(PROMPT));
    assert_eq!(versions(&world, id).len(), 2);
}

// a conflict of versions reads the memory again and asks again
#[test]
fn a_restore_conflict_rereads_and_asks_again() {
    let world = world();
    let id = world.memory.id.as_str();
    dream_update(&world, id, 1, "line three");
    let done = ask_meanwhile(
        &world,
        &world.work,
        &["restore", id, "--to", "1"],
        &mut || {
            dream_update(&world, id, 2, "line four");
        },
    );
    let expected = format!(
        "{}{PROMPT}{id} changed while waiting; nothing was changed yet\n{}{PROMPT}restored {id} \
         to the state of v1 as v4 (change {})\n",
        restore_plan(id, 2, 1, "the cue", "the cue", "content"),
        restore_plan(id, 3, 1, "the cue", "the cue", "content"),
        last_change(&world, id).change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    assert_eq!(current(&world, id).body, "line one\nline two");
}

#[test]
fn a_restore_conflict_under_yes_fails_instead_of_asking() {
    let world = world();
    let id = world.memory.id.as_str();
    dream_update(&world, id, 1, "line three");
    let done = run_meanwhile(&world, &["restore", id, "--to", "1", "--yes"], &mut || {
        dream_update(&world, id, 2, "line four");
    });
    let expected = format!(
        "{}refused (version): {id} changed while waiting; nothing was changed; run the command \
         again\n",
        restore_plan(id, 2, 1, "the cue", "the cue", "content")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(current(&world, id).version, 3);
    assert_eq!(current(&world, id).body, "line four");
}

// the state a version recorded is checked against today's policy; the rule is named
#[test]
fn a_restore_refused_by_the_content_policy_prints_the_rule() {
    let world = world();
    let id = world.memory.id.as_str();
    dream_update(&world, id, 1, "line three");
    plant_policy_hit_in_version(&world, id, 1);
    let done = world.ask(&["restore", id, "--to", "1"], "y\n");
    let expected = format!(
        "{}{PROMPT}refused (content policy): the state of v1 fails rule {SECRET_RULE}; nothing \
         was changed\n",
        restore_plan(id, 2, 1, "the cue", "the cue", "content")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(versions(&world, id).len(), 2);
}

#[test]
fn restore_to_an_unknown_version_is_refused() {
    let world = world();
    let id = world.memory.id.as_str();
    let done = world.ask(&["restore", id, "--to", "5"], "");
    assert_eq!(
        said(&done),
        (2, format!("no such version: {id} v5\n").as_str(), "")
    );
}

// --- undo ---

/// Two project memories merged into one global entry: the change, the merged entry and
/// the two.
fn merge(world: &World) -> (Change, String, String, String) {
    let first = world.memory.id.clone();
    let second = record(world, "another fact", "second cue").id;
    let merged = create(
        &world.global_id,
        "project",
        "merged cue",
        "both facts",
        &[(&first, 1), (&second, 1)],
        None,
    );
    let ops = [
        merged,
        Op::soft_delete(&first, 1),
        Op::soft_delete(&second, 1),
    ];
    let change = world
        .services
        .memory
        .apply(&ops, "dream", Some("codex"))
        .unwrap();
    let merged = change.steps[0].memory_id.clone();
    (change, merged, first, second)
}

#[test]
fn undo_shows_the_change_and_its_inverse_then_round_trips() {
    let world = world();
    let (change, merged, first, second) = merge(&world);
    let (project, change_id) = (world.project.id.as_str(), change.change_id.as_str());
    let done = world.ask(&["undo", change_id], "y\n");
    let undo = last_change(&world, &first);
    let expected = format!(
        "memriver undo: change {change_id} at {} by dream (codex)\n  {merged}  global  create \
         v1  merged cue\n  {first}  project {project}  soft_delete v1->v2  the cue\n  {second}  \
         project {project}  soft_delete v1->v2  second cue\nthe undo:\n  {merged}: soft \
         delete\n  {first}: restore the state of v1\n  {second}: restore the state of \
         v1\n{PROMPT}undone change {change_id} by change {}\n",
        change.at, undo.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    assert_eq!(
        (
            undo.undoes.as_deref(),
            undo.changed_by.as_str(),
            undo.changed_via.as_deref()
        ),
        (Some(change_id), "human", None)
    );
    assert!(matches!(
        world.services.memory.show(&merged, false),
        Err(CoreError::MemoryNotFound(_))
    ));
    assert_eq!(current(&world, &first).version, 3);

    // an undo is a change like any other, and can itself be undone
    let done = world.ask(&["undo", &undo.change_id], "y\n");
    let again = last_change(&world, &first);
    let expected = format!(
        "memriver undo: change {} at {} by human, an undo of {change_id}\n  {merged}  global  \
         soft_delete v1->v2  merged cue\n  {first}  project {project}  restore v2->v3  the \
         cue\n  {second}  project {project}  restore v2->v3  second cue\nthe undo:\n  {merged}: \
         restore the state of v1\n  {first}: restore the state of v2\n  {second}: restore the \
         state of v2\n{PROMPT}undone change {} by change {}\n",
        undo.change_id, undo.at, undo.change_id, again.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
}

#[test]
fn undo_refusals_print_their_reason() {
    let world = world();
    let id = world.memory.id.as_str();
    let unknown = new_id();
    let done = world.ask(&["undo", &unknown], "");
    let expected = format!("refused (not-found): no change {unknown}\n");
    assert_eq!(said(&done), (2, expected.as_str(), ""));

    let created = last_change(&world, id);
    dream_update(&world, id, 1, "line three");
    let done = world.ask(&["undo", &created.change_id], "y\n");
    let expected = format!(
        "memriver undo: change {created} at {} by mcp (t)\n  {id}  project {}  create v1  the \
         cue\nthe undo:\n  {id}: soft delete\n{PROMPT}refused (changed): {id} changed after \
         change {created}; nothing was changed; use memriver history and memriver restore\n",
        created.at,
        world.project.id,
        created = created.change_id
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
}

#[test]
fn undo_of_a_change_a_hard_delete_cut_is_refused() {
    let world = world();
    let project = world.project.id.as_str();
    let ops = [
        create(project, "project", "x cue", "x", &[], None),
        create(project, "project", "y cue", "y", &[], None),
    ];
    let pair = world.services.memory.apply(&ops, "dream", None).unwrap();
    let (kept, doomed) = (&pair.steps[0].memory_id, &pair.steps[1].memory_id);
    purge(&world, doomed);
    let done = world.ask(&["undo", &pair.change_id], "y\n");
    let expected = format!(
        "memriver undo: change {pair} at {} by dream\n  {kept}  project {project}  create v1  x \
         cue\n  (1 more step removed by a hard delete)\nthe undo:\n  {kept}: soft \
         delete\n{PROMPT}refused (hard-deleted): a hard delete removed part of change {pair}; it \
         cannot be undone\n",
        pair.at,
        pair = pair.change_id
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(current(&world, kept).version, 1);
}

#[test]
fn an_undo_refused_by_the_content_policy_prints_the_rule() {
    let world = world();
    let id = world.memory.id.as_str();
    let update = dream_update(&world, id, 1, "line three");
    plant_policy_hit_in_version(&world, id, 1);
    let done = world.ask(&["undo", &update.change_id], "y\n");
    let expected = format!(
        "memriver undo: change {} at {} by dream (codex)\n  {id}  project {}  update v1->v2  the \
         cue\nthe undo:\n  {id}: restore the state of v1\n{PROMPT}refused (content policy): the \
         undo would restore content failing rule {SECRET_RULE}; nothing was changed\n",
        update.change_id, update.at, world.project.id
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(versions(&world, id).len(), 2);
}

#[test]
fn an_undo_or_restore_past_a_metadata_limit_prints_the_setting() {
    let world = world();
    let refusal = "refused (standing_max_count): the result would exceed the standing_max_count \
                   setting; nothing was changed\n";
    let global = world.global_id.as_str();
    let rule = |description: &str, body: &str| {
        let op = create(global, "feedback", description, body, &[], Some(1));
        let change = world.services.memory.apply(&[op], "dream", None).unwrap();
        change.steps[0].memory_id.clone()
    };
    let first = rule("rule one", "one");
    let lower = edit(&first, 1, None, None, None, Some(2));
    let demote = world
        .services
        .memory
        .apply(&[lower], "dream", None)
        .unwrap();
    rule("rule two", "two");
    fs::write(
        world.store.join("settings.toml"),
        "standing_max_count = 1\n",
    )
    .unwrap();

    let done = world.ask(&["undo", &demote.change_id], "y\n");
    let expected = format!(
        "memriver undo: change {} at {} by dream\n  {first}  global  update v1->v2  rule \
         one\nthe undo:\n  {first}: restore the state of v1\n{PROMPT}{refusal}",
        demote.change_id, demote.at
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));

    let done = world.ask(&["restore", &first, "--to", "1"], "y\n");
    let expected = format!(
        "{}{PROMPT}{refusal}",
        restore_plan(&first, 2, 1, "rule one", "rule one", "level and tags")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(current(&world, &first).version, 2);
}

#[test]
fn undo_declined_changes_nothing() {
    let world = world();
    let id = world.memory.id.as_str();
    let created = last_change(&world, id);
    let done = world.ask(&["undo", &created.change_id], "n\n");
    assert_eq!(done.code, 1);
    assert!(done.stdout.ends_with(DECLINED), "{}", done.stdout);
    assert_eq!(current(&world, id).version, 1);
}

// --- delete (soft) ---

fn soft_plan(memory_id: &str, place: &str, cue: &str) -> String {
    format!("memriver delete: {memory_id} [project] in {place}: {cue}  (soft)\n")
}

fn in_project(world: &World) -> String {
    format!("project {}", world.project.id)
}

#[test]
fn soft_delete_from_the_project_directory_is_a_human_change() {
    let world = world();
    let id = world.memory.id.as_str();
    let done = world.ask(&["delete", id, "--version", "1"], "y\n");
    let expected = format!(
        "{}{PROMPT}deleted {id}\n",
        soft_plan(id, &in_project(&world), "the cue")
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    let stored = versions(&world, id);
    let latest = stored.last().unwrap();
    let made = change_of(latest);
    assert!(latest.deleted);
    assert_eq!(
        (made.changed_by.as_str(), made.changed_via.as_deref()),
        ("human", None)
    );
}

#[test]
fn soft_delete_needs_the_current_version() {
    let world = world();
    let id = world.memory.id.as_str();
    let done = world.ask(&["delete", id, "--version", "5"], "y\n");
    let expected = format!(
        "{}{PROMPT}refused: {id} changed since version 5; run memriver show {id} and retry\n",
        soft_plan(id, &in_project(&world), "the cue")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(current(&world, id).version, 1);
}

#[test]
fn soft_delete_from_outside_the_project_names_the_owning_project() {
    let world = world();
    let id = world.memory.id.as_str();
    let done = ask_from(&world, &world.tmp, &["delete", id, "--version", "1"], "y\n");
    let expected = format!(
        "refused: {id} belongs to project {}, not this directory's project; run memriver delete \
         from that project's directory\n",
        world.project.id
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(current(&world, id).version, 1);
}

// a global memory is deleted by id, as a change of the person at the terminal
#[test]
fn a_global_memory_is_soft_deleted_by_id_from_anywhere() {
    let world = world();
    let shared = global_memory(&world, &[]);
    let done = ask_from(
        &world,
        &world.tmp,
        &["delete", &shared, "--version", "1"],
        "y\n",
    );
    let expected = format!(
        "{}{PROMPT}deleted {shared}\n",
        soft_plan(&shared, "global", "a shared rule")
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    let stored = versions(&world, &shared);
    let latest = stored.last().unwrap();
    let made = change_of(latest);
    assert!(latest.deleted);
    assert_eq!(
        (made.changed_by.as_str(), made.changed_via.as_deref()),
        ("human", None)
    );
}

// every write is checked against the content policy, a soft delete included: a memory
// whose stored text now hits the policy cannot be soft-deleted, and the hard delete is
// the way out
#[test]
fn a_soft_delete_of_a_project_memory_whose_text_now_hits_the_policy_is_refused() {
    let world = world();
    let id = world.memory.id.as_str();
    plant_policy_hit(&world, id);
    let direct = world
        .services
        .memory
        .apply(&[Op::soft_delete(id, 1)], "human", None);
    assert!(format!("{direct:?}").contains(SECRET_RULE), "{direct:?}");
    let before = versions(&world, id);

    let done = world.ask(&["delete", id, "--version", "1"], "y\n");

    let expected = format!(
        "{}{PROMPT}refused (content policy): {id} fails rule {SECRET_RULE}; remove it with \
         memriver delete {id} --hard\n",
        soft_plan(id, &in_project(&world), "the cue")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(versions(&world, id), before);
}

#[test]
fn a_soft_delete_of_a_global_memory_whose_text_now_hits_the_policy_is_refused() {
    let world = world();
    let shared = global_memory(&world, &[]);
    plant_policy_hit(&world, &shared);
    let direct = world
        .services
        .memory
        .apply(&[Op::soft_delete(&shared, 1)], "human", None);
    assert!(format!("{direct:?}").contains(SECRET_RULE), "{direct:?}");
    let before = versions(&world, &shared);

    let done = ask_from(
        &world,
        &world.tmp,
        &["delete", &shared, "--version", "1"],
        "y\n",
    );

    let expected = format!(
        "{}{PROMPT}refused (content policy): {shared} fails rule {SECRET_RULE}; remove it with \
         memriver delete {shared} --hard\n",
        soft_plan(&shared, "global", "a shared rule")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(versions(&world, &shared), before);
}

#[test]
fn a_global_memory_hard_deleted_meanwhile_is_no_such_memory() {
    let world = world();
    let shared = global_memory(&world, &[]);
    let args = ["delete", &shared, "--version", "1"];
    let done = ask_meanwhile(&world, &world.tmp, &args, &mut || purge(&world, &shared));
    let expected = format!(
        "{}{PROMPT}no such memory: {shared}\n",
        soft_plan(&shared, "global", "a shared rule")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
}

#[test]
fn soft_delete_declined_at_eof_or_without_a_terminal_changes_nothing() {
    let world = world();
    let id = world.memory.id.as_str();
    let args = ["delete", id, "--version", "1"];
    assert_eq!(world.ask(&args, "n\n").code, 1);
    assert_eq!(world.ask(&args, "").code, 1);
    let done = world.run(&args);
    assert_eq!(done.code, 2);
    assert!(done.stdout.contains("stdin is not a terminal"));
    assert!(!done.stdout.contains(PROMPT));
    assert_eq!(current(&world, id).version, 1);
}

#[test]
fn soft_delete_with_yes_skips_the_prompt() {
    let world = world();
    let id = world.memory.id.as_str();
    let done = world.run(&["delete", id, "--version", "1", "--yes"]);
    let expected = format!(
        "{}deleted {id}\n",
        soft_plan(id, &in_project(&world), "the cue")
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
}

// --- delete --hard ---

/// The world's memory and a global memory citing its first version: a plan of two.
fn cited(world: &World) -> (String, String) {
    let target = world.memory.id.clone();
    let citing = global_memory(world, &[(&target, 1)]);
    (target, citing)
}

fn plan_of_two(world: &World, target: &str, citing: &str) -> String {
    format!(
        "memriver delete --hard: removes 2 memories with every version, source and read:\n  \
         {target}  project {}  v1  the cue\n  {citing}  global  v1  a shared rule\n    because \
         {citing} v1 cites {target} v1\n{UNDOABLE_NO_MORE}",
        world.project.id
    )
}

/// The same once `late` cites the target too: the two citing memories stand by id.
fn plan_of_three(world: &World, target: &str, citing: &str, late: &str) -> String {
    let mut text = format!(
        "memriver delete --hard: removes 3 memories with every version, source and read:\n  \
         {target}  project {}  v1  the cue\n",
        world.project.id
    );
    for citer in BTreeSet::from([citing, late]) {
        text.push_str(&format!(
            "  {citer}  global  v1  a shared rule\n    because {citer} v1 cites {target} v1\n"
        ));
    }
    text + UNDOABLE_NO_MORE
}

fn purged_ids(stdout: &str) -> BTreeSet<&str> {
    let line = stdout.lines().last().unwrap();
    line.strip_prefix("purged ").unwrap().split(", ").collect()
}

// --dry-run: the plan and the command with its code; nothing is deleted
#[test]
fn hard_dry_run_prints_the_plan_and_the_confirm_command() {
    let world = world();
    let (target, citing) = cited(&world);
    let args = [
        "delete",
        &target,
        "--hard",
        "--dry-run",
        "--root",
        store_of(&world),
    ];
    let done = world.ask(&args, "");
    let expected = format!(
        "{}to delete exactly these, run: memriver delete {target} --hard {}",
        plan_of_two(&world, &target, &citing),
        command_tail(&world, &plan_code(&world, &target))
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    assert!(!gone(&world, &target) && !gone(&world, &citing));
}

// a referrer id no write could have produced never reaches the plan: the dry run
// refuses cleanly
#[test]
fn hard_dry_run_refuses_cleanly_when_a_referrer_id_is_not_addressable() {
    let world = world();
    let target = world.memory.id.clone();
    global_memory(&world, &[(&target, 1)]);
    let citer = global_memory(&world, &[(&target, 1)]);
    let conn = unkeyed(&world);
    for (table, column) in [("memories", "id"), ("memory_sources", "memory_id")] {
        let sql = format!(
            "UPDATE {table} SET {column} = CAST(X'80616161616161616161' AS TEXT) WHERE {column} \
             = ?1"
        );
        conn.execute(&sql, [&citer]).unwrap();
    }

    let done = world.ask(&["delete", &target, "--hard", "--dry-run"], "");

    assert_eq!(said(&done), (2, STORE_FAILED, ""));
}

// --confirm CODE: nothing is asked; the code is all that links the two steps
#[test]
fn confirm_with_the_printed_code_purges_every_member() {
    let world = world();
    let (target, citing) = cited(&world);
    let code = plan_code(&world, &target);
    let done = world.ask(&["delete", &target, "--hard", "--confirm", &code], "");
    assert_eq!((done.code, done.stderr.as_str()), (0, ""));
    assert_eq!(done.stdout.lines().count(), 1, "{}", done.stdout);
    assert_eq!(
        purged_ids(&done.stdout),
        BTreeSet::from([target.as_str(), citing.as_str()])
    );
    assert!(gone(&world, &target) && gone(&world, &citing));
}

// a code of a plan that no longer holds deletes nothing; the new plan and its command
// are printed
#[test]
fn a_stale_code_deletes_nothing_and_prints_the_new_plan() {
    let world = world();
    let (target, citing) = cited(&world);
    let stale = plan_code(&world, &target);
    let late = global_memory(&world, &[(&target, 1)]);
    let root = store_of(&world);
    let done = world.ask(
        &[
            "delete",
            &target,
            "--hard",
            "--confirm",
            &stale,
            "--root",
            root,
        ],
        "",
    );
    let fresh = plan_code(&world, &target);
    assert_ne!(fresh, stale);
    let expected = format!(
        "refused: the plan changed since it was printed; nothing was deleted\n{}to delete \
         exactly these, run: memriver delete {target} --hard {}",
        plan_of_three(&world, &target, &citing, &late),
        command_tail(&world, &fresh)
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert!(
        ![&target, &citing, &late]
            .into_iter()
            .any(|id| gone(&world, id))
    );
}

// the default: the plan, the question, the delete of the set that was shown; a plan that
// changed is shown and asked about again
#[test]
fn interactive_hard_delete_asks_again_when_the_plan_changes() {
    let world = world();
    let (target, citing) = cited(&world);
    let mut late = Vec::new();
    let done = ask_meanwhile(
        &world,
        &world.work,
        &["delete", &target, "--hard"],
        &mut || {
            late.push(global_memory(&world, &[(&target, 1)]));
        },
    );
    let late = late.pop().unwrap();
    let asked = format!(
        "{}{PROMPT}the plan changed while waiting; nothing was deleted yet\n{}{PROMPT}",
        plan_of_two(&world, &target, &citing),
        plan_of_three(&world, &target, &citing, &late)
    );
    assert_eq!((done.code, done.stderr.as_str()), (0, ""));
    let result = done.stdout.strip_prefix(&asked).expect(&done.stdout);
    assert_eq!(
        purged_ids(result),
        BTreeSet::from([target.as_str(), citing.as_str(), late.as_str()])
    );
    assert_eq!(result.lines().count(), 1);
    assert!(
        [&target, &citing, &late]
            .into_iter()
            .all(|id| gone(&world, id))
    );
}

#[test]
fn hard_delete_under_yes_fails_when_the_plan_changes() {
    let world = world();
    let (target, citing) = cited(&world);
    let mut late = Vec::new();
    let done = run_meanwhile(&world, &["delete", &target, "--hard", "--yes"], &mut || {
        late.push(global_memory(&world, &[(&target, 1)]));
    });
    let expected = format!(
        "{}refused: the plan changed while waiting; nothing was deleted; run the command again\n",
        plan_of_two(&world, &target, &citing)
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert!(
        ![&target, &citing, &late[0]]
            .into_iter()
            .any(|id| gone(&world, id))
    );
}

#[test]
fn hard_delete_declined_deletes_nothing() {
    let world = world();
    let (target, citing) = cited(&world);
    let done = world.ask(&["delete", &target, "--hard"], "n\n");
    assert_eq!(done.code, 1);
    assert!(done.stdout.ends_with(DECLINED), "{}", done.stdout);
    assert!(!gone(&world, &target) && !gone(&world, &citing));
}

#[test]
fn hard_delete_of_an_unknown_memory_is_refused() {
    let world = world();
    let unknown = new_id();
    let done = world.ask(&["delete", &unknown, "--hard", "--dry-run"], "");
    assert_eq!(
        said(&done),
        (2, format!("no such memory: {unknown}\n").as_str(), "")
    );
}

// --- through the command line ---

#[test]
fn cli_wires_history_and_a_hard_dry_run() {
    let world = world();
    let id = world.memory.id.as_str();
    let root = store_of(&world);
    let done = world.run(&["history", id, "--root", root]);
    assert_eq!(done.code, 0);
    assert!(done.stdout.starts_with("v1  "), "{}", done.stdout);
    let done = world.run(&["delete", id, "--hard", "--dry-run", "--root", root]);
    assert_eq!(done.code, 0);
    let command = format!("memriver delete {id} --hard --confirm ");
    assert!(done.stdout.contains(&command), "{}", done.stdout);
    assert!(!gone(&world, id));
}

// --- the confirmation, for each command that writes ---

/// A memory at its second version, and the four commands that would change it: restore,
/// undo, soft delete and hard delete.
fn four_writes(world: &World) -> Vec<(&'static str, Vec<String>)> {
    let id = world.memory.id.as_str();
    let update = dream_update(world, id, 1, "line three");
    let lines = [
        ("restored ", vec!["restore", id, "--to", "1"]),
        ("undone change ", vec!["undo", update.change_id.as_str()]),
        ("deleted ", vec!["delete", id, "--version", "2"]),
        ("purged ", vec!["delete", id, "--hard"]),
    ];
    lines
        .into_iter()
        .map(|(done, line)| (done, line.into_iter().map(str::to_string).collect()))
        .collect()
}

fn strs(line: &[String]) -> Vec<&str> {
    line.iter().map(String::as_str).collect()
}

/// What each of the four prints before it asks.
fn four_plans(world: &World) -> [String; 4] {
    let id = world.memory.id.as_str();
    let project = in_project(world);
    let update = last_change(world, id);
    [
        restore_plan(id, 2, 1, "the cue", "the cue", "content"),
        format!(
            "memriver undo: change {} at {} by dream (codex)\n  {id}  {project}  update v1->v2  \
             the cue\nthe undo:\n  {id}: restore the state of v1\n",
            update.change_id, update.at
        ),
        soft_plan(id, &project, "the cue"),
        format!(
            "memriver delete --hard: removes 1 memory with every version, source and read:\n  \
             {id}  {project}  v2  the cue\n{UNDOABLE_NO_MORE}"
        ),
    ]
}

#[test]
fn non_tty_without_yes_is_exit_2() {
    let world = world();
    let writes = four_writes(&world);
    let plans = four_plans(&world);
    let before = world.files();
    for ((_, line), plan) in writes.iter().zip(&plans) {
        // what stands on stdin is never read
        for answer in ["", "y\n"] {
            let done = run_in(
                &world.env,
                &world.home,
                &world.work,
                &strs(line),
                answer,
                false,
            );
            let expected = format!("{plan}{NOT_A_TERMINAL}");
            assert_eq!(said(&done), (2, expected.as_str(), ""), "{line:?}");
            assert_eq!(world.files(), before, "{line:?}");
        }
    }
}

#[test]
fn declined_is_exit_1() {
    let world = world();
    let writes = four_writes(&world);
    let plans = four_plans(&world);
    let before = world.files();
    for ((_, line), plan) in writes.iter().zip(&plans) {
        for answer in ["n\n", "\n", "", "N\n", "no\n", "yess\n", "y y\n"] {
            let done = world.ask(&strs(line), answer);
            let expected = format!("{plan}{PROMPT}{DECLINED}");
            assert_eq!(
                said(&done),
                (1, expected.as_str(), ""),
                "{line:?} {answer:?}"
            );
            assert_eq!(world.files(), before, "{line:?} {answer:?}");
        }
    }
    for answer in ["y\n", "Y\n", " yes \n", "YES\n", "y", "\tYes\r\n"] {
        for index in 0..4 {
            let world = common::world();
            let (went, line) = four_writes(&world).swap_remove(index);
            let plan = &four_plans(&world)[index];
            let before = world.files();
            let done = world.ask(&strs(&line), answer);
            assert_eq!(
                (done.code, done.stderr.as_str()),
                (0, ""),
                "{line:?} {answer:?}"
            );
            assert_eq!(
                done.stdout.matches(PROMPT).count(),
                1,
                "{line:?} {answer:?}"
            );
            let result = done.stdout.strip_prefix(&format!("{plan}{PROMPT}"));
            let result = result.unwrap_or_else(|| panic!("{line:?} {answer:?} {}", done.stdout));
            assert!(result.starts_with(went), "{line:?} {answer:?} {result}");
            assert_eq!(result.lines().count(), 1, "{result}");
            assert_ne!(world.files(), before, "{line:?} {answer:?}");
        }
    }
}

#[test]
fn with_yes_nothing_is_asked_and_each_command_goes_ahead() {
    for index in 0..4 {
        let world = world();
        let id = world.memory.id.clone();
        let (_, mut line) = four_writes(&world).swap_remove(index);
        let update = last_change(&world, &id);
        let plan = &four_plans(&world)[index];
        line.push("--yes".to_string());
        // a terminal with an answer that would decline: it is never read
        let done = world.ask(&strs(&line), "n\n");
        let result = match index {
            0 => format!(
                "restored {id} to the state of v1 as v3 (change {})\n",
                last_change(&world, &id).change_id
            ),
            1 => format!(
                "undone change {} by change {}\n",
                update.change_id,
                last_change(&world, &id).change_id
            ),
            2 => format!("deleted {id}\n"),
            _ => format!("purged {id}\n"),
        };
        assert_eq!(
            said(&done),
            (0, format!("{plan}{result}").as_str(), ""),
            "{line:?}"
        );
        match index {
            0 | 1 => assert_eq!(current(&world, &id).body, "line one\nline two"),
            2 => assert!(versions(&world, &id).last().unwrap().deleted),
            _ => assert!(gone(&world, &id)),
        }
    }
}

// --- the flags of delete ---

#[test]
fn delete_flag_matrix() {
    let code = "0123456789abcdef";
    let lines: [(&[&str], &str); 11] = [
        (
            &["--hard", "--version", "1"],
            "--version is not accepted with --hard",
        ),
        (
            &["--hard", "--version", "1", "--yes"],
            "--version is not accepted with --hard",
        ),
        (
            &["--hard", "--version", "1", "--dry-run"],
            "--version is not accepted with --hard",
        ),
        (
            &["--hard", "--version", "1", "--confirm", code],
            "--version is not accepted with --hard",
        ),
        (&["--dry-run"], "--dry-run and --confirm need --hard"),
        (&["--confirm", code], "--dry-run and --confirm need --hard"),
        (
            &["--version", "1", "--dry-run"],
            "--dry-run and --confirm need --hard",
        ),
        (
            &["--version", "1", "--confirm", code, "--yes"],
            "--dry-run and --confirm need --hard",
        ),
        (&[], "--version is required without --hard"),
        (&["--yes"], "--version is required without --hard"),
        // the grammar keeps these two apart, in its own words
        (&["--hard", "--dry-run", "--confirm", code], ""),
    ];
    let world = world();
    let id = world.memory.id.as_str();
    // a store no command reads: one that is read answers with exit 1 and another line
    world.sql().pragma_update(None, "user_version", 5).unwrap();
    let before = world.files();
    for (flags, message) in lines {
        let args = [&["delete", id][..], flags].concat();
        for tty in [false, true] {
            let done = run_in(&world.env, &world.home, &world.work, &args, "y\n", tty);
            assert_eq!((done.code, done.stdout.as_str()), (2, ""), "{args:?}");
            assert!(done.stderr.contains(message), "{args:?} {}", done.stderr);
            assert!(done.stderr.contains("error:"), "{args:?} {}", done.stderr);
            assert!(
                !done.stderr.contains("schema version"),
                "{args:?} {}",
                done.stderr
            );
            assert_eq!(world.files(), before, "{args:?}");
        }
        // and no store is made where there is none
        let (sandbox, tmp) = common::empty();
        let done = run_in(
            &sandbox.env(),
            &tmp.join("home"),
            &tmp.join("work"),
            &args,
            "",
            false,
        );
        assert_eq!((done.code, done.stdout.as_str()), (2, ""), "{args:?}");
        assert!(done.stderr.contains(message), "{args:?} {}", done.stderr);
        assert!(!sandbox.root.exists(), "{args:?}");
        assert_eq!(
            fs::read_dir(tmp.join("home")).unwrap().count(),
            0,
            "{args:?}"
        );
    }
}

#[test]
fn a_dry_run_and_a_confirmed_delete_ask_nothing_and_take_no_yes() {
    let world = world();
    let id = world.memory.id.as_str();
    // a terminal whose answer would decline
    let done = world.ask(&["delete", id, "--hard", "--dry-run"], "n\n");
    let code = plan_code(&world, id);
    let expected = format!(
        "memriver delete --hard: removes 1 memory with every version, source and read:\n  {id}  \
         {}  v1  the cue\n{UNDOABLE_NO_MORE}to delete exactly these, run: memriver delete {id} \
         --hard --confirm {code}\n",
        in_project(&world)
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    assert!(!gone(&world, id));
    let done = world.ask(&["delete", id, "--hard", "--confirm", &code], "n\n");
    assert_eq!(said(&done), (0, format!("purged {id}\n").as_str(), ""));
    assert!(gone(&world, id));
}

// --- the remaining sentences ---

#[test]
fn a_store_of_a_later_schema_is_refused_by_each_command() {
    let world = world();
    let id = world.memory.id.as_str();
    let created = last_change(&world, id);
    world.sql().pragma_update(None, "user_version", 7).unwrap();
    let before = world.files();
    let code = "0123456789abcdef";
    let lines: [(&[&str], &str); 8] = [
        (&["history", id], UNREADABLE),
        (&["history", id, "--show", "1"], UNREADABLE),
        (&["restore", id, "--to", "1", "--yes"], UNREADABLE),
        (&["undo", &created.change_id, "--yes"], UNREADABLE),
        (&["delete", id, "--version", "1", "--yes"], UNREADABLE),
        (&["delete", id, "--hard", "--dry-run"], STORE_FAILED),
        (&["delete", id, "--hard", "--yes"], STORE_FAILED),
        (&["delete", id, "--hard", "--confirm", code], STORE_FAILED),
    ];
    for (line, refusal) in lines {
        assert_eq!(said(&world.run(line)), (2, refusal, ""), "{line:?}");
        assert_eq!(world.files(), before, "{line:?}");
    }
}

// the plan can be read and shown, and the write fails in the store
#[test]
fn a_store_that_cannot_be_written_is_refused_after_the_plan() {
    let world = world();
    let writes = four_writes(&world);
    let plans = four_plans(&world);
    for (name, on) in [
        ("no_change", "INSERT ON changes"),
        ("no_purge", "DELETE ON memories"),
    ] {
        let sql = format!("CREATE TRIGGER {name} BEFORE {on} BEGIN SELECT RAISE(ABORT, 'no'); END");
        world.sql().execute(&sql, []).unwrap();
    }
    let before = world.files();
    let refusals = [UNWRITABLE, UNWRITABLE, UNWRITABLE, STORE_FAILED];
    for (((_, line), plan), refusal) in writes.iter().zip(&plans).zip(refusals) {
        let done = world.ask(&strs(line), "y\n");
        let expected = format!("{plan}{PROMPT}{refusal}");
        assert_eq!(said(&done), (2, expected.as_str(), ""), "{line:?}");
        assert_eq!(world.files(), before, "{line:?}");
    }
}

// a source row that cannot be decoded: what reads it cannot read the store, and what
// writes over it cannot write
#[test]
fn a_damaged_source_row_is_a_store_that_cannot_be_read_or_written() {
    let world = world();
    let id = world.memory.id.as_str();
    let source = record(&world, "source", "src");
    let cite = edit(id, 1, None, None, Some(&[(&source.id, 1)]), None);
    let cited = world.services.memory.apply(&[cite], "human", None).unwrap();
    unkeyed(&world)
        .execute(
            "UPDATE memory_sources SET source_id = CAST(X'80616161616161616161' AS TEXT) WHERE \
             memory_id = ?1",
            [id],
        )
        .unwrap();
    let project = in_project(&world);

    let done = world.ask(&["restore", id, "--to", "1"], "y\n");
    assert_eq!(said(&done), (2, UNREADABLE, ""));
    let done = world.ask(&["history", id, "--show", "1"], "");
    assert_eq!(said(&done), (2, UNREADABLE, ""));

    let done = world.run(&["undo", &cited.change_id, "--yes"]);
    let expected = format!(
        "memriver undo: change {} at {} by human\n  {id}  {project}  update v1->v2  the cue\nthe \
         undo:\n  {id}: restore the state of v1\n{UNWRITABLE}",
        cited.change_id, cited.at
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));

    let done = world.run(&["delete", id, "--version", "2", "--yes"]);
    let expected = format!("{}{UNWRITABLE}", soft_plan(id, &project, "the cue"));
    assert_eq!(said(&done), (2, expected.as_str(), ""));
}

#[test]
fn an_undo_whose_memory_cannot_be_shown_says_the_store_moved() {
    let world = world();
    let id = world.memory.id.as_str();
    let created = last_change(&world, id);
    unkeyed(&world)
        .execute(
            "UPDATE change_steps SET memory_id = 'zzzzzzzzzz' WHERE change_id = ?1",
            [&created.change_id],
        )
        .unwrap();
    let done = world.ask(&["undo", &created.change_id], "y\n");
    let moved = "refused: the memory store changed while it was read; run the command again\n";
    assert_eq!(said(&done), (2, moved, ""));
    assert_eq!(current(&world, id).version, 1);
}

#[test]
fn an_undo_of_a_change_that_is_gone_when_it_is_written_is_not_found() {
    let world = world();
    let id = world.memory.id.as_str();
    let update = dream_update(&world, id, 1, "line three");
    let change_id = update.change_id.as_str();
    let done = ask_meanwhile(&world, &world.work, &["undo", change_id], &mut || {
        let conn = world.sql();
        for sql in [
            "UPDATE memory_versions SET change_id = NULL WHERE change_id = ?1",
            "DELETE FROM change_steps WHERE change_id = ?1",
            "DELETE FROM changes WHERE change_id = ?1",
        ] {
            conn.execute(sql, [change_id]).unwrap();
        }
    });
    let expected = format!(
        "memriver undo: change {change_id} at {} by dream (codex)\n  {id}  {}  update v1->v2  \
         the cue\nthe undo:\n  {id}: restore the state of v1\n{PROMPT}refused (not-found): no \
         change {change_id}\n",
        update.at,
        in_project(&world)
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(current(&world, id).version, 2);
}

#[test]
fn an_undo_of_a_memory_changed_or_hard_deleted_meanwhile_is_refused() {
    let world = world();
    let id = world.memory.id.as_str();
    let project = in_project(&world);
    let first = dream_update(&world, id, 1, "line three");
    let done = ask_meanwhile(
        &world,
        &world.work,
        &["undo", &first.change_id],
        &mut || {
            dream_update(&world, id, 2, "line four");
        },
    );
    let expected = format!(
        "memriver undo: change {first} at {} by dream (codex)\n  {id}  {project}  update v1->v2  \
         the cue\nthe undo:\n  {id}: restore the state of v1\n{PROMPT}refused (changed): {id} \
         changed after change {first}; nothing was changed; use memriver history and memriver \
         restore\n",
        first.at,
        first = first.change_id
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(done.stdout.matches(PROMPT).count(), 1);

    let second = last_change(&world, id);
    let done = ask_meanwhile(
        &world,
        &world.work,
        &["undo", &second.change_id],
        &mut || {
            purge(&world, id);
        },
    );
    let expected = format!(
        "memriver undo: change {second} at {} by dream (codex)\n  {id}  {project}  update \
         v2->v3  the cue\nthe undo:\n  {id}: restore the state of v2\n{PROMPT}refused \
         (hard-deleted): a hard delete removed part of change {second}; it cannot be undone\n",
        second.at,
        second = second.change_id
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
}

#[test]
fn undo_counts_the_steps_a_hard_delete_removed() {
    let world = world();
    let project = world.project.id.as_str();
    let three: Vec<Op> = ["x", "y", "z"]
        .iter()
        .map(|name| create(project, "project", &format!("{name} cue"), name, &[], None))
        .collect();
    let change = world.services.memory.apply(&three, "dream", None).unwrap();
    let kept = &change.steps[0].memory_id;
    purge(&world, &change.steps[1].memory_id);
    purge(&world, &change.steps[2].memory_id);
    let refusal = format!(
        "refused (hard-deleted): a hard delete removed part of change {}; it cannot be undone\n",
        change.change_id
    );
    let done = world.run(&["undo", &change.change_id, "--yes"]);
    let expected = format!(
        "memriver undo: change {} at {} by dream\n  {kept}  project {project}  create v1  x \
         cue\n  (2 more steps removed by a hard delete)\nthe undo:\n  {kept}: soft \
         delete\n{refusal}",
        change.change_id, change.at
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));

    // every step gone: the change is still there, and still cannot be undone
    purge(&world, kept);
    let done = world.run(&["undo", &change.change_id, "--yes"]);
    let expected = format!(
        "memriver undo: change {} at {} by dream\n  (3 more steps removed by a hard \
         delete)\nthe undo:\n{refusal}",
        change.change_id, change.at
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
}

#[test]
fn restore_to_the_state_that_is_the_current_one_is_refused() {
    let world = world();
    let id = world.memory.id.as_str();
    let refusal = "refused (same-state): the state of v1 is already the current one; nothing \
                   was changed\n";
    // the current version itself: with and without a terminal
    let plan = restore_plan(id, 1, 1, "the cue", "the cue", "nothing");
    let done = world.run(&["restore", id, "--to", "1", "--yes"]);
    assert_eq!(said(&done), (2, format!("{plan}{refusal}").as_str(), ""));
    let done = world.ask(&["restore", id, "--to", "1"], "y\n");
    assert_eq!(
        said(&done),
        (2, format!("{plan}{PROMPT}{refusal}").as_str(), "")
    );
    // an older version whose state came back
    dream_update(&world, id, 1, "line three");
    dream_update(&world, id, 2, "line one\nline two");
    let plan = restore_plan(id, 3, 1, "the cue", "the cue", "nothing");
    let done = world.run(&["restore", id, "--to", "1", "--yes"]);
    assert_eq!(said(&done), (2, format!("{plan}{refusal}").as_str(), ""));
    assert_eq!(versions(&world, id).len(), 3);
}

#[test]
fn the_restore_plan_names_everything_that_differs() {
    let world = world();
    let id = world.memory.id.as_str();
    let source = record(&world, "source", "src");
    let cite = edit(
        id,
        1,
        Some("new cue"),
        None,
        Some(&[(&source.id, 1)]),
        Some(3),
    );
    world.services.memory.apply(&[cite], "dream", None).unwrap();
    soft_delete(&world, id, 2);
    let done = world.run(&["restore", id, "--to", "1"]);
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        restore_plan(
            id,
            3,
            1,
            "new cue  [deleted]",
            "the cue",
            "content, sources, level and tags, deleted state"
        )
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    // and back: the target is the deleted one
    let done = world.run(&["restore", id, "--to", "1", "--yes"]);
    assert_eq!(done.code, 0);
    let done = world.run(&["restore", id, "--to", "3"]);
    let expected = format!(
        "{}{NOT_A_TERMINAL}",
        restore_plan(
            id,
            4,
            3,
            "the cue",
            "new cue  [deleted]",
            "content, sources, level and tags, deleted state"
        )
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    let done = world.run(&["restore", id, "--to", "2"]);
    let plan = restore_plan(
        id,
        4,
        2,
        "the cue",
        "new cue",
        "content, sources, level and tags",
    );
    assert_eq!(
        said(&done),
        (2, format!("{plan}{NOT_A_TERMINAL}").as_str(), "")
    );
}

#[test]
fn a_restore_of_a_memory_hard_deleted_meanwhile_is_refused_as_missing() {
    let world = world();
    let id = world.memory.id.as_str();
    dream_update(&world, id, 1, "line three");
    let args = ["restore", id, "--to", "1"];
    let done = ask_meanwhile(&world, &world.work, &args, &mut || purge(&world, id));
    let expected = format!(
        "{}{PROMPT}refused (missing): nothing was changed\n",
        restore_plan(id, 2, 1, "the cue", "the cue", "content")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
}

#[test]
fn restore_and_soft_delete_of_an_unknown_memory_are_refused() {
    let world = world();
    let unknown = new_id();
    let refusal = format!("no such memory: {unknown}\n");
    let code = "0123456789abcdef";
    let lines: [&[&str]; 5] = [
        &["restore", &unknown, "--to", "1"],
        &["delete", &unknown, "--version", "1"],
        &["delete", &unknown, "--hard"],
        &["delete", &unknown, "--hard", "--yes"],
        &["delete", &unknown, "--hard", "--confirm", code],
    ];
    for line in lines {
        assert_eq!(
            said(&world.ask(line, "y\n")),
            (2, refusal.as_str(), ""),
            "{line:?}"
        );
    }
    // a soft-deleted memory is none to delete again, wherever it is
    let id = world.memory.id.as_str();
    soft_delete(&world, id, 1);
    let done = world.ask(&["delete", id, "--version", "2"], "y\n");
    assert_eq!(
        said(&done),
        (2, format!("no such memory: {id}\n").as_str(), "")
    );
    let shared = global_memory(&world, &[]);
    soft_delete(&world, &shared, 1);
    let done = ask_from(
        &world,
        &world.tmp,
        &["delete", &shared, "--version", "2"],
        "y\n",
    );
    assert_eq!(
        said(&done),
        (2, format!("no such memory: {shared}\n").as_str(), "")
    );
}

#[test]
fn an_id_that_is_echoed_is_cut_and_shown_without_its_control_characters() {
    let world = world();
    let typed = format!("x\u{1b}y{}", "a".repeat(300));
    let echoed = format!("x y{}", "a".repeat(252));
    let memory = format!("no such memory: {echoed}\n");
    let change = format!("refused (not-found): no change {echoed}\n");
    let code = "0123456789abcdef";
    let lines: [(&[&str], &str); 6] = [
        (&["history", &typed], &memory),
        (&["restore", &typed, "--to", "1"], &memory),
        (&["undo", &typed], &change),
        (&["delete", &typed, "--version", "1"], &memory),
        (&["delete", &typed, "--hard"], &memory),
        (&["delete", &typed, "--hard", "--confirm", code], &memory),
    ];
    for (line, refusal) in lines {
        assert_eq!(
            said(&world.ask(line, "y\n")),
            (2, refusal, ""),
            "{:?}",
            &line[..1]
        );
    }
}

#[test]
fn a_number_is_shown_as_a_number_however_it_was_written() {
    let world = world();
    let id = world.memory.id.as_str();
    let plain = world.run(&["history", id, "--show", "1"]);
    assert_eq!(plain.code, 0);
    assert_eq!(world.run(&["history", id, "--show", " +0_1 "]), plain);
    let done = world.run(&["history", id, "--show", "007"]);
    assert_eq!(
        said(&done),
        (2, format!("no such version: {id} v7\n").as_str(), "")
    );
    let big = "99999999999999999999999";
    let done = world.run(&["history", id, "--show", big]);
    assert_eq!(
        said(&done),
        (2, format!("no such version: {id} v{big}\n").as_str(), "")
    );
    let done = world.run(&["restore", id, "--to", "0_0_9", "--yes"]);
    assert_eq!(
        said(&done),
        (2, format!("no such version: {id} v9\n").as_str(), "")
    );
    let done = world.run(&["restore", id, "--to", big, "--yes"]);
    assert_eq!(
        said(&done),
        (2, format!("no such version: {id} v{big}\n").as_str(), "")
    );
    for (written, shown) in [("+05", "5"), (big, big)] {
        let done = world.run(&["delete", id, "--version", written, "--yes"]);
        let expected = format!(
            "{}refused: {id} changed since version {shown}; run memriver show {id} and retry\n",
            soft_plan(id, &in_project(&world), "the cue")
        );
        assert_eq!(said(&done), (2, expected.as_str(), ""));
    }
    assert_eq!(current(&world, id).version, 1);
}

#[test]
fn a_soft_delete_of_a_memory_changed_or_hard_deleted_meanwhile_is_refused() {
    let world = world();
    let project = in_project(&world);
    // a project's memory, hard-deleted while the question is open
    let second = record(&world, "b", "second").id;
    let args = ["delete", &second, "--version", "1"];
    let done = ask_meanwhile(&world, &world.work, &args, &mut || purge(&world, &second));
    let expected = format!(
        "{}{PROMPT}no such memory: {second}\n",
        soft_plan(&second, &project, "second")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));

    // a project's memory, updated meanwhile
    let third = record(&world, "b", "third").id;
    let args = ["delete", &third, "--version", "1"];
    let done = ask_meanwhile(&world, &world.work, &args, &mut || {
        dream_update(&world, &third, 1, "c");
    });
    let expected = format!(
        "{}{PROMPT}refused: {third} changed since version 1; run memriver show {third} and \
         retry\n",
        soft_plan(&third, &project, "third")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(done.stdout.matches(PROMPT).count(), 1);
    assert_eq!(current(&world, &third).version, 2);

    // a global memory, updated and then soft-deleted meanwhile
    let shared = global_memory(&world, &[]);
    let args = ["delete", &shared, "--version", "1"];
    let done = ask_meanwhile(&world, &world.tmp, &args, &mut || {
        dream_update(&world, &shared, 1, "c");
    });
    let plan = soft_plan(&shared, "global", "a shared rule");
    let expected = format!(
        "{plan}{PROMPT}refused: {shared} changed since version 1; run memriver show {shared} \
         and retry\n"
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    let args = ["delete", &shared, "--version", "2"];
    let done = ask_meanwhile(&world, &world.tmp, &args, &mut || {
        soft_delete(&world, &shared, 2)
    });
    let expected = format!(
        "{plan}{PROMPT}refused: {shared} changed since version 2; run memriver show {shared} \
         and retry\n"
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(versions(&world, &shared).len(), 3);
}

// the memory's project is another one when the delete is written
#[test]
fn a_soft_delete_of_a_memory_whose_project_changed_meanwhile_is_refused() {
    let world = world();
    let id = world.memory.id.as_str();
    let args = ["delete", id, "--version", "1"];
    let done = ask_meanwhile(&world, &world.work, &args, &mut || {
        world
            .sql()
            .execute(
                "UPDATE memories SET project_id = ?1 WHERE id = ?2",
                (&world.global_id, id),
            )
            .unwrap();
    });
    let expected = format!(
        "{}{PROMPT}refused: the memory's project changed while waiting; run the command again\n",
        soft_plan(id, &in_project(&world), "the cue")
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(versions(&world, id).len(), 1);
}

// what was stored is shown with every control character as a space, the body with its
// newlines and tabs
#[test]
fn stored_text_is_shown_without_its_control_characters() {
    let world = world();
    let id = world.memory.id.as_str();
    let project = in_project(&world);
    let rewrite = edit(
        id,
        1,
        Some("a\u{1b}[31m cue\u{202e} x"),
        Some("b\u{7}ody\tkept\nnext"),
        None,
        None,
    );
    let ops = [rewrite];
    let change = world
        .services
        .memory
        .apply(&ops, "dr\u{1b}eam", Some("co\u{7}dex"))
        .unwrap();
    let untitled = create(
        &world.global_id,
        "project",
        "",
        "first \u{1b} line\nsecond",
        &[(id, 2)],
        None,
    );
    let made = world
        .services
        .memory
        .apply(&[untitled], "dr\u{1b}eam", Some("co\u{7}dex"));
    let made = made.unwrap();
    let shared = made.steps[0].memory_id.as_str();
    let first = versions(&world, id)[0].change.clone().unwrap();
    let cue = "a [31m cue  x";
    let by = "dr eam (co dex)";

    let done = world.run(&["history", id]);
    let expected = format!(
        "v1  {}  mcp (t)  change {}  the cue\nv2  {}  {by}  change {}  {cue}\n",
        first.at, first.change_id, change.at, change.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));

    let done = world.run(&["history", id, "--show", "2"]);
    let expected = format!(
        "id: {id}\nversion: 2\ntype: project\ntrust: agent\nsync: true\ndeleted: false\nchange: \
         {}  {by}  change {}\nsources: -\ndescription: {cue}\n---\nb ody\tkept\nnext\n",
        change.at, change.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));

    let done = world.run(&["history", shared]);
    let expected = format!(
        "v1  {}  {by}  change {}  \n    sources: {id} v2\n",
        made.at, made.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));

    let done = world.run(&["history", shared, "--show", "1"]);
    let expected = format!(
        "id: {shared}\nversion: 1\ntype: project\ntrust: agent\nsync: true\ndeleted: \
         false\nchange: {}  {by}  change {}\nsources: {id} v2\ndescription: \n---\nfirst   \
         line\nsecond\n",
        made.at, made.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));

    let done = world.run(&["restore", id, "--to", "1"]);
    let plan = restore_plan(id, 2, 1, cue, "the cue", "content");
    assert_eq!(
        said(&done),
        (2, format!("{plan}{NOT_A_TERMINAL}").as_str(), "")
    );

    let done = world.run(&["undo", &change.change_id]);
    let expected = format!(
        "memriver undo: change {} at {} by {by}\n  {id}  {project}  update v1->v2  {cue}\nthe \
         undo:\n  {id}: restore the state of v1\n{NOT_A_TERMINAL}",
        change.change_id, change.at
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));

    let done = world.run(&["delete", id, "--version", "2"]);
    let plan = soft_plan(id, &project, cue);
    assert_eq!(
        said(&done),
        (2, format!("{plan}{NOT_A_TERMINAL}").as_str(), "")
    );

    let done = world.run(&["delete", id, "--hard"]);
    let expected = format!(
        "memriver delete --hard: removes 2 memories with every version, source and read:\n  {id}  \
         {project}  v2  {cue}\n  {shared}  global  v1  first line\n    because {shared} v1 cites \
         {id} v2\n{UNDOABLE_NO_MORE}{NOT_A_TERMINAL}"
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
}

// a memory that is in the plan because a version it once had cites the target, and that
// is itself soft-deleted
#[test]
fn the_hard_delete_plan_marks_a_deleted_member_and_a_citation_of_its_history() {
    let world = world();
    let id = world.memory.id.as_str();
    let shared = global_memory(&world, &[(id, 1)]);
    let uncite = edit(&shared, 1, None, Some("no longer cites"), Some(&[]), None);
    world
        .services
        .memory
        .apply(&[uncite], "dream", None)
        .unwrap();
    soft_delete(&world, &shared, 2);
    let plan = format!(
        "memriver delete --hard: removes 2 memories with every version, source and read:\n  {id}  \
         {}  v1  the cue\n  {shared}  global  v3  (deleted)  a shared rule\n    because {shared} \
         v1 (history) cites {id} v1\n{UNDOABLE_NO_MORE}",
        in_project(&world)
    );
    let done = world.ask(&["delete", id, "--hard", "--dry-run"], "");
    let expected = format!(
        "{plan}to delete exactly these, run: memriver delete {id} --hard --confirm {}\n",
        plan_code(&world, id)
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));

    // the members are named as the plan lists them: the target first
    let done = world.ask(&["delete", id, "--hard"], "y\n");
    let expected = format!("{plan}{PROMPT}purged {id}, {shared}\n");
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    assert!(gone(&world, id) && gone(&world, &shared));
}

#[test]
fn a_hard_delete_of_a_citing_memory_leaves_what_it_cites() {
    let world = world();
    let id = world.memory.id.as_str();
    let shared = global_memory(&world, &[(id, 1)]);
    let done = world.run(&["delete", &shared, "--hard", "--yes"]);
    let expected = format!(
        "memriver delete --hard: removes 1 memory with every version, source and read:\n  \
         {shared}  global  v1  a shared rule\n{UNDOABLE_NO_MORE}purged {shared}\n"
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
    assert!(gone(&world, &shared) && !gone(&world, id));
}

// the command to run names the store only when this command was given one, as it was
// given, and as a shell takes it as one word
#[test]
fn the_confirm_command_names_the_root_it_was_given() {
    let world = world();
    let id = world.memory.id.as_str();
    let code = plan_code(&world, id);
    let command = |done: &Done| done.stdout.lines().last().unwrap().to_string();
    let start = format!("to delete exactly these, run: memriver delete {id} --hard --confirm");

    let done = world.run(&["delete", id, "--hard", "--dry-run"]);
    assert_eq!(command(&done), format!("{start} {code}"));

    // a stale code prints the same command
    let done = world.run(&["delete", id, "--hard", "--confirm", "0123456789abcdef"]);
    assert_eq!(done.code, 2);
    assert_eq!(command(&done), format!("{start} {code}"));

    let moved = world.tmp.join("it's a store");
    fs::rename(&world.store, &moved).unwrap();
    let root = moved.to_str().unwrap();
    let done = world.run(&["delete", id, "--hard", "--dry-run", "--root", root]);
    assert_eq!(done.code, 0, "{}", done.stdout);
    let quoted = format!("'{}'", root.replace('\'', "'\"'\"'"));
    assert_eq!(command(&done), format!("{start} {code} --root {quoted}"));
    let done = world.run(&[
        "delete",
        id,
        "--hard",
        "--confirm",
        "0123456789abcdef",
        "--root",
        root,
    ]);
    assert_eq!(done.code, 2);
    assert_eq!(command(&done), format!("{start} {code} --root {quoted}"));
}

// The root is named as the one place it is: absolute, and normalized by its text. The
// Python implementation printed it as it was typed.
#[test]
fn the_confirm_command_names_a_relative_root_by_its_absolute_path() {
    let world = world();
    let id = world.memory.id.as_str();
    let code = plan_code(&world, id);
    let command = |root: &str| {
        let done = world.run(&["delete", id, "--hard", "--dry-run", "--root", root]);
        assert_eq!(done.code, 0, "{}", done.stdout);
        done.stdout.lines().last().unwrap().to_string()
    };
    let start = format!("to delete exactly these, run: memriver delete {id} --hard --confirm");
    let longer = format!("{}/../mem//", world.work.to_str().unwrap());
    let named = format!(
        "{start} {code} --root {}",
        shell_word(world.store.to_str().unwrap())
    );
    assert_eq!(
        (command("../mem"), command(&longer)),
        (named.clone(), named)
    );
}

// several sources stand by id, and a description of several lines is one line
#[test]
fn history_lists_the_sources_of_a_version_by_id() {
    let world = world();
    let first = world.memory.id.clone();
    let second = record(&world, "another fact", "second cue").id;
    let third = record(&world, "third fact", "third cue").id;
    let citing = create(
        &world.global_id,
        "project",
        "d\ne  f",
        "shared body",
        &[(&third, 1), (&first, 1), (&second, 1)],
        None,
    );
    let made = world
        .services
        .memory
        .apply(&[citing], "dream", None)
        .unwrap();
    let shared = made.steps[0].memory_id.as_str();
    let cited: Vec<String> = BTreeSet::from([&first, &second, &third])
        .into_iter()
        .map(|id| format!("{id} v1"))
        .collect();
    let cited = cited.join(", ");

    let done = world.run(&["history", shared]);
    let expected = format!(
        "v1  {}  dream  change {}  d e f\n    sources: {cited}\n",
        made.at, made.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));

    let done = world.run(&["history", shared, "--show", "1"]);
    let expected = format!(
        "id: {shared}\nversion: 1\ntype: project\ntrust: agent\nsync: true\ndeleted: \
         false\nchange: {}  dream  change {}\nsources: {cited}\ndescription: d e f\n---\nshared \
         body\n",
        made.at, made.change_id
    );
    assert_eq!(said(&done), (0, expected.as_str(), ""));
}

// the steps stand as the change made them, the memories that changed since by id
#[test]
fn an_undo_names_every_memory_that_changed_since() {
    let world = world();
    let project = in_project(&world);
    let first = world.memory.id.clone();
    let second = record(&world, "another fact", "second cue").id;
    let third = record(&world, "third fact", "third cue").id;
    let ops = [
        edit(&third, 1, None, Some("c2"), None, None),
        edit(&first, 1, None, Some("a2"), None, None),
        edit(&second, 1, None, Some("b2"), None, None),
    ];
    let change = world.services.memory.apply(&ops, "dream", None).unwrap();
    dream_update(&world, &second, 2, "b3");
    dream_update(&world, &third, 2, "c3");
    let changed: Vec<&str> = BTreeSet::from([second.as_str(), third.as_str()])
        .into_iter()
        .collect();

    let done = world.run(&["undo", &change.change_id, "--yes"]);
    let expected = format!(
        "memriver undo: change {id} at {} by dream\n  {third}  {project}  update v1->v2  third \
         cue\n  {first}  {project}  update v1->v2  the cue\n  {second}  {project}  update \
         v1->v2  second cue\nthe undo:\n  {third}: restore the state of v1\n  {first}: restore \
         the state of v1\n  {second}: restore the state of v1\nrefused (changed): {} changed \
         after change {id}; nothing was changed; use memriver history and memriver restore\n",
        change.at,
        changed.join(", "),
        id = change.change_id
    );
    assert_eq!(said(&done), (2, expected.as_str(), ""));
    assert_eq!(current(&world, &first).version, 2);
}

// --- a value is what was typed, and nothing else is a value ---

/// Every memory, version and change of the store, as text.
fn rows(world: &World) -> Vec<String> {
    let conn = world.sql();
    let mut rows = Vec::new();
    for query in [
        "SELECT 'memory ' || id || ' v' || version || ' ' || coalesce(deleted_at, 'live') \
         || ' ' || body FROM memories ORDER BY id",
        "SELECT 'version ' || memory_id || ' v' || version FROM memory_versions ORDER BY 1",
        "SELECT 'change ' || change_id FROM changes ORDER BY 1",
    ] {
        let mut statement = conn.prepare(query).unwrap();
        let read = statement.query_map([], |row| row.get::<_, String>(0));
        rows.extend(read.unwrap().map(Result::unwrap));
    }
    rows
}

fn usage_error(done: &Done) {
    assert_eq!((done.code, done.stdout.as_str()), (2, ""), "{done:?}");
    assert!(done.stderr.starts_with("error: "), "{done:?}");
}

// a code or a number with a character before it is another code and no number, however
// the option and its value are joined
#[test]
fn a_character_before_a_code_or_a_version_is_part_of_what_was_typed() {
    let world = world();
    let id = world.memory.id.as_str();
    dream_update(&world, id, 1, "second body");
    let code = plan_code(&world, id);
    let before = rows(&world);
    assert_eq!(before.len(), 1 + 2 + 2, "{before:?}");

    let marked = format!("\u{e000}{code}");
    for confirm in [
        vec!["--confirm".to_string(), marked.clone()],
        vec![format!("--confirm={marked}")],
    ] {
        let mut args = vec!["delete", id, "--hard"];
        args.extend(confirm.iter().map(String::as_str));
        let done = world.run(&args);
        assert_eq!((done.code, done.stderr.as_str()), (2, ""), "{args:?}");
        let refused = "refused: the plan changed since it was printed; nothing was deleted\n";
        assert!(
            done.stdout.starts_with(refused),
            "{args:?}: {}",
            done.stdout
        );
        assert_eq!(rows(&world), before, "{args:?}");
    }
    for version in [
        "\u{e000}1",
        "\u{1c}1",
        "\u{1d}1",
        "\u{1e}1",
        "\u{1f}1",
        "1\u{1f}",
    ] {
        let joined = format!("--to={version}");
        for to in [&["--to", version][..], &[joined.as_str()]] {
            let args = [&["restore", id][..], to, &["--yes"]].concat();
            let done = world.run(&args);
            usage_error(&done);
            assert!(done.stderr.contains(version), "{args:?}: {}", done.stderr);
            assert_eq!(rows(&world), before, "{args:?}");
        }
        let done = world.run(&["delete", id, "--version", version, "--yes"]);
        usage_error(&done);
        assert_eq!(rows(&world), before, "{version:?}");
    }
    assert_eq!(current(&world, id).version, 2);
}

// a store whose name begins with a private-use character is that store, not the one
// named without it
#[test]
fn a_store_named_with_a_private_use_character_is_the_one_that_is_written() {
    let world = world();
    let id = world.memory.id.as_str();
    let (plain, marked) = (world.tmp.join("copy"), world.tmp.join("\u{e000}copy"));
    for copy in [&plain, &marked] {
        fs::create_dir(copy).unwrap();
        for (name, bytes) in world.files() {
            fs::write(copy.join(name), bytes).unwrap();
        }
    }
    let code = plan_code(&world, id);
    let confirm = format!("--confirm={code}");
    for root in [&["--root=\u{e000}copy"][..], &["--root", "\u{e000}copy"]] {
        let args = [&["delete", id, "--hard", "--dry-run"][..], root].concat();
        let done = world.run_from(&world.tmp, &args);
        assert_eq!(done.code, 0, "{done:?}");
        assert!(done.stdout.ends_with("/\u{e000}copy'\n"), "{}", done.stdout);
    }
    let args = ["delete", id, "--hard", &confirm, "--root=\u{e000}copy"];
    let done = world.run_from(&world.tmp, &args);
    assert_eq!(said(&done), (0, format!("purged {id}\n").as_str(), ""));
    let holds = |store: &Path| {
        let services = common::services(store, &world.home, &world.env);
        services.memory.show(id, true).is_ok()
    };
    assert!(holds(&plain) && !holds(&marked));
    assert!(!gone(&world, id));
}

// what a person typed is printed as it was typed
#[test]
fn a_private_use_character_of_the_callers_own_is_kept() {
    let world = world();
    let done = world.run(&["show", "\u{e000}x"]);
    assert_eq!(said(&done), (2, "no such memory: \u{e000}x\n", ""));
    for args in [
        &["search", "x", "--limit", "\u{e000}7"][..],
        &["list", "\u{e000}extra"],
        &["history", "x", "--show=\u{e000}\u{e000}2"],
    ] {
        let done = world.run(args);
        usage_error(&done);
        let typed = args.last().unwrap().trim_start_matches("--show=");
        assert!(done.stderr.contains(typed), "{args:?}: {}", done.stderr);
    }
}

// The Python implementation's parser read `-h sample` as its option -h, and refused it
// as the value of another option. It is never the name of a store or of a memory.
#[test]
fn a_spaced_argument_that_begins_with_a_hyphen_is_no_value() {
    let world = world();
    let id = world.memory.id.as_str();
    let before = rows(&world);
    for root in [&["--root", "-h sample"][..], &["--root", "- x"]] {
        let args = [&["delete", id, "--version", "1", "--yes"][..], root].concat();
        let done = world.run_from(&world.tmp, &args);
        usage_error(&done);
        assert_eq!(rows(&world), before, "{args:?}");
    }
    for args in [
        &["show", "- x"][..],
        &["delete", "- x", "--version", "1", "--yes"],
        &["search", "--not an option"],
    ] {
        usage_error(&world.run(args));
    }
    // as the one argument of a command it asks for the command's help, and nothing is read
    let done = world.run(&["show", "-h sample"]);
    assert!(!done.stdout.contains("no such memory"), "{done:?}");
    // after `--` it is a value like any other
    let done = world.run(&["show", "--", "-h sample"]);
    assert_eq!(said(&done), (2, "no such memory: -h sample\n", ""));
    let done = world.run(&["delete", "--version", "1", "--yes", "--", "-h sample"]);
    assert_eq!(said(&done), (2, "no such memory: -h sample\n", ""));
    assert_eq!(rows(&world), before);
    assert!(!world.tmp.join("-h sample").exists() && !world.tmp.join("- x").exists());
    assert!(!world.work.join("-h sample").exists());
}
