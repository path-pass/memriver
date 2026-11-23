use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::PathBuf;

use memriver_core::models::memory::{MemoryType, Trust};
use memriver_core::models::session::{PromptOmission, SessionKey, SessionOrigin};
use memriver_test_support::Sandbox;

use super::*;

const T0: &str = "2026-01-01T00:00:00.000000Z";
const T1: &str = "2026-01-01T00:05:00.000000Z";
const T2: &str = "2026-01-01T02:05:00.000000Z";
const DEMO: &str = "aaaaaaaaaa";
const OTHER: &str = "bbbbbbbbbb";
const NOT_LISTED: &str = "cccccccccc";

/// A temporary directory with an empty directory `inner` in it.
fn inner() -> (Sandbox, PathBuf, PathBuf) {
    let sandbox = Sandbox::new();
    let base = fs::canonicalize(sandbox.dir.path()).unwrap();
    let inner = base.join("inner");
    fs::create_dir(&inner).unwrap();
    (sandbox, base, inner)
}

/// The directory at `path`, opened inside the one above it, refusing a link in its place.
fn held(path: &Path) -> io::Result<OwnedFd> {
    let (above, name) = above_of(path)?;
    open_in(&above, &name)
}

/// The snapshot written into the directory at `target`, which is there already.
fn snapshot_at(
    target: &Path,
    listed: &[(Project, Vec<Memory>)],
    global_id: Option<&str>,
    count: &mut usize,
) -> io::Result<()> {
    let (above, name) = above_of(target)?;
    snapshot(&above, &name, listed, global_id, count)
}

fn names_in(directory: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(directory)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    names
}

fn mode(path: &Path) -> u32 {
    fs::symlink_metadata(path).unwrap().permissions().mode() & 0o7777
}

fn names() -> HashMap<String, String> {
    HashMap::from([
        (DEMO.to_string(), "demo".to_string()),
        (OTHER.to_string(), "other".to_string()),
    ])
}

/// A codex session registered to `demo`, with nothing else recorded.
fn session() -> Session {
    Session {
        key: SessionKey::new(Harness::Codex, "sess-1").unwrap(),
        status: SessionStatus::Registered,
        origin: SessionOrigin::Start,
        project_id: Some(DEMO.to_string()),
        candidate_id: None,
        candidate_root: None,
        entry_cwd: "/w".to_string(),
        branch: None,
        transcript_path: None,
        started_at: T0.to_string(),
        last_active_at: T1.to_string(),
        ended_at: None,
        prompt_count: 0,
        last_write_prompt_count: 0,
        last_nudge_prompt_count: 0,
        first_prompt: None,
        recent_prompts: Vec::new(),
        summary: None,
        summary_at: None,
    }
}

fn pending(candidate: Option<&str>) -> Session {
    Session {
        status: SessionStatus::Pending,
        origin: SessionOrigin::FirstSeen,
        project_id: None,
        candidate_id: candidate.map(str::to_string),
        ..session()
    }
}

/// A session with every field set, several of them with invisible characters.
fn busy_session() -> Session {
    let first = PromptEntry::text(T0, "first\u{200b}one").unwrap();
    Session {
        key: SessionKey::new(Harness::ClaudeCode, "it's").unwrap(),
        branch: Some("ma\u{1b}in".to_string()),
        ended_at: Some(T1.to_string()),
        first_prompt: Some(first.clone()),
        recent_prompts: vec![
            first,
            PromptEntry::omitted(T1, PromptOmission::Secret).unwrap(),
        ],
        summary: Some("Fixed  it.\nThen\tdocs.\u{202e}".to_string()),
        summary_at: Some(T1.to_string()),
        ..session()
    }
}

fn block_with(project_line: &str) -> String {
    format!(
        "codex sess-1\n  last active: {T1} (just now)\n  project: {project_line}\n  branch: -\n  \
         first recorded: {T0}\n  last end event: -\n  first prompt: -\n  latest prompt: -\n  \
         summary: -\n  resume: codex resume sess-1"
    )
}

