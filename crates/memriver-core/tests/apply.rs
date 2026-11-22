//! The management path, through the services: a batch of operations written whole or
//! not at all as one change -- by a human command or by dream, never by an agent.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use common::{
    CAPS, Env, SECRET, SECRET_RULE, body, citing, conflict, core, described, draft, judged,
    rejected, restore, setup, unread, update, value,
};
use memriver_core::application::memory::{MemoryLimits, MemoryService, policy_source};
use memriver_core::models::changes::{Change, Confirmation, Op, OpName};
use memriver_core::models::errors::{BatchConflictReason as Reason, CoreError, WriteError};
use memriver_core::models::helpers::now;
use memriver_core::repository::sqlite::project_store::ProjectStore;

const TABLES: [&str; 6] = [
    "memories",
    "memory_versions",
    "memory_sources",
    "changes",
    "change_steps",
    "memory_reads",
];

fn rows(env: &Env) -> [i64; 6] {
    TABLES.map(|table| env.count(table))
}

fn human(env: &Env, ops: &[Op]) -> Result<Change, WriteError> {
    env.services().memory.apply(ops, "human", None)
}

/// A memory of the bound project, made by a human: its id.
fn fact(env: &Env, text: &str) -> String {
    env.made(draft(&env.project.id, text).op())
}

