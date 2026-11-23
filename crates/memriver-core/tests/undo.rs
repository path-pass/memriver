//! Undo: the inverse of a change as one new change, while none of its memories
//! changed since and none was removed with its history.

mod common;

use common::{Env, SECRET, SECRET_RULE, body, core, draft, rejected, restore, setup};
use memriver_core::models::changes::{Change, Confirmation, Op, OpName};
use memriver_core::models::errors::{
    BatchConflictReason, CoreError, UndoRefusedReason, WriteError,
};
use rusqlite::Connection;

fn human(env: &Env, ops: &[Op]) -> Change {
    env.services().memory.apply(ops, "human", None).unwrap()
}

fn undo(env: &Env, change_id: &str) -> Result<Change, WriteError> {
    env.services().memory.undo(change_id, "human", Some("cli"))
}

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

fn is(version: i64, text: &str, deleted: bool) -> (i64, String, bool) {
    (version, text.to_string(), deleted)
}

fn counts(env: &Env) -> [i64; 4] {
    [
        "memory_versions",
        "memory_sources",
        "changes",
        "change_steps",
    ]
    .map(|table| env.count(table))
}

fn refused(result: Result<Change, WriteError>) -> (UndoRefusedReason, Vec<String>) {
    match core(result) {
        CoreError::UndoRefused { reason, memory_ids } => (reason, memory_ids),
        other => panic!("not a refused undo: {other:?}"),
    }
}

/// Two memories merged into a third in one change: the change, the merged memory and
/// the two originals.
fn merge(env: &Env) -> (Change, String, String, String) {
    let (alpha, beta) = (fact(env, "alpha"), fact(env, "beta"));
    let change = human(
        env,
        &[
            draft(&env.project.id, "alpha and beta")
                .citing(&[(&alpha, 1), (&beta, 1)])
                .op(),
            Op::soft_delete(&alpha, 1),
            Op::soft_delete(&beta, 1),
        ],
    );
    let merged = change.steps[0].memory_id.clone();
    (change, merged, alpha, beta)
}

/// What a hard delete does to the rows: the memories go, their versions, sources,
/// reads and steps with them; change rows stay.
fn remove_rows(env: &Env, memory_ids: &[&str]) {
    let conn: Connection = env.raw();
    conn.execute_batch("PRAGMA foreign_keys = ON; BEGIN")
        .unwrap();
    for memory_id in memory_ids {
        conn.execute("DELETE FROM memories WHERE id = ?1", [memory_id])
            .unwrap();
    }
    conn.execute_batch("COMMIT").unwrap();
}

fn step_count(env: &Env, change_id: &str) -> i64 {
    env.raw()
        .query_row(
            "SELECT step_count FROM changes WHERE change_id = ?1",
            [change_id],
            |row| row.get(0),
        )
        .unwrap()
}