fn memory(id: &str, project_id: &str, body: &str) -> Memory {
    let mut memory = Memory::new(
        body,
        MemoryType::Project,
        project_id,
        "t",
        "mcp",
        Trust::Agent,
        true,
        "the cue",
    )
    .unwrap();
    memory.id = id.to_string();
    memory
}

fn project(id: &str, root: Option<&str>) -> Project {
    Project {
        id: id.to_string(),
        name: "demo".to_string(),
        root: root.map(str::to_string),
    }
}

// --- what is shown ---

#[test]
fn visible_replaces_controls_one_for_one_and_keeps_ordinary_spaces() {
    // NUL, ESC, LF, NEL, LS, PS: one space each
    let hostile = "two  spaces\0\u{1b}[31m\n\u{85}\u{2028}\u{2029}end";
    let rendered = visible(hostile);
    assert_eq!(rendered, "two  spaces  [31m    end");
    assert_eq!(rendered.chars().count(), hostile.chars().count());
    assert_eq!(
        visible("  lead and  inner  and trail  "),
        "  lead and  inner  and trail  "
    );
}

#[test]
fn visible_replaces_format_characters_and_keeps_every_other_one() {
    // a zero-width space, a right-to-left override, a soft hyphen, a byte order mark,
    // DEL and a C1 control go; a no-break space, an ideographic space, a private-use
    // and an unassigned character stay
    assert_eq!(
        visible("a\u{200b}\u{202e}\u{ad}b\u{feff}\u{a0}\u{3000}c\u{7f}\u{9f}\u{e000}\u{378}"),
        "a   b \u{a0}\u{3000}c  \u{e000}\u{378}"
    );
    assert_eq!(visible(""), "");
    assert_eq!(visible("a\tb\rc"), "a b c");
}

#[test]
fn invisible_is_exactly_what_the_python_implementation_replaces() {
    // the categories Cc, Cf, Zl and Zp of Unicode 16.0.0, as Python 3.14 has them
    const RANGES: [(u32, u32); 23] = [
        (0x0, 0x1f),
        (0x7f, 0x9f),
        (0xad, 0xad),
        (0x600, 0x605),
        (0x61c, 0x61c),
        (0x6dd, 0x6dd),
        (0x70f, 0x70f),
        (0x890, 0x891),
        (0x8e2, 0x8e2),
        (0x180e, 0x180e),
        (0x200b, 0x200f),
        (0x2028, 0x202e),
        (0x2060, 0x2064),
        (0x2066, 0x206f),
        (0xfeff, 0xfeff),
        (0xfff9, 0xfffb),
        (0x110bd, 0x110bd),
        (0x110cd, 0x110cd),
        (0x13430, 0x1343f),
        (0x1bca0, 0x1bca3),
        (0x1d173, 0x1d17a),
        (0xe0001, 0xe0001),
        (0xe0020, 0xe007f),
    ];
    let listed = |c: char| {
        RANGES
            .iter()
            .any(|(first, last)| (*first..=*last).contains(&u32::from(c)))
    };
    let differing: Vec<char> = (char::MIN..=char::MAX)
        .filter(|c| invisible(*c) != listed(*c))
        .collect();
    assert_eq!(differing, Vec::<char>::new());
    assert_eq!(
        (char::MIN..=char::MAX).filter(|c| invisible(*c)).count(),
        237
    );
}

#[test]
fn body_keeps_newlines_and_tabs_and_nothing_else_invisible() {
    assert_eq!(
        body("a\tb\nc\rd\u{1b}e\u{2028}f\u{200b}g"),
        "a\tb\nc d e f g"
    );
}

