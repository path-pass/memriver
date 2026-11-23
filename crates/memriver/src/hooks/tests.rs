//! What needs a hand inside the hook: a content policy that fails, and the panic line
//! it would print, which only a process of its own can show.

use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};

use memriver_core::application::memory::PolicySource;
use memriver_core::application::sessions::SessionService;
use memriver_core::models::session::PromptOmission;
use memriver_core::repository::sqlite::project_store::ProjectStore;
use memriver_core::repository::sqlite::session_store::SessionStore;
use memriver_test_support::{Sandbox, isolated_command};

use super::*;

thread_local! {
    /// Set by a test: the services a hook builds on this thread check prompts with a
    /// policy whose rules fail to load.
    static BROKEN_POLICY: Cell<bool> = const { Cell::new(false) };
}

thread_local! {
    /// Set by a test: building the services panics, as a fault anywhere in a hook would.
    static FAILING_BUILD: Cell<bool> = const { Cell::new(false) };
}

pub(super) fn with_test_seams(mut services: Services, root: &Path, place: &Place) -> Services {
    if FAILING_BUILD.with(Cell::get) {
        panic!("/private/secret is on fire");
    }
    if BROKEN_POLICY.with(Cell::get) {
        let broken: PolicySource = Arc::new(LazyLock::new(|| panic!("the rules did not load")));
        services.session = Arc::new(SessionService::new(
            SessionStore::new(root),
            ProjectStore::new(root, place.home),
            broken,
            place.env.clone(),
        ));
    }
    services
}

/// A store with a project bound at `work`, a session started there, then one prompt
/// checked by a policy that fails -- through `run_hook`, or with the hook's own panic
/// line left on (`quiet` false).
fn prompt_with_a_broken_policy(quiet: bool) {
    let sandbox = Sandbox::new();
    let base = std::fs::canonicalize(sandbox.dir.path()).unwrap();
    let (store, work, home) = (base.join("store"), base.join("work"), base.join("home"));
    std::fs::create_dir_all(&work).unwrap();
    let env = sandbox.env();
    let services = build_services(&Settings::light(store.clone()), &home, &env, None);
    services.project.ensure_global().unwrap();
    let plan = services.project.plan_root(&work, None).unwrap();
    services.project.init_project("demo", &plan).unwrap();
    let payload = r#"{"session_id": "s", "source": "startup", "prompt": "hello"}"#;
    let hook = |event| {
        run_hook(
            event,
            Harness::Codex,
            payload,
            Some(&store),
            None,
            &work,
            &env,
            &home,
        )
    };
    assert!(hook(Event::SessionStart).stdout.contains("project: demo ["));
    BROKEN_POLICY.with(|broken| broken.set(true));
    let result = match quiet {
        true => hook(Event::UserPromptSubmit),
        false => {
            let place = Place {
                root: Some(&store),
                project_dir: None,
                cwd: &work,
                env: &env,
                home: &home,
            };
            others::user_prompt_submit(Harness::Codex, payload, &place)
        }
    };
    BROKEN_POLICY.with(|broken| broken.set(false));
    assert_eq!(result, HookResult::default());
    let key = SessionKey::new(Harness::Codex, "s").unwrap();
    let row = services.session.list_sessions(None, "", None).unwrap();
    let row = row.iter().find(|session| session.key == key).unwrap();
    let omitted = memriver_core::models::session::PromptBody::Omitted(PromptOmission::ScanError);
    assert_eq!(row.first_prompt.as_ref().unwrap().body(), &omitted);
}

#[test]
#[ignore = "run in a process of its own by a_failed_prompt_check_prints_no_panic_line"]
fn child_a_failed_prompt_check_through_the_hook() {
    prompt_with_a_broken_policy(true);
}

#[test]
#[ignore = "run in a process of its own by a_failed_prompt_check_prints_no_panic_line"]
fn child_a_failed_prompt_check_outside_the_hook() {
    prompt_with_a_broken_policy(false);
}