/// The version, the body and whether it is deleted, as stored.
fn state(env: &Env, memory_id: &str) -> (i64, String, bool) {
    env.raw()
        .query_row(
            "SELECT version, body, deleted_at IS NOT NULL FROM memories WHERE id = ?1",
            [memory_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap()
}

fn sources(env: &Env, memory_id: &str, version: i64) -> Vec<(String, i64)> {
    let conn = env.raw();
    let mut statement = conn
        .prepare(
            "SELECT source_id, source_version FROM memory_sources \
             WHERE memory_id = ?1 AND version = ?2 ORDER BY source_id",
        )
        .unwrap();
    let rows = statement
        .query_map((memory_id, version), |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap();
    rows.map(Result::unwrap).collect()
}

fn pairs(cited: &[(&str, i64)]) -> Vec<(String, i64)> {
    let mut pairs: Vec<(String, i64)> = cited
        .iter()
        .map(|(memory_id, version)| (memory_id.to_string(), *version))
        .collect();
    pairs.sort();
    pairs
}

fn trust_sync(env: &Env, memory_id: &str) -> (String, bool) {
    env.raw()
        .query_row(
            "SELECT trust, sync FROM memories WHERE id = ?1",
            [memory_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap()
}

/// Each version of a memory with the change and the step that own it.
type Owner = (i64, bool, String, Option<String>, String, Option<i64>);

fn owners(env: &Env, memory_id: &str) -> Vec<Owner> {
    let conn = env.raw();
    let mut statement = conn
        .prepare(
            "SELECT v.version, v.deleted, c.changed_by, c.changed_via, s.op, s.before_version \
             FROM memory_versions v JOIN changes c ON c.change_id = v.change_id \
             JOIN change_steps s ON s.change_id = c.change_id AND s.memory_id = v.memory_id \
             AND s.after_version = v.version WHERE v.memory_id = ?1 ORDER BY v.version",
        )
        .unwrap();
    let rows = statement
        .query_map([memory_id], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
            ))
        })
        .unwrap();
    rows.map(Result::unwrap).collect()
}

fn owner(
    version: i64,
    deleted: bool,
    by: &str,
    via: Option<&str>,
    op: &str,
    before: Option<i64>,
) -> Owner {
    (
        version,
        deleted,
        by.to_string(),
        via.map(str::to_string),
        op.to_string(),
        before,
    )
}

/// Whether the memory's row is what its latest version records.
fn row_is_latest_version(env: &Env, memory_id: &str) -> bool {
    env.raw()
        .query_row(
            "SELECT count(*) FROM memories m JOIN memory_versions v \
             ON v.memory_id = m.id AND v.version = m.version AND v.type = m.type \
             AND v.trust = m.trust AND v.sync = m.sync AND v.description = m.description \
             AND v.body = m.body AND v.deleted = (m.deleted_at IS NOT NULL) WHERE m.id = ?1",
            [memory_id],
            |row| row.get::<_, i64>(0),
        )
        .unwrap()
        == 1
}

#[test]
fn batch_is_all_or_nothing() {
    let env = setup();
    let (first, second) = (fact(&env, "first"), fact(&env, "second"));
    let services = env.services();
    let change = services
        .memory
        .apply(
            &[
                draft(&env.project.id, "merged").op(),
                body(&first, 1, "changed"),
                Op::soft_delete(&second, 1),
            ],
            "human",
            Some("cli"),
        )
        .unwrap();
    assert_eq!(
        (
            change.step_count,
            change.changed_by.as_str(),
            change.changed_via.as_deref(),
            &change.undoes
        ),
        (3, "human", Some("cli"), &None)
    );
    let steps: Vec<_> = change
        .steps
        .iter()
        .map(|step| (step.step, step.op, step.before_version, step.after_version))
        .collect();
    assert_eq!(
        steps,
        [
            (1, OpName::Create, None, 1),
            (2, OpName::Update, Some(1), 2),
            (3, OpName::SoftDelete, Some(1), 2)
        ]
    );
    assert_eq!(change.steps[1].memory_id, first);
    let stored: (i64, i64, String) = env
        .raw()
        .query_row(
            "SELECT step_count, (SELECT count(*) FROM change_steps s \
             WHERE s.change_id = c.change_id), at FROM changes c WHERE change_id = ?1",
            [&change.change_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(stored, (3, 3, change.at.clone()));
    assert_eq!(state(&env, &first), (2, "changed".to_string(), false));
    assert_eq!(state(&env, &second), (2, "second".to_string(), true));
    // what is no change at all writes nothing either
    let before = rows(&env);
    assert_eq!(
        value(human(
            &env,
            &[body(&first, 2, "x"), Op::soft_delete(&first, 2)]
        )),
        "a change touches a memory at most once"
    );
    assert_eq!(
        value(human(&env, &[])),
        "a change needs at least one operation"
    );
    let delete = [Op::soft_delete(&first, 2)];
    assert_eq!(
        value(services.memory.apply(&delete, &"x".repeat(33), None)),
        "changed_by is not 1 to 32 characters"
    );
    for via in [String::new(), "v".repeat(65)] {
        assert_eq!(
            value(services.memory.apply(&delete, "human", Some(&via))),
            "changed_via is not 1 to 64 characters"
        );
    }
    assert_eq!(rows(&env), before);
}

#[test]
fn a_batch_failing_at_step_three_leaves_steps_one_and_two_unwritten() {
    let env = setup();
    let (first, second) = (fact(&env, "first"), fact(&env, "second"));
    let before = rows(&env);
    let refused = human(
        &env,
        &[
            draft(&env.project.id, "new").op(),
            body(&first, 1, "changed"),
            Op::soft_delete(&second, 7),
        ],
    );
    assert_eq!(
        conflict(refused),
        (2, Some(second.clone()), Reason::Version)
    );
    assert_eq!(rows(&env), before);
    assert_eq!(state(&env, &first), (1, "first".to_string(), false));
    assert_eq!(state(&env, &second), (1, "second".to_string(), false));
}

#[test]
fn same_state_update_is_a_conflict_here() {
    let env = setup();
    let memory_id = env.made(Op::create(&env.project.id, "project", "cue", "a fact"));
    let before = rows(&env);
    let mut both = described(&memory_id, 1, "cue");
    if let Op::Update { body, .. } = &mut both {
        *body = Some("a fact".to_string());
    }
    for op in [body(&memory_id, 1, " a fact "), both, update(&memory_id, 1)] {
        assert_eq!(
            conflict(human(&env, &[op])),
            (0, Some(memory_id.clone()), Reason::SameState)
        );
    }
    assert_eq!(rows(&env), before);
    // the same text from an agent is no error: it is told what is there
    let same = env
        .services()
        .memory
        .update(&memory_id, 1, " a fact ", None, &env.context(), None)
        .unwrap();
    assert_eq!((same.version, same.body.as_str()), (1, "a fact"));
    assert_eq!(rows(&env), before);
}

#[test]
fn a_target_that_is_missing_deleted_or_at_another_version_is_refused() {
    let env = setup();
    let memory_id = fact(&env, "a fact");
    for op in [body(&memory_id, 2, "x"), Op::soft_delete(&memory_id, 2)] {
        assert_eq!(
            conflict(human(&env, &[op])),
            (0, Some(memory_id.clone()), Reason::Version)
        );
    }
    human(&env, &[Op::soft_delete(&memory_id, 1)]).unwrap();
    let before = rows(&env);
    for op in [body(&memory_id, 2, "x"), Op::soft_delete(&memory_id, 2)] {
        assert_eq!(
            conflict(human(&env, &[op])),
            (0, Some(memory_id.clone()), Reason::Deleted)
        );
    }
    for missing in ["zzzzzzzzzz", "no id at all"] {
        assert_eq!(
            conflict(human(&env, &[body(missing, 1, "x")])),
            (0, Some(missing.to_string()), Reason::Missing)
        );
    }
    assert_eq!(
        core(human(&env, &[draft("zzzzzzzzzz", "b").op()])),
        CoreError::ProjectNotFound("zzzzzzzzzz".to_string())
    );
    assert_eq!(rows(&env), before);
}

#[test]
fn source_must_be_a_current_live_version_before_the_batch() {
    let env = setup();
    let project = &env.project.id;
    // version 2 exists only after the batch's own first operation
    let first = fact(&env, "first");
    let before = rows(&env);
    let refused = human(
        &env,
        &[
            Op::soft_delete(&first, 1),
            draft(project, "x").citing(&[(&first, 2)]).op(),
        ],
    );
    assert_eq!(conflict(refused), (1, None, Reason::Source));
    assert_eq!(rows(&env), before);
    // a merge may delete what it cites in the same batch, before or after it cites
    for originals_first in [false, true] {
        let (alpha, beta) = (fact(&env, "alpha"), fact(&env, "beta"));
        let mut ops = vec![
            draft(project, "merged")
                .citing(&[(&alpha, 1), (&beta, 1)])
                .op(),
            Op::soft_delete(&alpha, 1),
            Op::soft_delete(&beta, 1),
        ];
        if originals_first {
            ops.rotate_left(1);
        }
        let change = human(&env, &ops).unwrap();
        let created = change
            .steps
            .iter()
            .find(|step| step.op == OpName::Create)
            .unwrap();
        assert_eq!(
            sources(&env, &created.memory_id, 1),
            pairs(&[(&alpha, 1), (&beta, 1)])
        );
        assert_eq!(state(&env, &alpha), (2, "alpha".to_string(), true));
        assert_eq!(state(&env, &beta), (2, "beta".to_string(), true));
    }
}

#[test]
fn an_already_cited_source_is_carried_even_after_it_was_deleted() {
    let env = setup();
    let (alpha, beta) = (fact(&env, "alpha"), fact(&env, "beta"));
    let merged = env.made(
        draft(&env.project.id, "the merged fact")
            .citing(&[(&alpha, 1), (&beta, 1)])
            .op(),
    );
    let both = pairs(&[(&alpha, 1), (&beta, 1)]);
    // neither cited version is current any more, and one source is deleted
    human(&env, &[Op::soft_delete(&alpha, 1)]).unwrap();
    human(&env, &[body(&beta, 1, "beta, later")]).unwrap();
    // an edit keeps citing them
    human(&env, &[body(&merged, 1, "the merged fact, edited")]).unwrap();
    assert_eq!(sources(&env, &merged, 2), both);
    // while the set changes: beta at 1 stays, alpha at 1 is dropped
    human(&env, &[citing(&merged, 2, &[(&beta, 1)])]).unwrap();
    assert_eq!(sources(&env, &merged, 3), pairs(&[(&beta, 1)]));
    // a restore puts version 1's set back as it was
    human(&env, &[restore(&merged, 3, 1)]).unwrap();
    assert_eq!(sources(&env, &merged, 4), both);
    // and a delete carries it to the deleted version
    human(&env, &[Op::soft_delete(&merged, 4)]).unwrap();
    assert_eq!(sources(&env, &merged, 5), both);
}

#[test]
fn a_newly_cited_source_must_be_current_and_live() {
    let env = setup();
    let project = &env.project.id;
    let alpha = fact(&env, "alpha");
    human(&env, &[body(&alpha, 1, "alpha, later")]).unwrap();
    let other = fact(&env, "other");
    // an older version, and one that never was: by a create and by an update
    assert_eq!(
        conflict(human(
            &env,
            &[draft(project, "again").citing(&[(&alpha, 1)]).op()]
        )),
        (0, None, Reason::Source)
    );
    for version in [1, 3] {
        assert_eq!(
            conflict(human(&env, &[citing(&other, 1, &[(&alpha, version)])])),
            (0, Some(other.clone()), Reason::Source)
        );
    }
    assert_eq!(
        conflict(human(&env, &[citing(&other, 1, &[("zzzzzzzzzz", 1)])])).2,
        Reason::Source
    );
    // the current version: a version of its own, with the text kept
    let change = human(&env, &[citing(&other, 1, &[(&alpha, 2)])]).unwrap();
    assert_eq!(change.steps[0].after_version, 2);
    assert_eq!(state(&env, &other), (2, "other".to_string(), false));
    assert_eq!(sources(&env, &other, 2), pairs(&[(&alpha, 2)]));
    // the same set again changes nothing
    assert_eq!(
        conflict(human(&env, &[citing(&other, 2, &[(&alpha, 2)])])).2,
        Reason::SameState
    );
    // a deleted memory is not newly cited, at its current version either
    human(&env, &[Op::soft_delete(&alpha, 2)]).unwrap();
    let another = fact(&env, "another");
    assert_eq!(
        conflict(human(&env, &[citing(&another, 1, &[(&alpha, 3)])])).2,
        Reason::Source
    );
    assert_eq!(
        value(human(
            &env,
            &[draft(project, "x").citing(&[(&other, 2), (&other, 2)]).op()]
        )),
        "a source set cites a memory at most once"
    );
}

#[test]
fn source_cycle_is_refused() {
    let env = setup();
    let alpha = fact(&env, "alpha");
    assert_eq!(
        conflict(human(&env, &[citing(&alpha, 1, &[(&alpha, 1)])])),
        (0, Some(alpha.clone()), Reason::Cycle)
    );
    let (gamma, delta) = (fact(&env, "gamma"), fact(&env, "delta"));
    let before = rows(&env);
    let within_the_batch = human(
        &env,
        &[
            citing(&gamma, 1, &[(&delta, 1)]),
            citing(&delta, 1, &[(&gamma, 1)]),
        ],
    );
    assert_eq!(conflict(within_the_batch).2, Reason::Cycle);
    assert_eq!(rows(&env), before);
}

#[test]
fn a_history_cycle_through_older_versions_is_refused() {
    let env = setup();
    let alpha = fact(&env, "alpha");
    let beta = env.made(draft(&env.project.id, "beta").citing(&[(&alpha, 1)]).op());
    // beta's current version cites nothing; its first still cites alpha
    human(&env, &[citing(&beta, 1, &[])]).unwrap();
    let before = rows(&env);
    assert_eq!(
        conflict(human(&env, &[citing(&alpha, 1, &[(&beta, 2)])])),
        (0, Some(alpha.clone()), Reason::Cycle)
    );
    assert_eq!(rows(&env), before);
}

#[test]
fn derived_trust_is_the_lowest_of_its_sources() {
    let env = setup();
    let project = &env.project.id;
    let stated = env.made(
        draft(project, "stated by the user")
            .trusted("user", true)
            .op(),
    );
    let agreed = env.made(draft(project, "an agent's").op());
    let fetched = env.made(
        draft(project, "read on a web page")
            .trusted("untrusted-derived", false)
            .op(),
    );
    for (cited, derived) in [
        (vec![(stated.as_str(), 1)], ("user", true)),
        (
            vec![(stated.as_str(), 1), (agreed.as_str(), 1)],
            ("agent", true),
        ),
        (
            vec![
                (stated.as_str(), 1),
                (agreed.as_str(), 1),
                (fetched.as_str(), 1),
            ],
            ("untrusted-derived", false),
        ),
    ] {
        let memory_id = env.made(draft(project, "derived").citing(&cited).op());
        assert_eq!(
            trust_sync(&env, &memory_id),
            (derived.0.to_string(), derived.1),
            "{cited:?}"
        );
    }
}

#[test]
fn create_with_sources_ignores_the_given_trust_and_sync() {
    let env = setup();
    let project = &env.project.id;
    let stated = env.made(
        draft(project, "stated by the user")
            .trusted("user", true)
            .op(),
    );
    let fetched = env.made(
        draft(project, "read on a web page")
            .trusted("untrusted-derived", false)
            .op(),
    );
    let raised = draft(project, "both")
        .citing(&[(&stated, 1), (&fetched, 1)])
        .trusted("user", true);
    assert_eq!(
        trust_sync(&env, &env.made(raised.op())),
        ("untrusted-derived".to_string(), false)
    );
    let lowered = draft(project, "one")
        .citing(&[(&stated, 1)])
        .trusted("untrusted-derived", false);
    assert_eq!(
        trust_sync(&env, &env.made(lowered.op())),
        ("user".to_string(), true)
    );
    // a trust that is none is not even looked at
    let unknown = draft(project, "one more")
        .citing(&[(&stated, 1)])
        .trusted("nobody", true);
    assert_eq!(
        trust_sync(&env, &env.made(unknown.op())),
        ("user".to_string(), true)
    );
}

#[test]
fn update_trust_can_only_go_down() {
    let env = setup();
    let project = &env.project.id;
    let stated = env.made(
        draft(project, "stated by the user")
            .trusted("user", true)
            .op(),
    );
    let fetched = env.made(
        draft(project, "read on a web page")
            .trusted("untrusted-derived", false)
            .op(),
    );
    let plain = fact(&env, "plain");
    // never above the state before
    human(&env, &[citing(&plain, 1, &[(&stated, 1)])]).unwrap();
    assert_eq!(trust_sync(&env, &plain), ("agent".to_string(), true));
    human(&env, &[citing(&plain, 2, &[(&stated, 1), (&fetched, 1)])]).unwrap();
    assert_eq!(
        trust_sync(&env, &plain),
        ("untrusted-derived".to_string(), false)
    );
    // and not back up when the low source is dropped, or all of them
    human(&env, &[citing(&plain, 3, &[(&stated, 1)])]).unwrap();
    human(&env, &[citing(&plain, 4, &[])]).unwrap();
    assert_eq!(
        trust_sync(&env, &plain),
        ("untrusted-derived".to_string(), false)
    );
    // only a restore takes a version's own recorded values
    human(&env, &[restore(&plain, 5, 1)]).unwrap();
    assert_eq!(trust_sync(&env, &plain), ("agent".to_string(), true));
    let recorded: (String, bool) = env
        .raw()
        .query_row(
            "SELECT trust, sync FROM memory_versions WHERE memory_id = ?1 AND version = 6",
            [&plain],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(recorded, ("agent".to_string(), true));
}

#[test]
fn update_level_none_keeps_and_tags_empty_clears() {
    let env = setup();
    let rule = env.made(
        draft(&env.global_id, "a rule")
            .judged(Some(1), &["python", "git"])
            .op(),
    );
    let metadata = || -> (i64, Option<i64>, String) {
        env.raw()
            .query_row(
                "SELECT version, level, tags FROM memories WHERE id = ?1",
                [&rule],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap()
    };
    assert_eq!(metadata(), (1, Some(1), r#"["git","python"]"#.to_string()));
    human(&env, &[body(&rule, 1, "a rule, reworded")]).unwrap();
    assert_eq!(metadata(), (2, Some(1), r#"["git","python"]"#.to_string()));
    human(&env, &[judged(&rule, 2, None, Some(&[]))]).unwrap();
    assert_eq!(metadata(), (3, Some(1), "[]".to_string()));
    human(&env, &[judged(&rule, 3, Some(2), None)]).unwrap();
    assert_eq!(metadata(), (4, Some(2), "[]".to_string()));
    assert_eq!(
        state(&env, &rule),
        (4, "a rule, reworded".to_string(), false)
    );
}

#[test]
fn soft_delete_of_text_failing_a_new_rule_is_refused() {
    let env = setup();
    let memory = env.record("clean when it was written");
    let conn = env.raw();
    conn.execute("UPDATE memories SET body = ?1", [SECRET])
        .unwrap();
    conn.execute("UPDATE memory_versions SET body = ?1", [SECRET])
        .unwrap();
    let before = rows(&env);
    let by_apply = rejected(human(&env, &[Op::soft_delete(&memory.id, 1)]));
    let by_agent =
        rejected(
            env.services()
                .memory
                .delete(&memory.id, 1, &env.context(), "mcp", None),
        );
    assert_eq!(
        by_apply,
        (
            String::new(),
            SECRET_RULE.to_string(),
            Some(memory.id.clone())
        )
    );
    assert_eq!(by_agent, by_apply);
    assert_eq!(rows(&env), before);
}

#[test]
fn global_is_writable_here() {
    let env = setup();
    let rule = env.made(draft(&env.global_id, "a principle").op());
    let change = human(&env, &[body(&rule, 1, "a sharper principle")]).unwrap();
    assert_eq!(change.steps[0].after_version, 2);
    let agents =
        env.services()
            .memory
            .update(&rule, 2, "an agent's edit", None, &env.context(), None);
    assert_eq!(core(agents), CoreError::GlobalReadOnly);
    let updated = |memory_id: &str| -> (String, Option<String>) {
        env.raw()
            .query_row(
                "SELECT updated, deleted_at FROM memories WHERE id = ?1",
                [memory_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap()
    };
    let before = updated(&rule).0;
    human(&env, &[Op::soft_delete(&rule, 2)]).unwrap();
    assert_eq!(
        owners(&env, &rule).last().unwrap(),
        &owner(3, true, "human", None, "soft_delete", Some(2))
    );
    // a delete moves the time like every change of state
    let (after, deleted_at) = updated(&rule);
    assert!(after > before);
    assert_eq!(deleted_at, Some(after));
}

#[test]
fn soft_delete_with_unread_since_is_refused_when_read_at_or_after_it() {
    let env = setup();
    let (read, never_read) = (fact(&env, "read"), fact(&env, "never read"));
    env.raw()
        .execute(
            "UPDATE memories SET last_read_at = '2026-09-27T10:00:00.000000Z' WHERE id = ?1",
            [&read],
        )
        .unwrap();
    let before = rows(&env);
    // read at that very instant, and one microsecond after it
    for since in ["2026-09-27T10:00:00.000000Z", "2026-09-27T09:59:59.999999Z"] {
        assert_eq!(
            conflict(human(&env, &[unread(&read, 1, since)])),
            (0, Some(read.clone()), Reason::ReadSince),
            "{since}"
        );
    }
    assert_eq!(rows(&env), before);
    // read one microsecond before it, and never
    let change = human(&env, &[unread(&read, 1, "2026-09-27T10:00:00.000001Z")]).unwrap();
    assert_eq!(change.steps[0].after_version, 2);
    human(
        &env,
        &[unread(&never_read, 1, "2026-09-27T10:00:00.000000Z")],
    )
    .unwrap();
    assert!(state(&env, &read).2 && state(&env, &never_read).2);
}

#[test]
fn unread_since_that_is_not_a_timestamp_is_an_error() {
    let env = setup();
    let memory_id = fact(&env, "a fact");
    let before = rows(&env);
    for since in [
        "yesterday",
        "",
        "2026-09-27 10:00:00",
        "2026-09-27T10:00:00Z",
    ] {
        assert_eq!(
            value(human(&env, &[unread(&memory_id, 1, since)])),
            "unread_since is not a timestamp",
            "{since:?}"
        );
    }
    // the memory is found first: one that is not there at that version is that answer
    assert_eq!(
        conflict(human(&env, &[unread(&memory_id, 7, "yesterday")])).2,
        Reason::Version
    );
    assert_eq!(rows(&env), before);
    assert!(!state(&env, &memory_id).2);
}

#[test]
fn read_since_is_checked_inside_the_write_transaction() {
    let env = setup();
    let services = env.services();
    let memory_id = fact(&env, "a fact");
    // whoever plans the delete lists the memory now; a session reads it before the
    // delete is applied
    let listed = now();
    services
        .memory
        .read(&memory_id, &env.context(), "codex")
        .unwrap();
    assert_eq!(
        conflict(human(&env, &[unread(&memory_id, 1, &listed)])),
        (0, Some(memory_id.clone()), Reason::ReadSince)
    );
    assert!(!state(&env, &memory_id).2);
}

#[test]
fn deferred_source_key_violation_fails_the_commit_and_rolls_back() {
    let env = setup();
    let (source, derived) = (fact(&env, "source"), fact(&env, "derived"));
    // a citation of a version that never was, planted behind the store's back
    env.unkeyed()
        .execute(
            "INSERT INTO memory_sources (memory_id, version, source_id, source_version) \
             VALUES (?1, 1, ?2, 9)",
            [&derived, &source],
        )
        .unwrap();
    let before = rows(&env);
    // an update looks the cited version up for its trust, and misses it
    assert_eq!(
        conflict(human(&env, &[body(&derived, 1, "x")])),
        (0, Some(derived.clone()), Reason::Source)
    );
    // a delete carries the set as it is: only the commit finds the key broken
    assert_eq!(
        core(human(&env, &[Op::soft_delete(&derived, 1)])),
        CoreError::StorageFailure
    );
    assert_eq!(rows(&env), before);
    assert_eq!(state(&env, &derived), (1, "derived".to_string(), false));
}

#[test]
fn the_policy_refuses_in_the_same_words_with_or_without_a_classifier() {
    let env = setup();
    let memory_id = fact(&env, "a fact");
    let (classified, asked) = env.noted();
    let before = rows(&env);
    for services in [env.services(), classified] {
        let refused = |op: Op| rejected(services.memory.apply(&[op], "human", None));
        let rule = |rule_id: &str, memory_id: Option<&String>| {
            (String::new(), rule_id.to_string(), memory_id.cloned())
        };
        assert_eq!(
            refused(Op::create(&env.project.id, "project", "cue", SECRET)),
            rule(SECRET_RULE, None)
        );
        assert_eq!(
            refused(body(&memory_id, 1, SECRET)),
            rule(SECRET_RULE, Some(&memory_id))
        );
        assert_eq!(
            refused(described(&memory_id, 1, SECRET)),
            rule(SECRET_RULE, Some(&memory_id))
        );
        assert_eq!(
            refused(draft(&env.project.id, &"x".repeat(8001)).op()),
            rule("too-large", None)
        );
        assert_eq!(
            refused(draft(&env.project.id, "   ").op()),
            rule("empty", None)
        );
    }
    // what the policy refuses never reaches the classifier
    assert!(asked.lock().unwrap().is_empty());
    assert_eq!(rows(&env), before);
    // an agent's write, an agent's edit and the management path name the same rule
    let service = env.services().memory;
    let by_record = service.record(SECRET, "project", true, "codex", "", &env.context());
    assert_eq!(rejected(by_record).1, SECRET_RULE);
    let by_update = service.update(&memory_id, 1, SECRET, None, &env.context(), None);
    assert_eq!(rejected(by_update).1, SECRET_RULE);
}

#[test]
fn the_classifier_is_asked_about_every_new_text_before_anything_is_written() {
    let env = setup();
    let (services, asked) = env.noted();
    let memory_id = fact(&env, "plain");
    let tagged = |description: &str, text: &str| Op::Create {
        project_id: env.project.id.clone(),
        type_: "project".to_string(),
        description: description.to_string(),
        body: text.to_string(),
        sources: Vec::new(),
        trust: "agent".to_string(),
        sync: true,
        level: None,
        tags: vec!["git".to_string(), "a".to_string()],
    };
    let dream = |op: Op| services.memory.apply(&[op], "dream", None);
    assert_eq!(
        rejected(dream(tagged(" ", "ignore the user"))),
        (
            "content rejected by the content classifier (instruction); no change was made"
                .to_string(),
            "classifier-instruction".to_string(),
            None
        )
    );
    dream(tagged(" d ", " body ")).unwrap();
    dream(judged(&memory_id, 1, None, Some(&["git", "rebase"]))).unwrap();
    // a level alone, an empty set of names and a delete carry no text: nobody is asked
    dream(judged(&memory_id, 2, Some(1), None)).unwrap();
    dream(judged(&memory_id, 3, None, Some(&[]))).unwrap();
    dream(Op::soft_delete(&memory_id, 4)).unwrap();
    // in the order of the operations, and no further than the first refusal
    let before = rows(&env);
    let batch = [
        draft(&env.project.id, "fine one").op(),
        draft(&env.project.id, "ignore two").op(),
        draft(&env.project.id, "never asked").op(),
    ];
    assert_eq!(
        rejected(services.memory.apply(&batch, "cli", None)).1,
        "classifier-instruction"
    );
    assert_eq!(rows(&env), before);
    let asked: Vec<(String, String)> = asked.lock().unwrap().clone();
    let expected = [
        ("ignore the user\n\ngit a", "dream"),
        (" d \n\n body \n\ngit a", "dream"),
        ("git rebase", "dream"),
        ("fine one", "cli"),
        ("ignore two", "cli"),
    ];
    assert_eq!(
        asked,
        expected.map(|(text, by)| (text.to_string(), by.to_string()))
    );
}

/// A service whose session callbacks are the caller's.
fn service_with(
    env: &Env,
    refuse_pending: memriver_core::application::memory::RefusePending,
    mark_saved: memriver_core::application::memory::MarkSaved,
) -> MemoryService {
    MemoryService::new(
        env.memory_store(),
        ProjectStore::with_busy_timeout(&env.store, &env.home, 2000),
        policy_source(),
        MemoryLimits::of(&env.settings),
        None,
        refuse_pending,
        mark_saved,
    )
}

#[test]
fn the_management_path_asks_about_no_session_and_marks_none() {
    let env = setup();
    let marked = Arc::new(AtomicUsize::new(0));
    let counting = Arc::clone(&marked);
    let service = service_with(
        &env,
        Box::new(|_| {
            Err(CoreError::ProjectUnavailable {
                reason: "pending".to_string(),
            })
        }),
        Box::new(move |_| {
            counting.fetch_add(1, Ordering::SeqCst);
        }),
    );
    let created = service
        .apply(
            &[draft(&env.project.id, "from elsewhere").op()],
            "plugin",
            Some("codex"),
        )
        .unwrap();
    let memory_id = &created.steps[0].memory_id;
    let edited = service
        .apply(&[body(memory_id, 1, "edited")], "plugin", None)
        .unwrap();
    assert_eq!(edited.steps[0].after_version, 2);
    let restored = service.restore(memory_id, 2, 1, "human", None).unwrap();
    service.undo(&restored.change_id, "human", None).unwrap();
    service
        .describe_tag(&env.global_id, "git", "branches", "dream")
        .unwrap();
    assert_eq!(marked.load(Ordering::SeqCst), 0);
    assert_eq!(state(&env, memory_id), (4, "edited".to_string(), false));
}

#[test]
fn a_peer_write_after_the_commit_does_not_change_what_a_write_returns() {
    let env = setup();
    // the session is marked after the write's own transaction is committed and before
    // the write returns: this is where a peer gets in
    type Peer = Arc<Mutex<Option<Box<dyn FnOnce() + Send>>>>;
    let peer: Peer = Arc::default();
    let waiting = Arc::clone(&peer);
    let service = service_with(
        &env,
        Box::new(|_| Ok(())),
        Box::new(move |_| {
            if let Some(write) = waiting.lock().unwrap().take() {
                write();
            }
        }),
    );
    let then = |write: Box<dyn FnOnce() + Send>| *peer.lock().unwrap() = Some(write);
    let peers = |ops: Vec<Op>| -> Box<dyn FnOnce() + Send> {
        let store = env.memory_store();
        Box::new(move || {
            store
                .apply(&ops, "human", None, &|_, _| None, &CAPS)
                .unwrap();
        })
    };
    let context = env.context();
    let record = |text: &str| {
        service
            .record(text, "project", true, "codex", "", &context)
            .unwrap()
    };
    let written = record("first words");
    // a peer's update
    then(peers(vec![body(&written.id, 2, "a peer's words")]));
    let mine = service
        .update(&written.id, 1, "my words", None, &context, None)
        .unwrap();
    assert_eq!(
        (mine.version, mine.body.as_str(), &mine.deleted_at),
        (2, "my words", &None)
    );
    assert_eq!(state(&env, &written.id).0, 3);
    // a peer's delete
    then(peers(vec![Op::soft_delete(&written.id, 4)]));
    let mine = service
        .update(&written.id, 3, "mine again", None, &context, None)
        .unwrap();
    assert_eq!(
        (mine.version, mine.body.as_str(), &mine.deleted_at),
        (4, "mine again", &None)
    );
    assert!(state(&env, &written.id).2);
    // a peer that removes the memory with all its history
    let doomed = record("to be removed");
    let (store, doomed_id) = (env.memory_store(), doomed.id.clone());
    then(Box::new(move || {
        let plan = store.plan_hard_delete(&doomed_id).unwrap();
        store
            .hard_delete(&doomed_id, Confirmation::Code(&plan.code))
            .unwrap();
    }));
    let mine = service
        .update(&doomed.id, 1, "my words", None, &context, None)
        .unwrap();
    assert_eq!(
        (mine.id.as_str(), mine.version, mine.body.as_str()),
        (doomed.id.as_str(), 2, "my words")
    );
    assert_eq!(env.count("memories"), 1);
    // after an update that changed nothing
    let same = record("same words");
    then(peers(vec![body(&same.id, 1, "a peer's words")]));
    let checked = service
        .update(&same.id, 1, "same words", None, &context, None)
        .unwrap();
    assert_eq!(
        (checked.version, checked.body.as_str(), &checked.updated),
        (1, "same words", &same.updated)
    );
    assert_eq!(state(&env, &same.id).0, 2);
    // after a first write
    let (store, database) = (env.memory_store(), env.raw());
    then(Box::new(move || {
        let recorded: String = database
            .query_row(
                "SELECT id FROM memories WHERE body = 'recorded words'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        store
            .apply(
                &[Op::soft_delete(&recorded, 1)],
                "human",
                None,
                &|_, _| None,
                &CAPS,
            )
            .unwrap();
    }));
    let recorded = record("recorded words");
    assert_eq!((recorded.version, &recorded.deleted_at), (1, &None));
    assert_eq!(
        state(&env, &recorded.id),
        (2, "recorded words".to_string(), true)
    );
}

#[test]
fn every_write_path_makes_one_version_owned_by_one_change() {
    let env = setup();
    let services = env.services();
    let created = services
        .memory
        .apply(
            &[Op::create(&env.project.id, "project", "cue", "v1")],
            "human",
            Some("cli"),
        )
        .unwrap();
    let memory_id = &created.steps[0].memory_id;
    let context = env.context();
    let edited = services
        .memory
        .update(memory_id, 1, "v2", None, &context, None)
        .unwrap();
    assert_eq!(edited.version, 2);
    assert_eq!(
        services.memory.delete(memory_id, 2, &context, "mcp", None),
        Ok(3)
    );
    assert_eq!(
        owners(&env, memory_id),
        [
            owner(1, false, "human", Some("cli"), "create", None),
            owner(2, false, "mcp", None, "update", Some(1)),
            owner(3, true, "mcp", None, "soft_delete", Some(2)),
        ]
    );
    assert!(row_is_latest_version(&env, memory_id));
    let recorded = services
        .memory
        .record(
            "uv manages python",
            "project",
            true,
            "codex",
            "cue",
            &context,
        )
        .unwrap();
    assert_eq!(
        (
            recorded.source_harness.as_str(),
            recorded.source_method.as_str()
        ),
        ("codex", "mcp")
    );
    assert_eq!(
        owners(&env, &recorded.id),
        [owner(1, false, "mcp", Some("codex"), "create", None)]
    );
    assert!(row_is_latest_version(&env, &recorded.id));
}

#[test]
fn agents_see_neither_history_nor_deleted_rows() {
    let env = setup();
    let (service, context) = (env.services().memory, env.context());
    let written = env.record("first words");
    service
        .update(&written.id, 1, "second words", None, &context, None)
        .unwrap();
    assert_eq!(service.search("first", None, &context), Ok(vec![]));
    let hits = service.search("second", None, &context).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(
        (hits[0].id.as_str(), hits[0].version, hits[0].body.as_str()),
        (written.id.as_str(), 2, "second words")
    );
    service
        .delete(&written.id, 2, &context, "mcp", None)
        .unwrap();
    assert_eq!(service.search("second", None, &context), Ok(vec![]));
    assert!(!service.index(&context, None).unwrap().contains(&written.id));
}

#[test]
fn writes_leave_the_read_facts_alone() {
    let env = setup();
    let written = env.record("a fact");
    env.raw()
        .execute(
            "UPDATE memories SET last_read_at = '2026-09-27T10:00:00.000000Z'",
            [],
        )
        .unwrap();
    env.services()
        .memory
        .update(&written.id, 1, "changed", None, &env.context(), None)
        .unwrap();
    human(&env, &[Op::soft_delete(&written.id, 2)]).unwrap();
    let last_read_at: String = env
        .raw()
        .query_row("SELECT last_read_at FROM memories", [], |row| row.get(0))
        .unwrap();
    assert_eq!(last_read_at, "2026-09-27T10:00:00.000000Z");
    assert_eq!(env.count("memory_reads"), 0);
}

#[test]
fn the_same_text_is_refused_where_any_other_text_is() {
    let env = setup();
    let (service, context) = (env.services().memory, env.context());
    let written = env.record("uv manages python");
    service
        .update(&written.id, 1, "v2", None, &context, None)
        .unwrap();
    // at a version that is not the current one
    assert_eq!(
        core(service.update(&written.id, 1, "v2", None, &context, None)),
        CoreError::VersionConflict(written.id.clone())
    );
    // in a project that became global after the context was made
    env.raw()
        .execute_batch(&format!(
            "UPDATE projects SET is_global = 0 WHERE id = '{}'; \
             UPDATE projects SET is_global = 1, root = NULL WHERE id = '{}';",
            env.global_id, env.project.id
        ))
        .unwrap();
    assert_eq!(
        core(service.update(&written.id, 2, "v2", None, &context, None)),
        CoreError::GlobalReadOnly
    );
    assert_eq!(state(&env, &written.id), (2, "v2".to_string(), false));
}

#[test]
fn restore_undeletes_and_restoring_a_deleted_version_deletes() {
    let env = setup();
    let service = env.services().memory;
    let memory_id = fact(&env, "kept");
    human(&env, &[Op::soft_delete(&memory_id, 1)]).unwrap();
    let change = service
        .restore(&memory_id, 2, 1, "human", Some("cli"))
        .unwrap();
    let step = &change.steps[0];
    assert_eq!(
        (step.op, step.before_version, step.after_version),
        (OpName::Restore, Some(2), 3)
    );
    assert_eq!(
        (change.changed_by.as_str(), change.changed_via.as_deref()),
        ("human", Some("cli"))
    );
    assert_eq!(state(&env, &memory_id), (3, "kept".to_string(), false));
    service.restore(&memory_id, 3, 2, "human", None).unwrap();
    assert_eq!(state(&env, &memory_id), (4, "kept".to_string(), true));
    assert_eq!(
        owners(&env, &memory_id).last().unwrap(),
        &owner(4, true, "human", None, "restore", Some(3))
    );
}

#[test]
fn restore_of_a_missing_or_the_current_state_is_refused() {
    let env = setup();
    let service = env.services().memory;
    let memory_id = fact(&env, "first");
    human(&env, &[body(&memory_id, 1, "second")]).unwrap();
    let before = rows(&env);
    for (expected_version, to_version, reason) in [
        (2, 9, Reason::Missing),
        (2, 0, Reason::Missing),
        (2, 2, Reason::SameState),
        (1, 1, Reason::Version),
    ] {
        assert_eq!(
            conflict(service.restore(&memory_id, expected_version, to_version, "human", None)),
            (0, Some(memory_id.clone()), reason),
            "{expected_version} {to_version}"
        );
    }
    assert_eq!(
        conflict(service.restore("zzzzzzzzzz", 1, 1, "human", None)).2,
        Reason::Missing
    );
    assert_eq!(rows(&env), before);
}

#[test]
fn restoring_a_damaged_history_row_is_a_storage_failure_and_writes_nothing() {
    for damage in [
        "body = CAST(X'80' AS TEXT)",
        "type = 'note'",
        "trust = 'nobody'",
        "tags = '{}'",
    ] {
        let env = setup();
        let memory_id = fact(&env, "clean");
        human(&env, &[body(&memory_id, 1, "second")]).unwrap();
        env.raw()
            .execute(
                &format!("UPDATE memory_versions SET {damage} WHERE version = 1"),
                [],
            )
            .unwrap();
        let before = rows(&env);
        let restored = env
            .services()
            .memory
            .restore(&memory_id, 2, 1, "human", None);
        assert_eq!(core(restored), CoreError::StorageFailure, "{damage}");
        assert_eq!(rows(&env), before);
        assert_eq!(state(&env, &memory_id), (2, "second".to_string(), false));
    }
}