#[test]
fn cue_is_the_description_or_the_first_line_of_the_body() {
    let x61 = "x".repeat(61);
    let e61 = "é".repeat(61);
    let table: [(&str, &str, &str); 20] = [
        ("desc", "body", "desc"),
        ("", "first\nsecond", "first"),
        // every break Python's str.splitlines knows
        ("", "a\rb", "a"),
        ("", "a\r\nb", "a"),
        ("", "a\u{b}b", "a"),
        ("", "a\u{c}b", "a"),
        ("", "a\u{1c}b", "a"),
        ("", "a\u{1d}b", "a"),
        ("", "a\u{1e}b", "a"),
        ("", "a\u{85}b", "a"),
        ("", "a\u{2028}b", "a"),
        ("", "a\u{2029}b", "a"),
        // and one it does not know
        ("", "a\u{1f}b", "a b"),
        ("", "", ""),
        ("", "\nsecond", ""),
        ("", "  a \t b\u{200b}c  ", "a b c"),
        ("d\ne  f", "body", "d e f"),
        ("", &x61, &x61[..60]),
        ("", &e61, &e61[..120]),
        (&e61, "body", &e61[..120]),
    ];
    for (description, text, expected) in table {
        assert_eq!(cue(description, text), expected, "{description:?} {text:?}");
    }
    assert_eq!(cue("", &e61).chars().count(), 60);
}

#[test]
fn memory_line_cuts_the_time_to_ten_characters_after_neutralising_it() {
    let mut memory = memory("mmmmmmmmmm", DEMO, "body");
    memory.updated = "\u{1b}[2J2026-09-2".to_string();
    memory.description = "cue\u{1b}[31m".to_string();
    assert_eq!(
        memory_line(&memory),
        "  mmmmmmmmmm  [project]   [2J2026-0  cue [31m\n"
    );
}

#[test]
fn echoed_is_the_first_255_characters_made_visible() {
    let long = format!("{}\n{}", "é".repeat(254), "x".repeat(10));
    assert_eq!(echoed(&long), format!("{} ", "é".repeat(254)));
    assert_eq!(echoed(&"é".repeat(256)), "é".repeat(255));
    assert_eq!(echoed("short"), "short");
}

#[test]
fn place_is_global_the_directory_or_none() {
    let bound = project(DEMO, Some("/w\n/x"));
    assert_eq!(place(&bound, Some(OTHER)), "/w /x");
    assert_eq!(place(&bound, None), "/w /x");
    assert_eq!(place(&bound, Some(DEMO)), "global");
    assert_eq!(place(&project(DEMO, None), Some(OTHER)), "no directory");
    assert_eq!(place(&project(DEMO, Some("")), None), "no directory");
}

// --- sessions ---

#[test]
fn shell_quote_is_what_shlex_quote_gives() {
    let table = [
        ("", "''"),
        ("abc", "abc"),
        ("a@%+=:,./-_Z9", "a@%+=:,./-_Z9"),
        ("a b", "'a b'"),
        ("it's", "'it'\"'\"'s'"),
        ("é", "'é'"),
        ("a$b", "'a$b'"),
        ("a\nb", "'a\nb'"),
        ("~", "'~'"),
        ("a*", "'a*'"),
    ];
    for (given, quoted) in table {
        assert_eq!(shell_quote(given), quoted, "{given:?}");
    }
}

