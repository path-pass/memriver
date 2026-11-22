//! What an agent may do to memories: write one, read one, rewrite one, delete one --
//! in its own project only, and with every refusal leaving nothing behind.

mod common;

use std::cell::{Cell, RefCell};
use std::fs;
use std::rc::Rc;
use std::sync::{Arc, Barrier};
use std::thread;

use common::{CAPS, Env, context_of, core, setup, value};
use memriver_core::application::memory::{MemoryLimits, MemoryService, policy_source};
use memriver_core::content_policy::{ContentClassifier, Verdict};
use memriver_core::models::changes::{Op, SourceRef};
use memriver_core::models::context::{ProjectContext, ProjectContextState};
use memriver_core::models::errors::{CoreError, WriteError};
use memriver_core::models::helpers::{is_id, is_timestamp};
use memriver_core::models::memory::{Memory, MemoryType, Trust};
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::repository::sqlite::project_store::ProjectStore;
use memriver_core::settings::Settings;

const SECRET: &str = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";

fn rejected<T: std::fmt::Debug>(result: Result<T, WriteError>) -> (String, String, Option<String>) {
    match core(result) {
        CoreError::ContentRejected {
            message,
            rule_id,
            memory_id,
            ..
        } => (message, rule_id, memory_id),
        other => panic!("not a content refusal: {other:?}"),
    }
}

/// Every table a write touches, counted.
fn rows(env: &Env) -> [i64; 5] {
    [
        "memories",
        "memory_versions",
        "changes",
        "change_steps",
        "memory_sources",
    ]
    .map(|table| env.count(table))
}