#[test]
fn a_failed_prompt_check_prints_no_panic_line() {
    // the same prompt and policy in a process of its own: outside the hook the
    // default panic line reaches stderr, through the hook nothing does
    let program = std::env::current_exe().unwrap();
    for (child, printed) in [
        ("child_a_failed_prompt_check_outside_the_hook", true),
        ("child_a_failed_prompt_check_through_the_hook", false),
    ] {
        let sandbox = Sandbox::new();
        let name = format!("hooks::tests::{child}");
        let done = isolated_command(&sandbox, &program)
            .args([
                "--exact",
                &name,
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .output()
            .unwrap();
        let (stdout, stderr) = (
            String::from_utf8_lossy(&done.stdout),
            String::from_utf8_lossy(&done.stderr),
        );
        assert!(done.status.success(), "{stdout}\n{stderr}");
        assert!(stdout.contains("1 passed"), "{stdout}");
        assert_eq!(stderr.contains("panicked"), printed, "{child}: {stderr}");
    }
}

#[test]
fn a_panic_is_silent_inside_a_hook_and_printed_again_after_it() {
    let outer = QUIET.with(Cell::get);
    let caught = quietly(|| {
        assert!(QUIET.with(Cell::get));
        catch_unwind(|| panic!("inside")).is_err()
    });
    assert!(caught);
    assert_eq!(QUIET.with(Cell::get), outer);
}

#[test]
fn emit_spaces_items_as_python_and_keeps_text_outside_ascii() {
    let value = serde_json::json!({
        "a": [1, "\u{8336}\u{1f375}", null, true],
        "b": {"c": "\"\\\n\r\t\u{8}\u{c}\u{1}\u{1f}\u{7f}\u{2028}"}
    });
    assert_eq!(
        emit(&value),
        "{\"a\": [1, \"\u{8336}\u{1f375}\", null, true], \
         \"b\": {\"c\": \"\\\"\\\\\\n\\r\\t\\b\\f\\u0001\\u001f\u{7f}\u{2028}\"}}\n"
    );
}

/// A store with project `demo` bound at `work`; (sandbox, store, work, home).
fn store_with_a_project() -> (Sandbox, PathBuf, PathBuf, PathBuf) {
    let sandbox = Sandbox::new();
    let base = std::fs::canonicalize(sandbox.dir.path()).unwrap();
    let (store, work, home) = (base.join("store"), base.join("work"), base.join("home"));
    std::fs::create_dir_all(&work).unwrap();
    let services = build_services(&Settings::light(store.clone()), &home, &sandbox.env(), None);
    services.project.ensure_global().unwrap();
    let plan = services.project.plan_root(&work, None).unwrap();
    services.project.init_project("demo", &plan).unwrap();
    (sandbox, store, work, home)
}

#[test]
fn a_fault_inside_a_hook_never_fails_the_harness() {
    let (sandbox, store, work, home) = store_with_a_project();
    let env = sandbox.env();
    let payload = r#"{"session_id": "s", "source": "startup", "prompt": "p",
                      "stop_hook_active": false, "tool_use_id": "toolu_1"}"#;
    FAILING_BUILD.with(|failing| failing.set(true));
    let results: Vec<(Event, HookResult)> = [
        Event::SessionStart,
        Event::UserPromptSubmit,
        Event::Stop,
        Event::SessionEnd,
        Event::PreToolUse,
    ]
    .into_iter()
    .map(|event| {
        let result = run_hook(
            event,
            Harness::ClaudeCode,
            payload,
            Some(&store),
            None,
            &work,
            &env,
            &home,
        );
        (event, result)
    })
    .collect();
    FAILING_BUILD.with(|failing| failing.set(false));
    for (event, result) in results {
        let expected = match event {
            // one fixed line that names no path
            Event::SessionStart => HookResult::stderr(STORE_UNAVAILABLE),
            _ => HookResult::default(),
        };
        assert_eq!(result, expected, "{event:?}");
    }
}

#[test]
fn stop_and_pre_tool_use_never_load_the_content_policy() {
    // a policy that panics when loaded: Stop still nudges and PreToolUse still records
    let (sandbox, store, work, home) = store_with_a_project();
    let env = sandbox.env();
    let hook = |event, payload: &str| {
        run_hook(
            event,
            Harness::ClaudeCode,
            payload,
            Some(&store),
            None,
            &work,
            &env,
            &home,
        )
    };
    hook(
        Event::SessionStart,
        r#"{"session_id": "s", "source": "startup"}"#,
    );
    for _ in 0..5 {
        hook(
            Event::UserPromptSubmit,
            r#"{"session_id": "s", "prompt": "next"}"#,
        );
    }
    BROKEN_POLICY.with(|broken| broken.set(true));
    let nudged = hook(
        Event::Stop,
        r#"{"session_id": "s", "stop_hook_active": false}"#,
    );
    let recorded = hook(
        Event::PreToolUse,
        r#"{"session_id": "s", "tool_use_id": "toolu_1"}"#,
    );
    BROKEN_POLICY.with(|broken| broken.set(false));
    assert!(
        nudged.stdout.starts_with(r#"{"decision": "block""#),
        "{nudged:?}"
    );
    assert_eq!(recorded, HookResult::default());
    let services = build_services(&Settings::light(store.clone()), &home, &env, None);
    assert!(
        services
            .session
            .call_record("claude-code", "toolu_1")
            .unwrap()
            .is_some()
    );
}

#[test]
fn move_notice_long_fields_fit_budget() {
    // the widest names and directories a notice can carry, under compact with a
    // pending notice too, leave the header and fit Claude Code's limit
    let (name, entry) = (
        "\u{8336}\u{1f375}".repeat(400),
        format!("/{}", "\u{76ee}\u{5f55}\u{1f600}/".repeat(400)),
    );
    let shown = (session_start::field(&name), session_start::field(&entry));
    assert_eq!(shown.0.chars().count(), 120);
    let header = format!("project: {}", "\u{8336}".repeat(600));
    let index: Vec<String> = (0..100)
        .map(|n| {
            format!(
                "- [project] {n:064}: {} (2026-01-01)",
                "\u{1f375}".repeat(60)
            )
        })
        .collect();
    let index = format!("{header}\n{}", index.join("\n"));
    for pending in [false, true] {
        let target = crate::protocol_text::project_phrase(&shown.0, "bbbbbbbbbb");
        let notice = match pending {
            false => crate::protocol_text::move_notice(
                "no project",
                &shown.1,
                crate::protocol_text::Moved::Project {
                    name: &shown.0,
                    id: "bbbbbbbbbb",
                },
            ),
            true => format!(
                "{}\n{}",
                crate::protocol_text::move_notice(
                    "",
                    &shown.1,
                    crate::protocol_text::Moved::Pending {
                        old: &target,
                        new: &target
                    },
                ),
                crate::protocol_text::pending_notice(&shown.1, Some(&target))
            ),
        };
        let text = compose::compose(&index, Some("compact"), Harness::ClaudeCode, Some(&notice));
        assert!(text.encode_utf16().count() <= compose::INLINE_LIMIT);
        assert!(text.contains(&format!("begin ---\n{header}\n")));
        assert!(text.starts_with(&notice));
        assert!(!notice.contains(&name) && !notice.contains(&entry));
    }
    assert_eq!(session_start::field("left\u{202e}right"), "left right");
}

#[test]
fn a_vanished_project_is_named_by_its_id() {
    let (sandbox, store, _, home) = store_with_a_project();
    let services = build_services(&Settings::light(store), &home, &sandbox.env(), None);
    let outcome = memriver_core::models::session::MoveOutcome {
        from_project_id: Some("abcdefghjk".to_string()),
        to_project_id: None,
        from_candidate_id: None,
        to_candidate_id: None,
        to_entry_cwd: "/w".to_string(),
        pending: false,
        degraded: false,
    };
    let context = services
        .project
        .open_project_context(Path::new("/"))
        .unwrap();
    let notice = session_start::notice(&services, &context, Some(&outcome)).unwrap();
    assert!(
        notice.starts_with(
            "[memriver] This session moved with /cd from project abcdefghjk [abcdefghjk] to /w,"
        ),
        "{notice}"
    );
}