#[test]
fn create_is_undone_by_soft_delete() {
    let env = setup();
    let change = human(&env, &[draft(&env.project.id, "made").op()]);
    let memory_id = &change.steps[0].memory_id;
    let undone = undo(&env, &change.change_id).unwrap();
    assert_eq!(
        (
            undone.undoes.as_deref(),
            undone.changed_by.as_str(),
            undone.changed_via.as_deref(),
            undone.step_count
        ),
        (Some(change.change_id.as_str()), "human", Some("cli"), 1)
    );
    let steps: Vec<_> = undone
        .steps
        .iter()
        .map(|step| {
            (
                step.op,
                step.memory_id.as_str(),
                step.before_version,
                step.after_version,
            )
        })
        .collect();
    assert_eq!(
        steps,
        [(OpName::SoftDelete, memory_id.as_str(), Some(1), 2)]
    );
    assert_eq!(state(&env, memory_id), is(2, "made", true));
    let stored: Option<String> = env
        .raw()
        .query_row(
            "SELECT undoes FROM changes WHERE change_id = ?1",
            [&undone.change_id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(stored, Some(change.change_id));
}

#[test]
fn update_restores_the_previous_state() {
    let env = setup();
    let memory_id = fact(&env, "v1");
    let update = human(&env, &[body(&memory_id, 1, "v2")]);
    let undone = undo(&env, &update.change_id).unwrap();
    let step = &undone.steps[0];
    assert_eq!(
        (step.op, step.before_version, step.after_version),
        (OpName::Restore, Some(2), 3)
    );
    assert_eq!(state(&env, &memory_id), is(3, "v1", false));
    // a delete
    let delete = human(&env, &[Op::soft_delete(&memory_id, 3)]);
    undo(&env, &delete.change_id).unwrap();
    assert_eq!(state(&env, &memory_id), is(5, "v1", false));
    // a restore
    let restored = env
        .services()
        .memory
        .restore(&memory_id, 5, 2, "human", None)
        .unwrap();
    assert_eq!(state(&env, &memory_id), is(6, "v2", false));
    undo(&env, &restored.change_id).unwrap();
    assert_eq!(state(&env, &memory_id), is(7, "v1", false));
}

#[test]
fn undo_of_an_undo() {
    let env = setup();
    let (merge, merged, alpha, beta) = merge(&env);
    let undone = undo(&env, &merge.change_id).unwrap();
    // a step for each step of the change, in the same order
    let steps: Vec<_> = undone
        .steps
        .iter()
        .map(|step| (step.step, step.memory_id.as_str(), step.op))
        .collect();
    assert_eq!(
        steps,
        [
            (1, merged.as_str(), OpName::SoftDelete),
            (2, alpha.as_str(), OpName::Restore),
            (3, beta.as_str(), OpName::Restore)
        ]
    );
    assert_eq!(state(&env, &merged), is(2, "alpha and beta", true));
    assert_eq!(state(&env, &alpha), is(3, "alpha", false));
    assert_eq!(state(&env, &beta), is(3, "beta", false));
    // the originals moved on; the second undo still puts back the merged memory's
    // citations of their first versions, which a new citation could not make
    let redone = undo(&env, &undone.change_id).unwrap();
    assert_eq!(redone.undoes.as_deref(), Some(undone.change_id.as_str()));
    assert_eq!(state(&env, &merged), is(3, "alpha and beta", false));
    assert_eq!(state(&env, &alpha), is(4, "alpha", true));
    assert_eq!(state(&env, &beta), is(4, "beta", true));
    let conn = env.raw();
    let mut statement = conn
        .prepare(
            "SELECT source_id, source_version FROM memory_sources \
             WHERE memory_id = ?1 AND version = 3 ORDER BY source_id",
        )
        .unwrap();
    let cited: Vec<(String, i64)> = statement
        .query_map([&merged], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    let mut expected = vec![(alpha, 1), (beta, 1)];
    expected.sort();
    assert_eq!(cited, expected);
}

#[test]
fn later_change_refuses_the_undo_naming_the_ids() {
    let env = setup();
    // a change that was undone: every memory of it moved
    let (first, merged, alpha, beta) = merge(&env);
    undo(&env, &first.change_id).unwrap();
    let mut all = vec![merged, alpha, beta];
    all.sort();
    assert_eq!(
        refused(undo(&env, &first.change_id)),
        (UndoRefusedReason::Changed, all)
    );
    // one memory of three: the whole undo is refused, nothing is written
    let (second, merged, alpha, _) = merge(&env);
    human(&env, &[body(&merged, 1, "edited")]);
    let before = counts(&env);
    assert_eq!(
        refused(undo(&env, &second.change_id)),
        (UndoRefusedReason::Changed, vec![merged])
    );
    assert_eq!(counts(&env), before);
    assert_eq!(state(&env, &alpha), is(2, "alpha", true));
    // named in the order of their ids, whatever the order of the change's steps
    let mut pair = [fact(&env, "one"), fact(&env, "two")];
    pair.sort();
    let backwards = human(
        &env,
        &[Op::soft_delete(&pair[1], 1), Op::soft_delete(&pair[0], 1)],
    );
    undo(&env, &backwards.change_id).unwrap();
    assert_eq!(
        refused(undo(&env, &backwards.change_id)),
        (UndoRefusedReason::Changed, pair.to_vec())
    );
    // text changed back to what it was is still a change
    let memory_id = fact(&env, "same");
    let edit = human(&env, &[body(&memory_id, 1, "other")]);
    human(&env, &[body(&memory_id, 2, "same")]);
    assert_eq!(
        refused(undo(&env, &edit.change_id)),
        (UndoRefusedReason::Changed, vec![memory_id])
    );
}

#[test]
fn a_read_does_not_refuse_an_undo() {
    let env = setup();
    let memory_id = fact(&env, "v1");
    let change = human(&env, &[body(&memory_id, 1, "v2")]);
    env.services()
        .memory
        .read(&memory_id, &env.context(), "codex")
        .unwrap();
    undo(&env, &change.change_id).unwrap();
    assert_eq!(state(&env, &memory_id), is(3, "v1", false));
}

#[test]
fn hard_deleted_step_refuses() {
    let env = setup();
    let (merge, merged, alpha, beta) = merge(&env);
    let unrelated_id = fact(&env, "unrelated");
    let unrelated = human(&env, &[body(&unrelated_id, 1, "edited")]);
    // one memory of the change is gone: it cites, nothing cites it
    remove_rows(&env, &[&merged]);
    assert_eq!(
        refused(undo(&env, &merge.change_id)),
        (UndoRefusedReason::HardDeleted, vec![])
    );
    // all of them
    remove_rows(&env, &[&alpha, &beta]);
    assert_eq!(
        refused(undo(&env, &merge.change_id)).0,
        UndoRefusedReason::HardDeleted
    );
    // the change row stays, and another change is undone as ever
    assert_eq!(step_count(&env, &merge.change_id), 3);
    undo(&env, &unrelated.change_id).unwrap();
    assert_eq!(state(&env, &unrelated_id), is(3, "unrelated", false));
}

#[test]
fn a_real_hard_delete_of_one_member_refuses_the_undo_and_keeps_the_change_row() {
    let env = setup();
    let (merge, merged, _, _) = merge(&env);
    let store = env.memory_store();
    let plan = store.plan_hard_delete(&merged).unwrap();
    assert_eq!(
        store.hard_delete(&merged, Confirmation::Expected(&plan.expected())),
        Ok(vec![merged.clone()])
    );
    assert_eq!(
        refused(undo(&env, &merge.change_id)),
        (UndoRefusedReason::HardDeleted, vec![])
    );
    assert_eq!(step_count(&env, &merge.change_id), 3);
}

#[test]
fn a_policy_hit_in_the_restored_content_refuses_the_undo() {
    let env = setup();
    let memory_id = fact(&env, "clean");
    env.raw()
        .execute(
            "UPDATE memory_versions SET body = ?1 WHERE memory_id = ?2 AND version = 1",
            [SECRET, &memory_id],
        )
        .unwrap();
    let change = human(&env, &[body(&memory_id, 1, "edited")]);
    let before = counts(&env);
    let by_undo = rejected(undo(&env, &change.change_id));
    let by_apply = rejected(env.services().memory.apply(
        &[body(&memory_id, 2, SECRET)],
        "human",
        None,
    ));
    assert_eq!(
        by_undo,
        (
            String::new(),
            SECRET_RULE.to_string(),
            Some(memory_id.clone())
        )
    );
    assert_eq!(by_undo, by_apply);
    // by a restore it is the same refusal
    let by_restore = rejected(
        env.services()
            .memory
            .restore(&memory_id, 2, 1, "human", None),
    );
    assert_eq!(by_restore, by_undo);
    assert_eq!(counts(&env), before);
    assert_eq!(state(&env, &memory_id), is(2, "edited", false));
}

#[test]
fn an_undo_back_to_a_deleted_version_whose_text_hits_the_policy_is_refused() {
    let env = setup();
    let memory_id = fact(&env, "clean");
    human(&env, &[Op::soft_delete(&memory_id, 1)]);
    env.raw()
        .execute(
            "UPDATE memory_versions SET body = ?1 WHERE memory_id = ?2 AND version = 2",
            [SECRET, &memory_id],
        )
        .unwrap();
    let undelete = human(&env, &[restore(&memory_id, 2, 1)]);
    let before = counts(&env);
    // a state that is deleted passes the policy too: by an undo and by a restore
    for back in [
        undo(&env, &undelete.change_id),
        env.services()
            .memory
            .restore(&memory_id, 3, 2, "human", None),
    ] {
        assert_eq!(
            rejected(back),
            (
                String::new(),
                SECRET_RULE.to_string(),
                Some(memory_id.clone())
            )
        );
    }
    assert_eq!(counts(&env), before);
    assert_eq!(state(&env, &memory_id), is(3, "clean", false));
}

#[test]
fn an_unknown_change_is_not_found() {
    let env = setup();
    fact(&env, "a fact");
    let before = counts(&env);
    for change_id in ["zzzzzzzzzz", "nope", ""] {
        assert_eq!(
            refused(undo(&env, change_id)),
            (UndoRefusedReason::NotFound, vec![]),
            "{change_id:?}"
        );
        assert_eq!(env.services().memory.change(change_id), Ok(None));
    }
    assert_eq!(counts(&env), before);
    // a store that is not there is never made by an undo
    let nowhere = env.limited(|settings| settings.root = env.tmp.join("no-store"));
    assert_eq!(
        core(nowhere.memory.undo("zzzzzzzzzz", "human", None)),
        CoreError::StorageFailure
    );
    assert_eq!(nowhere.memory.change("zzzzzzzzzz"), Ok(None));
    assert!(!env.tmp.join("no-store").exists());
}

#[test]
fn change_returns_the_change_with_its_stored_steps() {
    let env = setup();
    let (merge, merged, alpha, beta) = merge(&env);
    let service = env.services().memory;
    assert_eq!(service.change(&merge.change_id), Ok(Some(merge.clone())));
    // an undo is a change like another, with what it undoes
    let undone = undo(&env, &merge.change_id).unwrap();
    assert_eq!(service.change(&undone.change_id), Ok(Some(undone)));
    remove_rows(&env, &[&merged]);
    let partial = service.change(&merge.change_id).unwrap().unwrap();
    let left: Vec<&str> = partial
        .steps
        .iter()
        .map(|step| step.memory_id.as_str())
        .collect();
    assert_eq!((partial.step_count, left), (3, vec![&*alpha, &*beta]));
}

#[test]
fn a_step_without_the_version_before_it_is_refused_and_nothing_is_written() {
    let env = setup();
    let memory_id = fact(&env, "v1");
    let edit = human(&env, &[body(&memory_id, 1, "v2")]);
    // a step that lost the version it came from, and a version numbered 0 that no
    // write of memriver makes: neither is taken for the other
    let conn = env.unkeyed();
    conn.execute(
        "UPDATE change_steps SET before_version = NULL WHERE change_id = ?1",
        [&edit.change_id],
    )
    .unwrap();
    conn.execute_batch("PRAGMA ignore_check_constraints = ON")
        .unwrap();
    conn.execute(
        "INSERT INTO memory_versions (memory_id, version, type, trust, sync, description, \
         body, deleted, change_id) VALUES (?1, 0, 'project', 'agent', 1, '', 'planted', 0, NULL)",
        [&memory_id],
    )
    .unwrap();
    let before = counts(&env);
    assert_eq!(
        core(undo(&env, &edit.change_id)),
        CoreError::BatchConflict {
            index: 0,
            memory_id: Some(memory_id.clone()),
            reason: BatchConflictReason::Missing,
        }
    );
    assert_eq!(counts(&env), before);
    assert_eq!(state(&env, &memory_id), is(2, "v2", false));
    let step: (Option<i64>, i64) = env
        .raw()
        .query_row(
            "SELECT before_version, after_version FROM change_steps WHERE change_id = ?1",
            [&edit.change_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(step, (None, 2));
    // a restore that names version 0 itself is another matter: it is what was asked
    human(&env, &[restore(&memory_id, 2, 0)]);
    assert_eq!(state(&env, &memory_id), is(3, "planted", false));
}