#[test]
fn write_records_one_change_one_step_one_version() {
    let env = setup();
    let service = env.services().memory;
    let memory = service
        .record(
            "  the body \u{1f}",
            "feedback",
            false,
            "claude-code",
            " a cue\t",
            &env.context(),
        )
        .unwrap();
    assert!(is_id(&memory.id) && is_timestamp(&memory.created));
    assert_eq!(
        (
            memory.project_id.as_str(),
            memory.type_,
            memory.trust,
            memory.sync,
            memory.version
        ),
        (
            env.project.id.as_str(),
            MemoryType::Feedback,
            Trust::Agent,
            false,
            1
        )
    );
    // stripped as Python strips: U+001F goes too
    assert_eq!(
        (memory.description.as_str(), memory.body.as_str()),
        ("a cue", "the body")
    );
    assert_eq!(
        (
            memory.source_harness.as_str(),
            memory.source_method.as_str()
        ),
        ("claude-code", "mcp")
    );
    assert_eq!(memory.created, memory.updated);
    assert_eq!(
        (&memory.deleted_at, &memory.last_read_at, memory.level),
        (&None, &None, None)
    );
    assert!(memory.tags.is_empty());
    assert_eq!(rows(&env), [1, 1, 1, 1, 0]);
    let raw = env.raw();
    let change: (String, String, String, Option<String>, i64, Option<String>) = raw
        .query_row(
            "SELECT change_id, at, changed_by, changed_via, step_count, undoes FROM changes",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            },
        )
        .unwrap();
    assert!(is_id(&change.0) && is_timestamp(&change.1) && change.1 <= memory.created);
    assert_eq!(
        (change.2.as_str(), change.3.as_deref(), change.4, &change.5),
        ("mcp", Some("claude-code"), 1, &None)
    );
    let step: (String, i64, String, String, Option<i64>, i64) = raw
        .query_row(
            "SELECT change_id, step, memory_id, op, before_version, after_version \
             FROM change_steps",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(
        step,
        (
            change.0.clone(),
            1,
            memory.id.clone(),
            "create".to_string(),
            None,
            1
        )
    );
    let version: (
        String,
        i64,
        String,
        String,
        i64,
        String,
        String,
        i64,
        String,
        String,
    ) = raw
        .query_row(
            "SELECT memory_id, version, type, trust, sync, description, body, deleted, \
             change_id, tags FROM memory_versions",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                    row.get(7)?,
                    row.get(8)?,
                    row.get(9)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(
        version,
        (
            memory.id.clone(),
            1,
            "feedback".to_string(),
            "agent".to_string(),
            0,
            "a cue".to_string(),
            "the body".to_string(),
            0,
            change.0,
            "[]".to_string()
        )
    );
    // and it reads back as it was returned, in every context that may read it
    assert_eq!(
        service
            .read(
                &memory.id,
                &context_of(Some(&env.project.id), None),
                "claude-code"
            )
            .unwrap(),
        memory
    );
    // the same memory, but for the mark the first read left
    let again = service
        .read(&memory.id, &env.context(), "claude-code")
        .unwrap();
    assert!(again.last_read_at.is_some());
    assert_eq!(
        Memory {
            last_read_at: None,
            ..again
        },
        memory
    );
}

#[test]
fn precheck_measures_raw_text_kernel_rechecks_stripped() {
    let mut env = setup();
    env.settings.max_body_chars = 10;
    let service = env.services().memory;
    // the raw text is what the first check measures: eleven characters, nine of them
    // spaces, are too many, in the policy's own words
    let (message, rule_id, memory_id) =
        rejected(service.record("    ab     ", "project", true, "codex", "", &env.context()));
    assert_eq!(
        (message.as_str(), rule_id.as_str(), memory_id),
        (
            "content too large (11 > 10 chars); store a summary or pointer instead",
            "too-large",
            None
        )
    );
    assert_eq!(rows(&env), [0; 5]);
    // ten raw characters pass, and are stored stripped
    let memory = service
        .record("    ab    ", "project", true, "codex", "", &env.context())
        .unwrap();
    assert_eq!(memory.body, "ab");
    // the store checks the state it is about to write, with the generic sentence: a
    // policy that lets the raw text through and refuses the stripped one shows it
    let store = env.memory_store();
    let refused = store.write(
        &Op::create(&env.project.id, "project", "", "  refuse me  "),
        &env.context().read_write_set,
        "mcp",
        Some("codex"),
        &|_, body| (body == "refuse me").then(|| "a-rule".to_string()),
        &CAPS,
    );
    let err = core(refused);
    assert_eq!(
        err.to_string(),
        "content rejected (a-rule); no change was made"
    );
    assert_eq!(rows(&env), [1, 1, 1, 1, 0]);
}

#[test]
fn blank_body_refused_blank_description_allowed() {
    let env = setup();
    let service = env.services().memory;
    for body in ["", "   ", "\n\t", "\u{1c}\u{1f}"] {
        let (message, rule_id, _) =
            rejected(service.record(body, "user", true, "codex", "a cue", &env.context()));
        assert_eq!(
            (message.as_str(), rule_id.as_str()),
            ("content is empty; nothing to store", "empty"),
            "{body:?}"
        );
    }
    assert_eq!(rows(&env), [0; 5]);
    for description in ["", "  ", "\n"] {
        let memory = service
            .record("a body", "user", true, "codex", description, &env.context())
            .unwrap();
        assert_eq!(memory.description, "", "{description:?}");
    }
    // an update to a blank body is refused the same way, the description may be cleared
    let memory = service
        .record("a body", "user", true, "codex", "a cue", &env.context())
        .unwrap();
    let (message, _, _) =
        rejected(service.update(&memory.id, 1, "  ", None, &env.context(), Some("codex")));
    assert_eq!(message, "content is empty; nothing to store");
    let cleared = service
        .update(
            &memory.id,
            1,
            "a body",
            Some(""),
            &env.context(),
            Some("codex"),
        )
        .unwrap();
    assert_eq!((cleared.description.as_str(), cleared.version), ("", 2));
}

#[test]
fn description_budget_is_8000_whatever_the_setting() {
    let mut env = setup();
    env.settings.max_body_chars = 20;
    let service = env.services().memory;
    let memory = service
        .record(
            "short",
            "user",
            true,
            "codex",
            &"d".repeat(8000),
            &env.context(),
        )
        .unwrap();
    assert_eq!(memory.description.chars().count(), 8000);
    let (message, rule_id, _) = rejected(service.record(
        "short",
        "user",
        true,
        "codex",
        &"d".repeat(8001),
        &env.context(),
    ));
    assert_eq!(
        (message.as_str(), rule_id.as_str()),
        (
            "content too large (8001 > 8000 chars); store a summary or pointer instead",
            "too-large"
        )
    );
    // counted in characters, not bytes
    env.settings.max_body_chars = 5;
    let service = env.services().memory;
    service
        .record(
            "\u{4e2d}\u{6587}\u{8bb0}\u{5fc6}\u{4f53}",
            "user",
            true,
            "codex",
            "",
            &env.context(),
        )
        .unwrap();
    // a secret in the description, or as the harness, is refused like one in the body
    for (body, harness, description) in [
        ("b", "codex", SECRET),
        (SECRET, "codex", ""),
        ("b", "ghp_012345678901234567890123456789abcdef", ""),
    ] {
        let (message, rule_id, _) =
            rejected(service.record(body, "user", true, harness, description, &env.context()));
        assert!(
            !message.is_empty() && !message.contains("wJalr"),
            "{message}"
        );
        assert!(!rule_id.is_empty());
    }
}

#[test]
fn a_harness_that_is_no_label_is_refused_before_anything_is_checked() {
    let env = setup();
    let service = env.services().memory;
    for harness in ["", "claude code", "a/b", &"h".repeat(65), "h\n", "\u{e9}"] {
        let (message, rule_id, _) =
            rejected(service.record("b", "user", true, harness, "", &env.context()));
        assert_eq!(
            (message.as_str(), rule_id.as_str()),
            (
                "invalid harness identifier (allowed: letters, digits, ., _, -, max 64 chars)",
                "invalid-harness"
            ),
            "{harness:?}"
        );
    }
    service
        .record("b", "user", true, &"h".repeat(64), "", &env.context())
        .unwrap();
    service
        .record("b", "user", true, "A.b_c-9", "", &env.context())
        .unwrap();
}

#[test]
fn a_type_or_trust_that_is_none_is_refused_in_pythons_words() {
    let env = setup();
    let service = env.services().memory;
    for (type_, shown) in [
        ("task", "'task'"),
        ("", "''"),
        ("User", "'User'"),
        ("it's", "\"it's\""),
        ("a\nb", "'a\\nb'"),
        ("\u{e9}\u{1f}", "'\u{e9}\\x1f'"),
    ] {
        let refused = service.record("b", type_, true, "codex", "", &env.context());
        assert_eq!(value(refused), format!("invalid memory type: {shown}"));
    }
    assert_eq!(rows(&env), [0; 5]);
    // the type is looked at first, then the trust, then the project's id
    let create = |type_: &str, trust: &str, project_id: &str| {
        let Op::Create {
            description,
            body,
            sources,
            sync,
            level,
            tags,
            ..
        } = Op::create(project_id, type_, "", "b")
        else {
            unreachable!()
        };
        Op::Create {
            project_id: project_id.to_string(),
            type_: type_.to_string(),
            description,
            body,
            sources,
            trust: trust.to_string(),
            sync,
            level,
            tags,
        }
    };
    assert_eq!(
        value(env.apply(&[create("task", "bogus", &env.project.id)])),
        "invalid memory type: 'task'"
    );
    assert_eq!(
        value(env.apply(&[create("user", "bo'gus", &env.project.id)])),
        "invalid trust: \"bo'gus\""
    );
    env.raw()
        .execute_batch(
            "PRAGMA foreign_keys = OFF; PRAGMA ignore_check_constraints = ON; \
             INSERT INTO projects (id, name, root, is_global) VALUES ('BAD', 'bad', NULL, 0);",
        )
        .unwrap();
    assert_eq!(
        value(env.apply(&[create("user", "bogus", "BAD")])),
        "invalid trust: 'bogus'"
    );
    assert_eq!(
        value(env.apply(&[create("user", "user", "BAD")])),
        "invalid project id"
    );
    assert_eq!(rows(&env), [0; 5]);
}

#[test]
fn unchanged_update_writes_nothing_and_returns_current() {
    let env = setup();
    let service = env.services().memory;
    let memory = service
        .record("the body", "user", true, "codex", "a cue", &env.context())
        .unwrap();
    let before = rows(&env);
    // the same text, and the same text once stripped; the description kept or repeated
    for (content, description) in [
        ("the body", None),
        ("  the body\n", None),
        ("the body", Some(" a cue ")),
    ] {
        let same = service
            .update(
                &memory.id,
                1,
                content,
                description,
                &env.context(),
                Some("codex"),
            )
            .unwrap();
        assert_eq!(same, memory, "{content:?} {description:?}");
    }
    assert_eq!(rows(&env), before);
    // every check still ran before the answer: a stale version is a conflict
    let stale = service.update(
        &memory.id,
        7,
        "the body",
        None,
        &env.context(),
        Some("codex"),
    );
    assert_eq!(
        core(stale),
        CoreError::VersionConflict(memory.id.to_string())
    );
}

#[test]
fn an_update_replaces_the_text_and_moves_the_version_and_the_time() {
    let env = setup();
    let service = env.services().memory;
    let memory = service
        .record("the body", "user", false, "codex", "a cue", &env.context())
        .unwrap();
    let updated = service
        .update(
            &memory.id,
            1,
            " another body ",
            None,
            &env.context(),
            Some("cursor"),
        )
        .unwrap();
    assert_eq!(
        Memory {
            body: "the body".to_string(),
            updated: memory.updated.clone(),
            version: 1,
            ..updated.clone()
        },
        memory
    );
    assert_eq!(
        (updated.body.as_str(), updated.version),
        ("another body", 2)
    );
    assert!(updated.updated > memory.updated && is_timestamp(&updated.updated));
    assert_eq!(rows(&env), [1, 2, 2, 2, 0]);
    let step: (String, Option<i64>, i64, Option<String>) = env
        .raw()
        .query_row(
            "SELECT s.op, s.before_version, s.after_version, c.changed_via FROM change_steps s \
             JOIN changes c ON c.change_id = s.change_id WHERE s.after_version = 2",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    assert_eq!(
        step,
        ("update".to_string(), Some(1), 2, Some("cursor".to_string()))
    );
    // a description replaces, and no harness is no `changed_via`
    let described = service
        .update(
            &memory.id,
            2,
            "another body",
            Some("new cue"),
            &env.context(),
            None,
        )
        .unwrap();
    assert_eq!(
        (described.description.as_str(), described.version),
        ("new cue", 3)
    );
    let via: Option<String> = env
        .raw()
        .query_row(
            "SELECT c.changed_via FROM change_steps s JOIN changes c \
             ON c.change_id = s.change_id WHERE s.after_version = 3",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(via, None);
}

#[test]
fn update_carries_sources_forward() {
    let env = setup();
    let service = env.services().memory;
    let source = env.record("a source");
    let Op::Create {
        project_id,
        type_,
        description,
        trust,
        sync,
        level,
        tags,
        ..
    } = Op::create(&env.project.id, "project", "", "derived")
    else {
        unreachable!()
    };
    let derived = env.manage(Op::Create {
        project_id,
        type_,
        description,
        body: "derived".to_string(),
        sources: vec![SourceRef {
            memory_id: source.id.clone(),
            version: 1,
        }],
        trust,
        sync,
        level,
        tags,
    });
    let updated = service
        .update(
            &derived,
            1,
            "derived, reworded",
            None,
            &env.context(),
            Some("codex"),
        )
        .unwrap();
    assert_eq!((updated.version, updated.trust), (2, Trust::Agent));
    let sources: Vec<(i64, String, i64)> = env
        .raw()
        .prepare(
            "SELECT version, source_id, source_version FROM memory_sources \
             WHERE memory_id = ?1 ORDER BY version",
        )
        .unwrap()
        .query_map([&derived], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(sources, [(1, source.id.clone(), 1), (2, source.id, 1)]);
}

#[test]
fn stale_version_is_a_conflict() {
    let env = setup();
    let service = env.services().memory;
    let memory = env.record("the body");
    let before = rows(&env);
    for version in [0, 2, -1, i64::MAX] {
        let conflict = CoreError::VersionConflict(memory.id.to_string());
        assert_eq!(
            core(service.update(&memory.id, version, "new", None, &env.context(), None)),
            conflict
        );
        assert_eq!(
            core(service.delete(&memory.id, version, &env.context(), None)),
            conflict
        );
    }
    assert_eq!(rows(&env), before);
    assert_eq!(
        service.read(&memory.id, &env.context(), "codex").unwrap(),
        memory
    );
}

#[test]
fn of_two_writers_with_one_version_exactly_one_wins() {
    let env = setup();
    let memory = env.record("the body");
    let barrier = Arc::new(Barrier::new(2));
    let writers: Vec<_> = ["one", "two"]
        .into_iter()
        .map(|body| {
            let (settings, home) = (env.settings.clone(), env.home.clone());
            let (context, id) = (env.context(), memory.id.clone());
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                let service = memriver_core::build_services(&settings, &home, None).memory;
                barrier.wait();
                service
                    .update(&id, 1, body, None, &context, Some("codex"))
                    .map(|memory| memory.body)
            })
        })
        .collect();
    let results: Vec<_> = writers
        .into_iter()
        .map(|writer| writer.join().unwrap())
        .collect();
    let won: Vec<String> = results.iter().filter_map(|r| r.clone().ok()).collect();
    assert_eq!(won.len(), 1, "{results:?}");
    let lost = results.into_iter().find(Result::is_err).unwrap();
    assert_eq!(core(lost), CoreError::VersionConflict(memory.id.clone()));
    let now = env
        .services()
        .memory
        .read(&memory.id, &env.context(), "codex")
        .unwrap();
    assert_eq!((&now.body, now.version), (&won[0], 2));
}

#[test]
fn delete_is_soft_and_bumps_version() {
    let env = setup();
    let service = env.services().memory;
    let memory = env.record("the body");
    assert_eq!(
        service
            .delete(&memory.id, 1, &env.context(), Some("codex"))
            .unwrap(),
        2
    );
    // gone for an agent, like an id that never was
    let gone = CoreError::MemoryNotFound(memory.id.clone());
    assert_eq!(
        service.read(&memory.id, &env.context(), "codex"),
        Err(gone.clone())
    );
    assert_eq!(
        core(service.update(&memory.id, 2, "again", None, &env.context(), None)),
        gone
    );
    assert_eq!(
        core(service.delete(&memory.id, 2, &env.context(), None)),
        gone
    );
    // the row stays, deleted at the time it was last changed
    let row: (i64, String, Option<String>, String) = env
        .raw()
        .query_row(
            "SELECT version, updated, deleted_at, body FROM memories",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    assert_eq!((row.0, row.3.as_str()), (2, "the body"));
    assert!(row.1 > memory.updated);
    assert_eq!(row.2, Some(row.1));
    let step: (String, i64, i64) = env
        .raw()
        .query_row(
            "SELECT s.op, s.after_version, v.deleted FROM change_steps s JOIN memory_versions v \
             ON v.memory_id = s.memory_id AND v.version = s.after_version \
             WHERE s.after_version = 2",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(step, ("soft_delete".to_string(), 2, 1));
    // the project is still there, and its other memories
    let other = env.record("another");
    assert_eq!(
        service.read(&other.id, &env.context(), "codex").unwrap(),
        other
    );
}

#[test]
fn a_delete_whose_text_a_newer_rule_refuses_is_refused() {
    let env = setup();
    let memory = env.record("the body");
    let refused = env.memory_store().write(
        &Op::soft_delete(&memory.id, 1),
        &env.context().read_write_set,
        "mcp",
        None,
        &|_, _| Some("newer-rule".to_string()),
        &CAPS,
    );
    assert_eq!(
        core(refused),
        CoreError::ContentRejected {
            message: String::new(),
            rule_id: "newer-rule".to_string(),
            memory_id: Some(memory.id),
            detail: String::new(),
        }
    );
    assert_eq!(rows(&env), [1, 1, 1, 1, 0]);
}

#[test]
fn global_is_read_only_on_this_path() {
    let env = setup();
    let service = env.services().memory;
    let rule = env.global("a global rule", None, &[]);
    let before = rows(&env);
    // readable
    assert_eq!(
        service
            .read(&rule, &env.context(), "codex")
            .unwrap()
            .project_id,
        env.global_id
    );
    assert_eq!(
        core(service.update(&rule, 1, "mine now", None, &env.context(), None)),
        CoreError::GlobalReadOnly
    );
    assert_eq!(
        core(service.delete(&rule, 1, &env.context(), None)),
        CoreError::GlobalReadOnly
    );
    // a context that names global as its project writes nothing either
    for context in [
        context_of(Some(&env.global_id), Some(&env.global_id)),
        context_of(Some(&env.global_id), None),
    ] {
        let refused = core(service.record("b", "user", true, "codex", "", &context));
        assert!(
            matches!(
                refused,
                CoreError::GlobalReadOnly | CoreError::ProjectUnavailable { .. }
            ),
            "{refused:?}"
        );
    }
    assert_eq!(
        core(service.record(
            "b",
            "user",
            true,
            "codex",
            "",
            &context_of(Some(&env.global_id), None)
        )),
        CoreError::GlobalReadOnly
    );
    assert_eq!(rows(&env), before);
    // the row decides, not the id the context was built with: a project turned global
    // after the context was made is read-only at once
    let mine = env.record("mine");
    env.raw()
        .execute_batch(&format!(
            "UPDATE projects SET is_global = 0, name = 'was-global' WHERE id = '{}'; \
             UPDATE projects SET is_global = 1, root = NULL WHERE id = '{}';",
            env.global_id, env.project.id
        ))
        .unwrap();
    assert_eq!(
        core(service.record("b", "user", true, "codex", "", &env.context())),
        CoreError::GlobalReadOnly
    );
    assert_eq!(
        core(service.update(&mine.id, 1, "new", None, &env.context(), None)),
        CoreError::GlobalReadOnly
    );
    assert_eq!(
        core(service.delete(&mine.id, 1, &env.context(), None)),
        CoreError::GlobalReadOnly
    );
}

#[test]
fn other_projects_memory_is_not_found() {
    let env = setup();
    let service = env.services().memory;
    let theirs = service
        .record(
            "theirs",
            "user",
            true,
            "codex",
            "",
            &context_of(Some(&env.other.id), Some(&env.global_id)),
        )
        .unwrap();
    let before = rows(&env);
    let missing = "zzzzzzzzzz";
    for id in [theirs.id.as_str(), missing] {
        let gone = CoreError::MemoryNotFound(id.to_string());
        assert_eq!(service.read(id, &env.context(), "codex"), Err(gone.clone()));
        // whatever version is named: nothing about the entry is told
        for version in [1, 2] {
            assert_eq!(
                core(service.update(id, version, "mine", None, &env.context(), None)),
                gone
            );
            assert_eq!(
                core(service.delete(id, version, &env.context(), None)),
                gone
            );
        }
    }
    assert_eq!(rows(&env), before);
    // an id that is none never reaches the database
    for id in [
        "",
        "short",
        "../../etc/x",
        "ZZZZZZZZZZ",
        "zzzzzzzzz\n",
        "\u{e9}",
    ] {
        let gone = CoreError::MemoryNotFound(id.to_string());
        assert_eq!(service.read(id, &env.context(), "codex"), Err(gone.clone()));
        assert_eq!(
            core(service.update(id, 1, "b", None, &env.context(), None)),
            gone
        );
        assert_eq!(core(service.delete(id, 1, &env.context(), None)), gone);
    }
    // a context without a project reads global only and writes nowhere
    let nowhere = context_of(None, Some(&env.global_id));
    assert_eq!(
        core(service.record("b", "user", true, "codex", "", &nowhere)),
        CoreError::ProjectUnavailable {
            reason: String::new()
        }
    );
    // before anything else is checked: a harness that is no label is not looked at
    assert_eq!(
        core(service.record("b", "user", true, "not a label", "", &nowhere)),
        CoreError::ProjectUnavailable {
            reason: String::new()
        }
    );
    // a create that names a project the caller may not write
    let foreign = env.memory_store().write(
        &Op::create(&env.other.id, "user", "", "b"),
        &env.context().read_write_set,
        "mcp",
        None,
        &|_, _| None,
        &CAPS,
    );
    assert_eq!(
        core(foreign),
        CoreError::ProjectUnavailable {
            reason: String::new()
        }
    );
    let mine = env.record("mine");
    assert_eq!(
        service.read(&mine.id, &nowhere, "codex"),
        Err(CoreError::MemoryNotFound(mine.id.clone()))
    );
    assert_eq!(
        service.read(&mine.id, &context_of(None, None), "codex"),
        Err(CoreError::MemoryNotFound(mine.id.clone()))
    );
    // a project in the context that the store does not hold
    let unknown = context_of(Some("yyyyyyyyyy"), Some(&env.global_id));
    assert_eq!(
        core(service.record("b", "user", true, "codex", "", &unknown)),
        CoreError::ProjectUnavailable {
            reason: String::new()
        }
    );
    // a memory whose project row is gone is no one's
    env.raw()
        .execute_batch(&format!(
            "PRAGMA foreign_keys = OFF; DELETE FROM projects WHERE id = '{}';",
            env.project.id
        ))
        .unwrap();
    assert_eq!(
        service.read(&mine.id, &env.context(), "codex"),
        Err(CoreError::MemoryNotFound(mine.id))
    );
}

/// A row no version of memriver writes: its type is none.
fn damage(env: &Env, id: &str) {
    env.raw()
        .execute_batch(&format!(
            "PRAGMA ignore_check_constraints = ON; \
             UPDATE memories SET type = 'bogus' WHERE id = '{id}';"
        ))
        .unwrap();
}

#[test]
fn damaged_row_in_another_project_is_not_found_not_failure() {
    let env = setup();
    let service = env.services().memory;
    let theirs_context = context_of(Some(&env.other.id), Some(&env.global_id));
    let theirs = service
        .record("theirs", "user", true, "codex", "", &theirs_context)
        .unwrap();
    damage(&env, &theirs.id);
    let gone = CoreError::MemoryNotFound(theirs.id.clone());
    assert_eq!(
        service.read(&theirs.id, &env.context(), "codex"),
        Err(gone.clone())
    );
    assert_eq!(
        core(service.update(&theirs.id, 1, "b", None, &env.context(), None)),
        gone
    );
    // text that is not text at all
    env.raw()
        .execute(
            "UPDATE memories SET body = CAST(x'ff' AS TEXT) WHERE id = ?1",
            [&theirs.id],
        )
        .unwrap();
    assert_eq!(service.read(&theirs.id, &env.context(), "codex"), Err(gone));
    // to those who may read it, damage is damage
    assert_eq!(
        service.read(&theirs.id, &theirs_context, "codex"),
        Err(CoreError::StorageFailure)
    );
    assert_eq!(
        core(service.update(&theirs.id, 1, "b", None, &theirs_context, None)),
        CoreError::StorageFailure
    );
    // unless it is deleted: then it is gone for everyone, before a column is decoded
    let mine = env.record("mine");
    service.delete(&mine.id, 1, &env.context(), None).unwrap();
    damage(&env, &mine.id);
    assert_eq!(
        service.read(&mine.id, &env.context(), "codex"),
        Err(CoreError::MemoryNotFound(mine.id))
    );
}

#[test]
fn read_records_a_read_and_survives_a_failing_touch() {
    let env = setup();
    let memory = env.record("the body");
    let session = memriver_core::models::session::SessionKey::new(
        memriver_core::models::session::Harness::ClaudeCode,
        "a-session",
    )
    .unwrap();
    let context = ProjectContext {
        session_key: Some(session),
        ..env.context()
    };
    let service = env.services().memory;
    let read = service.read(&memory.id, &context, "claude-code").unwrap();
    // what is handed out is the row as it was before the read was recorded
    assert_eq!(read, memory);
    let fact: (String, i64, String, String, Option<String>) = env
        .raw()
        .query_row(
            "SELECT memory_id, memory_version, read_at, harness, session_id FROM memory_reads",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(
        (fact.0.as_str(), fact.1, fact.3.as_str(), fact.4.as_deref()),
        (memory.id.as_str(), 1, "claude-code", Some("a-session"))
    );
    assert!(is_timestamp(&fact.2));
    let row: (Option<String>, i64, String) = env
        .raw()
        .query_row(
            "SELECT last_read_at, version, updated FROM memories",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    // a read is no change: no version, no change row, the time of the last change kept
    assert_eq!(row, (Some(fact.2.clone()), 1, memory.updated.clone()));
    assert_eq!(rows(&env), [1, 1, 1, 1, 0]);
    // without a session, and the high-water mark never goes back
    let again = service.read(&memory.id, &env.context(), "codex").unwrap();
    assert_eq!(again.last_read_at, Some(fact.2.clone()));
    assert_eq!(env.count("memory_reads"), 2);
    let store = env.memory_store();
    store
        .touch_read(&memory.id, "2000-01-01T00:00:00.000000Z", 1, "codex", None)
        .unwrap();
    let last: Option<String> = env
        .raw()
        .query_row("SELECT last_read_at FROM memories", [], |row| row.get(0))
        .unwrap();
    assert!(last >= Some(fact.2));
    assert_eq!(env.count("memory_reads"), 3);
    // a harness the table refuses: the mark is set, the fact is not written, the read
    // is answered
    let read = service
        .read(&memory.id, &env.context(), &"h".repeat(65))
        .unwrap();
    assert_eq!(read.id, memory.id);
    assert_eq!(env.count("memory_reads"), 3);
    let newer: Option<String> = env
        .raw()
        .query_row("SELECT last_read_at FROM memories", [], |row| row.get(0))
        .unwrap();
    assert!(newer > last);
    // a time that is none, an id that is none: nothing happens
    store
        .touch_read(&memory.id, "yesterday", 1, "codex", None)
        .unwrap();
    store
        .touch_read("nope", "2030-01-01T00:00:00.000000Z", 1, "codex", None)
        .unwrap();
    assert_eq!(env.count("memory_reads"), 3);
    // the store cannot be written while the read is recorded: the read is answered
    let holder = env.raw();
    holder.execute_batch("BEGIN IMMEDIATE").unwrap();
    let mut settings = env.settings.clone();
    settings.root = env.store.clone();
    let impatient = MemoryService::new(
        memriver_core::repository::sqlite::memory_store::MemoryStore::with_busy_timeout(
            &env.store, 50,
        ),
        ProjectStore::with_busy_timeout(&env.store, &env.home, 50),
        policy_source(),
        limits(&settings),
        None,
        Box::new(|_| Ok(())),
        Box::new(|_| {}),
    );
    assert_eq!(
        impatient
            .read(&memory.id, &env.context(), "codex")
            .unwrap()
            .id,
        memory.id
    );
    let locked = memriver_core::repository::sqlite::memory_store::MemoryStore::with_busy_timeout(
        &env.store, 50,
    );
    assert_eq!(
        locked.touch_read(&memory.id, "2031-01-01T00:00:00.000000Z", 1, "codex", None),
        Ok(())
    );
    holder.execute_batch("ROLLBACK").unwrap();
    assert_eq!(env.count("memory_reads"), 3);
}

fn limits(settings: &Settings) -> MemoryLimits {
    MemoryLimits::of(settings)
}

/// The texts a classifier was asked about, each with who asked.
type Texts = Rc<RefCell<Vec<(String, String)>>>;

struct Asked {
    texts: Texts,
    verdict: Verdict,
    /// Checked while it is asked: nobody holds the store's write lock.
    database: Option<std::path::PathBuf>,
}

impl ContentClassifier for Asked {
    fn classify(&self, text: &str, changed_by: &str) -> Verdict {
        self.texts
            .borrow_mut()
            .push((text.to_string(), changed_by.to_string()));
        if let Some(database) = &self.database {
            let conn = rusqlite::Connection::open(database).unwrap();
            conn.busy_timeout(std::time::Duration::ZERO).unwrap();
            conn.execute_batch("BEGIN IMMEDIATE; ROLLBACK")
                .expect("the write lock is free while the classifier is asked");
        }
        self.verdict.clone()
    }
}

fn asked(env: &Env, verdict: Verdict) -> (MemoryService, Texts) {
    let texts = Rc::new(RefCell::new(Vec::new()));
    let classifier = Asked {
        texts: Rc::clone(&texts),
        verdict,
        database: Some(env.store.join(DATABASE_FILENAME)),
    };
    (env.classified(Box::new(classifier)).memory, texts)
}

#[test]
fn classifier_runs_before_the_transaction_and_blocks() {
    let env = setup();
    let memory = env.record("the body");
    let before = rows(&env);
    let blocked = Verdict::Blocked {
        category: "instruction".to_string(),
    };
    let (service, texts) = asked(&env, blocked);
    let refused = service.record("a body", "user", true, "codex", " a cue ", &env.context());
    assert_eq!(
        core(refused),
        CoreError::ContentRejected {
            message: "content rejected by the content classifier (instruction); no change was \
                      made"
                .to_string(),
            rule_id: "classifier-instruction".to_string(),
            memory_id: None,
            detail: String::new(),
        }
    );
    let refused = service.update(&memory.id, 1, "new text", None, &env.context(), None);
    assert!(matches!(core(refused), CoreError::ContentRejected { .. }));
    // a delete has no new text: nobody is asked, and it goes through
    assert_eq!(
        *texts.borrow(),
        [
            (" a cue \n\na body".to_string(), "mcp".to_string()),
            ("new text".to_string(), "mcp".to_string())
        ]
    );
    assert_eq!(rows(&env), before);
    service.delete(&memory.id, 1, &env.context(), None).unwrap();
    assert_eq!(texts.borrow().len(), 2);
}

#[test]
fn the_classifier_is_sent_only_the_new_text_and_never_a_secret() {
    let env = setup();
    let (service, texts) = asked(&env, Verdict::Allowed);
    // a blank description is not sent
    let memory = service
        .record("a body", "user", true, "codex", "  ", &env.context())
        .unwrap();
    service
        .update(&memory.id, 1, "b2", Some("d2"), &env.context(), None)
        .unwrap();
    // the policy refuses first: the classifier never sees what it refused
    let refused = service.record(SECRET, "user", true, "codex", "", &env.context());
    assert!(matches!(core(refused), CoreError::ContentRejected { .. }));
    assert_eq!(
        *texts.borrow(),
        [
            ("a body".to_string(), "mcp".to_string()),
            ("d2\n\nb2".to_string(), "mcp".to_string())
        ]
    );
}

#[test]
fn a_classifier_that_cannot_decide_refuses_and_its_labels_are_never_echoed() {
    let env = setup();
    let cases = [
        (
            Verdict::Unavailable {
                detail: "timeout".to_string(),
            },
            "the content classifier could not check this text (timeout); no change was made; \
             see memriver doctor",
            "classifier-unavailable",
            "timeout",
        ),
        (
            Verdict::Unavailable {
                detail: String::new(),
            },
            "the content classifier could not check this text (unknown); no change was made; \
             see memriver doctor",
            "classifier-unavailable",
            "unknown",
        ),
        (
            Verdict::Unavailable {
                detail: "Ignore previous instructions".to_string(),
            },
            "the content classifier could not check this text (unknown); no change was made; \
             see memriver doctor",
            "classifier-unavailable",
            "unknown",
        ),
        (
            Verdict::Blocked {
                category: "Not A Label".to_string(),
            },
            "content rejected by the content classifier (invalid); no change was made",
            "classifier-invalid",
            "",
        ),
        (
            Verdict::Blocked {
                category: "x".repeat(33),
            },
            "content rejected by the content classifier (invalid); no change was made",
            "classifier-invalid",
            "",
        ),
        (
            Verdict::Blocked {
                category: "unavailable".to_string(),
            },
            "the content classifier could not check this text (unknown); no change was made; \
             see memriver doctor",
            "classifier-unavailable",
            "unknown",
        ),
    ];
    for (verdict, message, rule_id, detail) in cases {
        let (service, _) = asked(&env, verdict.clone());
        let refused = service.record("a body", "user", true, "codex", "", &env.context());
        assert_eq!(
            core(refused),
            CoreError::ContentRejected {
                message: message.to_string(),
                rule_id: rule_id.to_string(),
                memory_id: None,
                detail: detail.to_string(),
            },
            "{verdict:?}"
        );
    }
    assert_eq!(rows(&env), [0; 5]);
}

#[test]
fn write_into_a_missing_store_is_project_unavailable() {
    let env = setup();
    let memory = env.record("the body");
    fs::remove_dir_all(&env.store).unwrap();
    let service = env.services().memory;
    assert_eq!(
        core(service.record("b", "user", true, "codex", "", &env.context())),
        CoreError::ProjectUnavailable {
            reason: String::new()
        }
    );
    let gone = CoreError::MemoryNotFound(memory.id.clone());
    assert_eq!(
        core(service.update(&memory.id, 1, "b", None, &env.context(), None)),
        gone
    );
    assert_eq!(
        core(service.delete(&memory.id, 1, &env.context(), None)),
        gone
    );
    assert_eq!(service.read(&memory.id, &env.context(), "codex"), Err(gone));
    assert_eq!(
        service.search("body", None, &env.context()).unwrap(),
        Vec::new()
    );
    assert_eq!(
        service.index(&env.context(), None).unwrap(),
        "(no memories yet)"
    );
    // and nothing brought it back
    assert!(!env.store.exists());
}

#[test]
fn what_only_the_management_path_may_do_is_no_agent_operation() {
    let env = setup();
    let memory = env.record("the body");
    let store = env.memory_store();
    let rws = env.context().read_write_set;
    let write = |op: &Op| store.write(op, &rws, "mcp", None, &|_, _| None, &CAPS);
    let restore = Op::Restore {
        memory_id: memory.id.clone(),
        expected_version: 1,
        to_version: 1,
    };
    assert_eq!(value(write(&restore)), "restore is not an agent operation");
    let refused = "citing sources or setting metadata is not an agent operation";
    let cited = vec![SourceRef {
        memory_id: memory.id.clone(),
        version: 1,
    }];
    let plain = || Op::create(&env.project.id, "user", "", "b");
    let mut creates = Vec::new();
    for change in 0..3 {
        let Op::Create {
            project_id,
            type_,
            description,
            body,
            trust,
            sync,
            ..
        } = plain()
        else {
            unreachable!()
        };
        creates.push(Op::Create {
            project_id,
            type_,
            description,
            body,
            trust,
            sync,
            sources: if change == 0 {
                cited.clone()
            } else {
                Vec::new()
            },
            level: (change == 1).then_some(2),
            tags: if change == 2 {
                vec!["a-tag".to_string()]
            } else {
                Vec::new()
            },
        });
    }
    let updates = [
        Op::Update {
            memory_id: memory.id.clone(),
            expected_version: 1,
            description: None,
            body: Some("b".to_string()),
            sources: Some(Vec::new()),
            level: None,
            tags: None,
        },
        Op::Update {
            memory_id: memory.id.clone(),
            expected_version: 1,
            description: None,
            body: None,
            sources: None,
            level: Some(1),
            tags: None,
        },
        Op::Update {
            memory_id: memory.id.clone(),
            expected_version: 1,
            description: None,
            body: None,
            sources: None,
            level: None,
            tags: Some(Vec::new()),
        },
    ];
    for op in creates.iter().chain(&updates) {
        assert_eq!(value(write(op)), refused, "{op:?}");
    }
    assert_eq!(rows(&env), [1, 1, 1, 1, 0]);
}

#[test]
fn a_pending_session_writes_nothing_and_a_save_marks_the_session() {
    let env = setup();
    let memory = env.record("the body");
    let marked = Rc::new(Cell::new(0));
    let service = |refuse: bool| {
        let marked = Rc::clone(&marked);
        MemoryService::new(
            env.memory_store(),
            ProjectStore::with_busy_timeout(&env.store, &env.home, 2000),
            policy_source(),
            limits(&env.settings),
            None,
            Box::new(move |context| {
                if refuse && context.state == ProjectContextState::Pending {
                    return Err(CoreError::ProjectUnavailable {
                        reason: "pending".to_string(),
                    });
                }
                Ok(())
            }),
            Box::new(move |_| marked.set(marked.get() + 1)),
        )
    };
    let pending = ProjectContext {
        state: ProjectContextState::Pending,
        ..env.context()
    };
    let refusing = service(true);
    let refused = CoreError::ProjectUnavailable {
        reason: "pending".to_string(),
    };
    // before anything else is looked at: a harness that is no label, a missing entry
    assert_eq!(
        core(refusing.record("b", "user", true, "not a label", "", &pending)),
        refused
    );
    assert_eq!(
        core(refusing.update("nope", 1, "b", None, &pending, None)),
        refused
    );
    assert_eq!(core(refusing.delete("nope", 1, &pending, None)), refused);
    assert_eq!(marked.get(), 0);
    // a write and an update mark the session, an update that changes nothing too; a
    // delete does not, and neither does anything refused
    let saving = service(false);
    saving
        .record("b", "user", true, "codex", "", &env.context())
        .unwrap();
    assert_eq!(marked.get(), 1);
    saving
        .update(&memory.id, 1, "the body", None, &env.context(), None)
        .unwrap();
    assert_eq!(marked.get(), 2);
    saving
        .update(&memory.id, 1, "another", None, &env.context(), None)
        .unwrap();
    assert_eq!(marked.get(), 3);
    assert!(
        saving
            .update(&memory.id, 1, "stale", None, &env.context(), None)
            .is_err()
    );
    saving.delete(&memory.id, 2, &env.context(), None).unwrap();
    assert_eq!(marked.get(), 3);
}

#[test]
fn records_from_many_writers_at_once_all_land() {
    let env = setup();
    let writers: Vec<_> = (0..20)
        .map(|n| {
            let (settings, home, context) = (env.settings.clone(), env.home.clone(), env.context());
            thread::spawn(move || {
                memriver_core::build_services(&settings, &home, None)
                    .memory
                    .record(&format!("fact {n}"), "project", true, "codex", "", &context)
                    .map(|memory| memory.id)
            })
        })
        .collect();
    let ids: std::collections::BTreeSet<String> = writers
        .into_iter()
        .map(|writer| writer.join().unwrap().unwrap())
        .collect();
    assert_eq!(ids.len(), 20);
    let mut bodies: Vec<String> = env
        .services()
        .memory
        .search("fact", Some(50), &env.context())
        .unwrap()
        .into_iter()
        .map(|memory| memory.body)
        .collect();
    bodies.sort();
    let mut expected: Vec<String> = (0..20).map(|n| format!("fact {n}")).collect();
    expected.sort();
    assert_eq!(bodies, expected);
    assert_eq!(rows(&env), [20, 20, 20, 20, 0]);
    let integrity: String = env
        .raw()
        .query_row("PRAGMA integrity_check", [], |row| row.get(0))
        .unwrap();
    assert_eq!(integrity, "ok");
}