#[test]
fn python_json_escapes_everything_outside_ascii() {
    let text = json!("a\"b\\c\u{8}\u{c}\n\r\t\u{7f}\0\u{1f} é 修 😀 ~ /");
    let written = r#""a\"b\\c\b\f\n\r\t\u007f\u0000\u001f \u00e9 \u4fee \ud83d\ude00 ~ /""#;
    assert_eq!(python_json(&text, Some(2)), written);
    assert_eq!(python_json(&text, None), written);
    assert_eq!(python_json(&json!("\u{2028}"), None), r#""\u2028""#);
    assert_eq!(
        python_json(&json!("\u{abcd}\u{10ffff}"), None),
        r#""\uabcd\udbff\udfff""#
    );
    assert_eq!(python_json(&json!({"é": 1}), None), r#"{"\u00e9": 1}"#);
}

#[test]
fn python_json_breaks_lines_as_indent_2_does() {
    for empty in [json!([]), json!({})] {
        let written = empty.to_string();
        assert_eq!(python_json(&empty, Some(2)), written);
        assert_eq!(python_json(&empty, None), written);
    }
    let list = json!([1, null, true, false, "x"]);
    assert_eq!(
        python_json(&list, Some(2)),
        "[\n  1,\n  null,\n  true,\n  false,\n  \"x\"\n]"
    );
    assert_eq!(python_json(&list, None), r#"[1, null, true, false, "x"]"#);
    let nested = json!({"a": [], "b": {"c": [1, [2]]}});
    assert_eq!(
        python_json(&nested, Some(2)),
        "{\n  \"a\": [],\n  \"b\": {\n    \"c\": [\n      1,\n      [\n        2\n      ]\n    \
         ]\n  }\n}"
    );
    assert_eq!(
        python_json(&nested, None),
        r#"{"a": [], "b": {"c": [1, [2]]}}"#
    );
    assert_eq!(python_json(&json!([[]]), Some(2)), "[\n  []\n]");
    assert_eq!(python_json(&json!(-7), Some(2)), "-7");
}

#[test]
fn relative_age_is_just_now_then_minutes_hours_and_days() {
    let table = [
        ("2025-12-31T00:00:00.000000Z", "just now"), // the clock was set back
        ("2026-01-01T00:00:00.000000Z", "just now"),
        ("2026-01-01T00:00:59.999999Z", "just now"),
        ("2026-01-01T00:01:00.000000Z", "1 minute ago"),
        ("2026-01-01T00:01:59.999999Z", "1 minute ago"),
        ("2026-01-01T00:02:00.000000Z", "2 minutes ago"),
        ("2026-01-01T00:59:59.000000Z", "59 minutes ago"),
        ("2026-01-01T01:00:00.000000Z", "1 hour ago"),
        ("2026-01-01T01:59:59.999999Z", "1 hour ago"),
        ("2026-01-01T02:00:00.000000Z", "2 hours ago"),
        ("2026-01-01T23:59:59.000000Z", "23 hours ago"),
        ("2026-01-02T00:00:00.000000Z", "1 day ago"),
        ("2026-01-02T23:59:59.999999Z", "1 day ago"),
        ("2026-01-03T00:00:00.000000Z", "2 days ago"),
        ("2026-02-12T00:00:00.000000Z", "42 days ago"),
    ];
    for (reference, age) in table {
        assert_eq!(
            relative_age(T0, reference).as_deref(),
            Some(age),
            "{reference}"
        );
    }
    assert_eq!(relative_age("yesterday", T0), None);
    assert_eq!(relative_age(T0, "today"), None);
}

#[test]
fn session_block_of_a_session_with_nothing_recorded() {
    assert_eq!(
        session_block(&session(), &names(), T1, &[]),
        block_with("demo (aaaaaaaaaa)")
    );
    // empty text is shown like nothing
    let blank = Session {
        branch: Some(String::new()),
        ended_at: Some(String::new()),
        summary: Some(String::new()),
        ..session()
    };
    assert_eq!(
        session_block(&blank, &names(), T1, &[]),
        block_with("demo (aaaaaaaaaa)")
    );
}

#[test]
fn session_block_names_the_project_or_says_why_there_is_none() {
    let table = [
        (
            Session {
                project_id: None,
                ..session()
            },
            "(unbound)",
        ),
        (
            Session {
                project_id: Some(NOT_LISTED.to_string()),
                ..session()
            },
            "unknown project (cccccccccc)",
        ),
        (pending(None), "pending"),
        (pending(Some(OTHER)), "pending -> other (bbbbbbbbbb)"),
        (
            pending(Some(NOT_LISTED)),
            "pending -> unknown project (cccccccccc)",
        ),
        // a pending session's own project is never the one shown
        (
            Session {
                project_id: Some(DEMO.to_string()),
                ..pending(None)
            },
            "pending",
        ),
    ];
    for (session, cell) in table {
        assert_eq!(
            session_block(&session, &names(), T1, &[]),
            block_with(cell),
            "{cell}"
        );
    }
}

#[test]
fn session_block_lines_of_a_session_with_everything() {
    let session = busy_session();
    let moved = |seq, from: &str, from_id: Option<&str>, to: &str, to_id: Option<&str>| {
        SessionMove::new(
            session.key.clone(),
            seq,
            T1,
            from,
            from_id.map(str::to_string),
            to,
            to_id.map(str::to_string),
        )
        .unwrap()
    };
    let moves = [
        moved(1, "/w", Some(DEMO), "/x\ny", None),
        moved(2, "/x\ny", None, "/z", Some(NOT_LISTED)),
    ];
    let lines = [
        "claude-code it's",
        "  last active: 2026-01-01T00:05:00.000000Z (2 hours ago)",
        "  project: demo (aaaaaaaaaa)",
        "  branch: ma in",
        "  moved: 2026-01-01T00:05:00.000000Z from /w (demo (aaaaaaaaaa)) to /x y (-)",
        "  moved: 2026-01-01T00:05:00.000000Z from /x y (-) to /z (unknown project (cccccccccc))",
        "  first recorded: 2026-01-01T00:00:00.000000Z",
        "  last end event: 2026-01-01T00:05:00.000000Z",
        "  first prompt: first one",
        "  latest prompt: (omitted: secret)",
        "  summary: Fixed it. Then docs. ",
        "  resume: claude --resume 'it'\"'\"'s'",
    ];
    assert_eq!(
        session_block(&session, &names(), T2, &moves),
        lines.join("\n")
    );
}

#[test]
fn session_item_is_the_item_an_agent_and_a_script_share() {
    let busy = concat!(
        r#"{"harness": "claude-code", "session_id": "it's", "project": "aaaaaaaaaa", "#,
        r#""branch": "ma\u001bin", "entry_cwd": "/w", "#,
        r#""first_recorded": "2026-01-01T00:00:00.000000Z", "#,
        r#""last_active_at": "2026-01-01T00:05:00.000000Z", "#,
        r#""last_end_event_at": "2026-01-01T00:05:00.000000Z", "#,
        r#""first_prompt": {"at": "2026-01-01T00:00:00.000000Z", "text": "first\u200bone"}, "#,
        r#""recent_prompts": [{"at": "2026-01-01T00:00:00.000000Z", "text": "first\u200bone"}, "#,
        r#"{"at": "2026-01-01T00:05:00.000000Z", "omitted": "secret"}], "#,
        r#""resume_command": "claude --resume 'it'\"'\"'s'", "#,
        r#""summary": "Fixed  it.\nThen\tdocs.\u202e", "#,
        r#""summary_at": "2026-01-01T00:05:00.000000Z"}"#
    );
    let item = Value::Object(session_item(&busy_session()));
    assert_eq!(python_json(&item, None), busy);
    let plain = concat!(
        r#"{"harness": "codex", "session_id": "sess-1", "project": "aaaaaaaaaa", "#,
        r#""branch": null, "entry_cwd": "/w", "first_recorded": "2026-01-01T00:00:00.000000Z", "#,
        r#""last_active_at": "2026-01-01T00:05:00.000000Z", "last_end_event_at": null, "#,
        r#""first_prompt": null, "recent_prompts": [], "#,
        r#""resume_command": "codex resume sess-1", "summary": null, "summary_at": null}"#
    );
    let item = Value::Object(session_item(&session()));
    assert_eq!(python_json(&item, None), plain);
}

// --- export ---

#[test]
fn write_private_refuses_a_name_that_would_leave_the_directory() {
    let (_sandbox, base, inner) = inner();
    let directory = held(&inner).unwrap();
    for name in ["../x", "", ".", "..", "x/y", "/x", "x\0y"] {
        let refused = write_private(&directory, name, "t").unwrap_err();
        assert_eq!(refused.to_string(), "unsafe export name", "{name:?}");
        assert_eq!(refused.raw_os_error(), None, "{name:?}");
    }
    assert!(!base.join("x").exists());
    assert_eq!(names_in(&inner), Vec::<String>::new());
    assert_eq!(names_in(&base), ["home", "inner"]);
}

#[test]
fn write_private_makes_a_0600_file_once_and_never_through_a_link() {
    let (_sandbox, base, inner) = inner();
    let directory = held(&inner).unwrap();
    write_private(&directory, "note.md", "caf\u{e9}\n").unwrap();
    assert_eq!(
        fs::read(inner.join("note.md")).unwrap(),
        "caf\u{e9}\n".as_bytes()
    );
    assert_eq!(mode(&inner.join("note.md")), 0o600);
    // a name that is taken is never written over
    let taken = write_private(&directory, "note.md", "second").unwrap_err();
    assert_eq!(taken.raw_os_error(), Some(libc::EEXIST));
    assert_eq!(
        fs::read(inner.join("note.md")).unwrap(),
        "caf\u{e9}\n".as_bytes()
    );
    // nor is a link followed, whether it leads to a file or to nothing
    fs::write(base.join("outside"), "kept").unwrap();
    symlink(base.join("outside"), inner.join("to-file")).unwrap();
    symlink(base.join("nowhere"), inner.join("to-nothing")).unwrap();
    for name in ["to-file", "to-nothing"] {
        assert!(
            write_private(&directory, name, "through").is_err(),
            "{name}"
        );
    }
    assert_eq!(fs::read(base.join("outside")).unwrap(), b"kept");
    assert!(!base.join("nowhere").exists());
}

#[test]
fn a_directory_is_held_only_when_it_is_no_link() {
    let (_sandbox, base, inner) = inner();
    symlink(&inner, base.join("link")).unwrap();
    let refused = held(&base.join("link")).unwrap_err();
    // which of the two depends on the system
    let errno = refused.raw_os_error();
    assert!(
        errno == Some(libc::ELOOP) || errno == Some(libc::ENOTDIR),
        "{refused}"
    );
    fs::write(base.join("file"), "").unwrap();
    assert_eq!(
        held(&base.join("file")).unwrap_err().raw_os_error(),
        Some(libc::ENOTDIR)
    );
    assert!(held(&inner).is_ok());
}

#[test]
fn a_folder_is_made_0700_inside_the_held_directory_and_never_reused() {
    let (_sandbox, base, inner) = inner();
    let directory = held(&inner).unwrap();
    let made = folder(&directory, DEMO).unwrap();
    assert_eq!(mode(&inner.join(DEMO)), 0o700);
    write_private(&made, "note.md", "t").unwrap();
    assert!(inner.join(DEMO).join("note.md").is_file());
    assert_eq!(
        folder(&directory, DEMO).unwrap_err().raw_os_error(),
        Some(libc::EEXIST)
    );
    // a link in the name's place is not a folder to write into
    let outside = base.join("outside");
    fs::create_dir(&outside).unwrap();
    symlink(&outside, inner.join(OTHER)).unwrap();
    assert_eq!(
        folder(&directory, OTHER).unwrap_err().raw_os_error(),
        Some(libc::EEXIST)
    );
    assert_eq!(folder(&directory, "../x").unwrap_err().raw_os_error(), None);
    assert_eq!(names_in(&outside), Vec::<String>::new());
    assert!(!base.join("x").exists());
}

#[test]
fn a_snapshot_counts_the_memories_it_wrote() {
    let (_sandbox, _base, inner) = inner();
    let listed = [
        (
            Project {
                name: "de\nmo  x".to_string(),
                ..project(DEMO, Some("/w\n/x"))
            },
            vec![
                memory("mmmmmmmmm1", DEMO, "one"),
                memory("mmmmmmmmm2", DEMO, "two\n"),
            ],
        ),
        (project(OTHER, None), Vec::new()),
    ];
    let mut count = 0;
    snapshot_at(&inner, &listed, Some(OTHER), &mut count).unwrap();
    assert_eq!(count, 2);
    assert_eq!(names_in(&inner), [DEMO, "projects.md"]);
    assert_eq!(
        names_in(&inner.join(DEMO)),
        ["mmmmmmmmm1.md", "mmmmmmmmm2.md"]
    );
    assert_eq!(
        fs::read_to_string(inner.join("projects.md")).unwrap(),
        "- aaaaaaaaaa de mo x (/w /x)\n- bbbbbbbbbb demo (global)\n"
    );
    let written = fs::read_to_string(inner.join(DEMO).join("mmmmmmmmm2.md")).unwrap();
    assert!(written.starts_with("---\nid: \"mmmmmmmmm2\"\nproject_id: \"aaaaaaaaaa\"\n"));
    assert!(
        written.ends_with("\ndescription: \"the cue\"\n---\ntwo"),
        "{written}"
    );
}

#[test]
fn a_snapshot_is_not_written_through_a_target_that_became_a_link() {
    // the directory the export made, swapped for a link before it was opened
    let (_sandbox, base, inner) = inner();
    let target = base.join("snap");
    symlink(&inner, &target).unwrap();
    let listed = [(
        project(DEMO, Some("/w")),
        vec![memory("mmmmmmmmm1", DEMO, "one")],
    )];
    let mut count = 0;
    let stopped = snapshot_at(&target, &listed, None, &mut count).unwrap_err();
    let errno = stopped.raw_os_error();
    assert!(
        errno == Some(libc::ELOOP) || errno == Some(libc::ENOTDIR),
        "{stopped}"
    );
    assert_eq!(count, 0);
    assert_eq!(names_in(&inner), Vec::<String>::new());
}

#[test]
fn a_snapshot_is_not_written_through_a_project_folder_that_is_a_link() {
    let (_sandbox, base, inner) = inner();
    let outside = base.join("outside");
    fs::create_dir(&outside).unwrap();
    symlink(&outside, inner.join(DEMO)).unwrap();
    let listed = [(
        project(DEMO, Some("/w")),
        vec![memory("mmmmmmmmm1", DEMO, "one")],
    )];
    let mut count = 0;
    let stopped = snapshot_at(&inner, &listed, None, &mut count).unwrap_err();
    assert_eq!(stopped.raw_os_error(), Some(libc::EEXIST));
    assert_eq!(count, 0);
    assert_eq!(names_in(&outside), Vec::<String>::new());
    assert_eq!(names_in(&inner), [DEMO, "projects.md"]);
}

#[test]
fn a_snapshot_that_stops_half_way_has_counted_what_it_wrote() {
    let (_sandbox, base, inner) = inner();
    // the same file twice: the second write finds the name taken
    let twice = vec![
        memory("mmmmmmmmm1", DEMO, "one"),
        memory("mmmmmmmmm1", DEMO, "again"),
    ];
    let listed = [(project(DEMO, Some("/w")), twice)];
    let mut count = 0;
    let stopped = snapshot_at(&inner, &listed, None, &mut count).unwrap_err();
    assert_eq!((stopped.raw_os_error(), count), (Some(libc::EEXIST), 1));
    let written = fs::read_to_string(inner.join(DEMO).join("mmmmmmmmm1.md")).unwrap();
    assert!(written.ends_with("---\none"), "{written}");

    // a name that is no single path component stops it without an error of the system
    for (project_id, memory_id) in [("../x", "mmmmmmmmm1"), (DEMO, "../x")] {
        let second = base.join(format!("second-{memory_id}").replace(['.', '/'], ""));
        fs::create_dir(&second).unwrap();
        let listed = [(
            project(project_id, Some("/w")),
            vec![memory(memory_id, DEMO, "one")],
        )];
        let mut count = 0;
        let stopped = snapshot_at(&second, &listed, None, &mut count).unwrap_err();
        assert_eq!(
            (stopped.raw_os_error(), count),
            (None, 0),
            "{project_id} {memory_id}"
        );
        assert!(!base.join("x").exists() && !base.join("x.md").exists());
    }
}

/// `memriver export <directory>` over a store that is not there, and what it printed.
fn exported(base: &Path, directory: &Path) -> (i32, String) {
    let store = base.join("mem").to_string_lossy().into_owned();
    let env = memriver_core::settings::Env::from([("MEMRIVER_ROOT".to_string(), store)]);
    let (mut stdin, mut stdout, mut stderr) = (&b""[..], Vec::new(), Vec::new());
    let mut io = crate::cli::Io {
        stdin: &mut stdin,
        stdin_is_tty: false,
        stdout: &mut stdout,
        stderr: &mut stderr,
    };
    let home = base.join("home");
    let mut inv = Invocation {
        env: &env,
        home: &home,
        cwd: base,
        io: &mut io,
    };
    let args = Export {
        directory: directory.to_string_lossy().into_owned(),
        root: None,
    };
    let code = export(&args, &mut inv).unwrap();
    assert_eq!(stderr, b"");
    (code, String::from_utf8(stdout).unwrap())
}

// the directory above the one that was just made is moved away, and a link to another
// directory, which has one of the same name, stands in its place
#[test]
fn an_export_is_written_into_the_directory_it_made_whatever_stands_above_it_then() {
    let (_sandbox, base, _inner) = inner();
    let (above, moved, other) = (base.join("above"), base.join("moved"), base.join("other"));
    fs::create_dir(&above).unwrap();
    fs::create_dir_all(other.join("snap")).unwrap();
    {
        let (above, moved, other) = (above.clone(), moved.clone(), other.clone());
        meanwhile(move || {
            fs::rename(&above, &moved).unwrap();
            symlink(&other, &above).unwrap();
        });
    }
    let (code, printed) = exported(&base, &above.join("snap"));
    let shown = above.join("snap");
    assert_eq!(
        (code, printed),
        (0, format!("exported 0 memories to {}\n", shown.display()))
    );
    assert_eq!(names_in(&moved.join("snap")), ["projects.md"]);
    assert_eq!(names_in(&other.join("snap")), Vec::<String>::new());
    assert_eq!(mode(&moved.join("snap")), 0o700);
}

// the directory an export is made in need only be one that can be searched and written
#[test]
fn an_export_is_made_in_a_directory_that_cannot_be_listed() {
    let (_sandbox, base, inner) = inner();
    fs::set_permissions(&inner, fs::Permissions::from_mode(0o300)).unwrap();
    let (code, printed) = exported(&base, Path::new("inner/snap"));
    fs::set_permissions(&inner, fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(code, 0, "{printed}");
    assert_eq!(names_in(&inner.join("snap")), ["projects.md"]);
    // and what the system says of a directory that is not there, or is a file, is said
    fs::write(base.join("file"), "").unwrap();
    for (directory, reason) in [
        ("missing/snap", "No such file or directory"),
        ("file/snap", "Not a directory"),
        ("missing/..", "No such file or directory"),
    ] {
        let (code, printed) = exported(&base, Path::new(directory));
        let shown = base.join(directory);
        let refused = format!(
            "refused: {} could not be created ({reason})\n",
            shown.display()
        );
        assert_eq!((code, printed), (2, refused));
    }
    let (code, printed) = exported(&base, Path::new("inner/snap/"));
    assert_eq!(code, 2);
    assert!(
        printed.ends_with("/inner/snap already exists\n"),
        "{printed}"
    );
}
