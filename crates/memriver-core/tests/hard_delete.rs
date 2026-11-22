//! The hard delete: a memory removed with every version it ever had, and with every
//! memory a version of which cites one that goes -- as one plan, confirmed as shown.

mod common;

use std::fs;

use common::{Env, body, citing, core, draft, setup};
use memriver_core::models::changes::{
    Confirmation, HardDeleteItem, HardDeletePlan, Op, PlanCitation,
};
use memriver_core::models::errors::{CoreError, UndoRefusedReason};
use memriver_core::repository::directories::DATABASE_FILENAME;

const TABLES: [&str; 6] = [
    "memories",
    "memory_versions",
    "memory_sources",
    "memory_reads",
    "changes",
    "change_steps",
];

fn rows(env: &Env) -> [i64; 6] {
    TABLES.map(|table| env.count(table))
}

fn human(env: &Env, ops: &[Op]) {
    env.services().memory.apply(ops, "human", None).unwrap();
}

fn fact(env: &Env, text: &str, cited: &[(&str, i64)]) -> String {
    env.made(draft(&env.project.id, text).citing(cited).op())
}

/// How many rows of `table` name one of `ids` in `column`.
fn named(env: &Env, table: &str, column: &str, ids: &[String]) -> i64 {
    let marks = vec!["?"; ids.len()].join(", ");
    env.raw()
        .query_row(
            &format!("SELECT count(*) FROM {table} WHERE {column} IN ({marks})"),
            rusqlite::params_from_iter(ids),
            |row| row.get(0),
        )
        .unwrap()
}

struct Graph {
    source: String,
    target: String,
    history_only: String,
    current: String,
    gone: String,
    unrelated: String,
}

/// `target` cites `source`; `history_only` cited `target` in its first version only;
/// `current` cites the current version of `history_only`; `gone`, deleted, cites
/// `current`.
fn graph(env: &Env) -> Graph {
    let source = fact(env, "the source", &[]);
    let target = fact(env, "the target", &[(&source, 1)]);
    let history_only = fact(env, "once cited it", &[(&target, 1)]);
    human(env, &[citing(&history_only, 1, &[])]);
    let current = fact(env, "cites it now", &[(&history_only, 2)]);
    let gone = fact(env, "deleted citer", &[(&current, 1)]);
    human(env, &[Op::soft_delete(&gone, 1)]);
    let unrelated = fact(env, "nothing to do with it", &[]);
    Graph {
        source,
        target,
        history_only,
        current,
        gone,
        unrelated,
    }
}

fn cites(
    citing: &str,
    version: i64,
    cited: &str,
    cited_version: i64,
    current: bool,
) -> PlanCitation {
    PlanCitation {
        citing_id: citing.to_string(),
        citing_version: version,
        cited_id: cited.to_string(),
        cited_version,
        citing_current: current,
    }
}

#[test]
fn plan_lists_the_target_then_every_citing_memory() {
    let env = setup();
    let graph = graph(&env);
    let before = rows(&env);
    let plan = env.memory_store().plan_hard_delete(&graph.target).unwrap();
    assert_eq!(plan.target, graph.target);
    // every version of every member is followed, a member's own sources never
    let mut referrers = [&graph.history_only, &graph.current, &graph.gone];
    referrers.sort();
    let listed: Vec<&String> = plan.items.iter().map(|item| &item.memory_id).collect();
    assert_eq!(
        listed,
        [&graph.target, referrers[0], referrers[1], referrers[2]]
    );
    let item = |memory_id: &str| {
        plan.items
            .iter()
            .find(|item| item.memory_id == memory_id)
            .unwrap()
    };
    let project = &env.project.id;
    assert_eq!(
        item(&graph.target),
        &HardDeleteItem {
            memory_id: graph.target.clone(),
            project_id: project.clone(),
            version: 1,
            deleted: false,
            citations: vec![],
        }
    );
    assert_eq!(
        item(&graph.history_only).citations,
        [cites(&graph.history_only, 1, &graph.target, 1, false)]
    );
    assert_eq!(item(&graph.history_only).version, 2);
    assert_eq!(
        item(&graph.current).citations,
        [cites(&graph.current, 1, &graph.history_only, 2, true)]
    );
    assert_eq!(
        item(&graph.gone),
        &HardDeleteItem {
            memory_id: graph.gone.clone(),
            project_id: project.clone(),
            version: 2,
            deleted: true,
            citations: vec![
                cites(&graph.gone, 1, &graph.current, 1, false),
                cites(&graph.gone, 2, &graph.current, 1, true),
            ],
        }
    );
    let expected: Vec<(&str, i64)> = plan.expected().into_iter().collect();
    let mut members = vec![
        (graph.target.as_str(), 1),
        (graph.history_only.as_str(), 2),
        (graph.current.as_str(), 1),
        (graph.gone.as_str(), 2),
    ];
    members.sort();
    assert_eq!(expected, members);
    assert_eq!(plan.code.len(), 16);
    assert!(
        plan.code
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    );
    // a plan is a reading: nothing is written, and the same reading gives the same plan
    assert_eq!(env.memory_store().plan_hard_delete(&graph.target), Ok(plan));
    assert_eq!(rows(&env), before);
}

#[test]
fn code_is_stable_across_implementations() {
    let env = setup();
    let target = fact(&env, "target", &[]);
    let first = fact(&env, "a", &[(&target, 1)]);
    let second = fact(&env, "b", &[(&target, 1)]);
    human(&env, &[body(&second, 1, "b2")]);
    // the same three memories under ids that can be written down
    let conn = env.unkeyed();
    for (drawn, literal) in [
        (&target, "tttttttttt"),
        (&first, "aaaaaaaaaa"),
        (&second, "bbbbbbbbbb"),
    ] {
        for (table, column) in [
            ("memories", "id"),
            ("memory_versions", "memory_id"),
            ("memory_sources", "memory_id"),
            ("memory_sources", "source_id"),
            ("change_steps", "memory_id"),
        ] {
            conn.execute(
                &format!("UPDATE {table} SET {column} = ?1 WHERE {column} = ?2"),
                [literal, drawn],
            )
            .unwrap();
        }
    }
    let item = |memory_id: &str, version, citations| HardDeleteItem {
        memory_id: memory_id.to_string(),
        project_id: env.project.id.clone(),
        version,
        deleted: false,
        citations,
    };
    // the code the Python implementation gives the same three: the first 16 hex
    // digits of the SHA-256 of "aaaaaaaaaa:1\nbbbbbbbbbb:2\ntttttttttt:1"
    assert_eq!(
        env.memory_store().plan_hard_delete("tttttttttt"),
        Ok(HardDeletePlan {
            target: "tttttttttt".to_string(),
            items: vec![
                item("tttttttttt", 1, vec![]),
                item(
                    "aaaaaaaaaa",
                    1,
                    vec![cites("aaaaaaaaaa", 1, "tttttttttt", 1, true)]
                ),
                item(
                    "bbbbbbbbbb",
                    2,
                    vec![
                        cites("bbbbbbbbbb", 1, "tttttttttt", 1, false),
                        cites("bbbbbbbbbb", 2, "tttttttttt", 1, true)
                    ]
                ),
            ],
            code: "289d09a8b810c4bb".to_string(),
        })
    );
}

#[test]
fn changed_plan_is_refused() {
    let env = setup();
    let graph = graph(&env);
    let store = env.memory_store();
    let shown = store.plan_hard_delete(&graph.target).unwrap();
    human(&env, &[body(&graph.current, 1, "cites it now, edited")]);
    let before = rows(&env);
    let now = store.plan_hard_delete(&graph.target).unwrap();
    assert_ne!(now.code, shown.code);
    for stale in [
        Confirmation::Expected(&shown.expected()),
        Confirmation::Code(&shown.code),
        Confirmation::Code(""),
    ] {
        // the refusal carries the plan as it stands now
        assert_eq!(
            store.hard_delete(&graph.target, stale),
            Err(CoreError::PlanChanged(now.clone()))
        );
    }
    assert_eq!(rows(&env), before);
    // a new referrer changes the plan just the same
    fact(&env, "a new referrer", &[(&graph.target, 1)]);
    assert!(matches!(
        store.hard_delete(&graph.target, Confirmation::Code(&now.code)),
        Err(CoreError::PlanChanged(_))
    ));
    assert_eq!(env.count("memories"), before[0] + 1);
}

#[test]
fn all_history_reads_and_sources_go_in_one_transaction() {
    for by_code in [false, true] {
        let env = setup();
        let graph = graph(&env);
        let services = env.services();
        // a read fact that goes with its memory
        services
            .memory
            .read(&graph.current, &env.context(), "codex")
            .unwrap();
        let mixed = services
            .memory
            .apply(
                &[
                    body(&graph.current, 1, "cites it now, edited"),
                    body(&graph.unrelated, 1, "still nothing to do with it"),
                ],
                "human",
                None,
            )
            .unwrap();
        let changes = env.count("changes");
        let store = env.memory_store();
        let plan = store.plan_hard_delete(&graph.target).unwrap();
        let expected = plan.expected();
        let confirmation = if by_code {
            Confirmation::Code(&plan.code)
        } else {
            Confirmation::Expected(&expected)
        };
        let deleted = store.hard_delete(&graph.target, confirmation).unwrap();
        let members: Vec<String> = plan
            .items
            .iter()
            .map(|item| item.memory_id.clone())
            .collect();
        assert_eq!(deleted, members);
        for (table, column) in [
            ("memories", "id"),
            ("memory_versions", "memory_id"),
            ("memory_sources", "memory_id"),
            ("memory_sources", "source_id"),
            ("memory_reads", "memory_id"),
            ("change_steps", "memory_id"),
        ] {
            assert_eq!(named(&env, table, column, &deleted), 0, "{table}.{column}");
        }
        // change rows stay, and so does everything that is no member
        assert_eq!(env.count("changes"), changes);
        let survivors = [graph.source.clone(), graph.unrelated.clone()];
        assert_eq!(named(&env, "memories", "id", &survivors), 2);
        assert_eq!(
            named(&env, "memory_versions", "memory_id", &survivors[..1]),
            1
        );
        // the change that touched a member and a survivor cannot be undone any more
        assert_eq!(
            core(services.memory.undo(&mixed.change_id, "human", None)),
            CoreError::UndoRefused {
                reason: UndoRefusedReason::HardDeleted,
                memory_ids: vec![],
            }
        );
        let left = services.memory.change(&mixed.change_id).unwrap().unwrap();
        assert_eq!((left.step_count, left.steps.len()), (2, 1));
        assert_eq!(left.steps[0].memory_id, graph.unrelated);
    }
}

#[test]
fn an_unknown_id_is_not_found() {
    let env = setup();
    fact(&env, "a fact", &[]);
    let before = rows(&env);
    let store = env.memory_store();
    let nowhere = memriver_core::repository::sqlite::memory_store::MemoryStore::new(
        &env.tmp.join("no-store"),
    );
    for memory_id in ["zzzzzzzzzz", "nope", ""] {
        let not_found = CoreError::MemoryNotFound(memory_id.to_string());
        for store in [&store, &nowhere] {
            assert_eq!(store.plan_hard_delete(memory_id), Err(not_found.clone()));
            assert_eq!(
                store.hard_delete(memory_id, Confirmation::Code("0123456789abcdef")),
                Err(not_found.clone())
            );
        }
    }
    assert_eq!(rows(&env), before);
    assert!(!env.tmp.join("no-store").exists());
}

#[test]
fn a_referrer_id_that_is_no_id_is_a_storage_failure() {
    // an id no write of memriver makes: bytes that are not UTF-8, and text outside
    // the alphabet of an id
    for raw_id in [&b"\x80123456789"[..], &b"\x1b[31m\nABCD"[..]] {
        let env = setup();
        let target = fact(&env, "the target", &[]);
        fact(&env, "cites it too", &[(&target, 1)]);
        let citer = fact(&env, "cites it", &[(&target, 1)]);
        let conn = env.unkeyed();
        for (table, column) in [("memories", "id"), ("memory_sources", "memory_id")] {
            conn.execute(
                &format!("UPDATE {table} SET {column} = CAST(?1 AS TEXT) WHERE {column} = ?2"),
                (raw_id, &citer),
            )
            .unwrap();
        }
        let before = rows(&env);
        let store = env.memory_store();
        assert_eq!(
            store.plan_hard_delete(&target),
            Err(CoreError::StorageFailure)
        );
        assert_eq!(
            store.hard_delete(&target, Confirmation::Code("0123456789abcdef")),
            Err(CoreError::StorageFailure)
        );
        assert_eq!(rows(&env), before);
    }
}

#[test]
fn no_copy_of_the_text_stays_in_the_database_file() {
    let env = setup();
    let marker = "AKIASECRETMARKER1234567890";
    let target = fact(&env, marker, &[]);
    let database = env.store.join(DATABASE_FILENAME);
    let holds = |bytes: &[u8]| {
        bytes
            .windows(marker.len())
            .any(|at| at == marker.as_bytes())
    };
    assert!(holds(&fs::read(&database).unwrap()));
    let store = env.memory_store();
    let plan = store.plan_hard_delete(&target).unwrap();
    assert_eq!(
        store.hard_delete(&target, Confirmation::Expected(&plan.expected())),
        Ok(vec![target])
    );
    for suffix in ["-journal", "-wal"] {
        let beside = format!("{DATABASE_FILENAME}{suffix}");
        assert!(!env.store.join(beside).exists(), "{suffix}");
    }
    assert!(!holds(&fs::read(&database).unwrap()));
}

#[test]
fn a_global_or_deleted_target_is_deleted_like_any_other() {
    let env = setup();
    let principle = env.made(draft(&env.global_id, "a principle").op());
    let gone = fact(&env, "gone", &[]);
    human(&env, &[Op::soft_delete(&gone, 1)]);
    let store = env.memory_store();
    for target in [&principle, &gone] {
        let plan = store.plan_hard_delete(target).unwrap();
        assert_eq!(
            store.hard_delete(target, Confirmation::Expected(&plan.expected())),
            Ok(vec![target.clone()])
        );
    }
    assert_eq!(env.count("memories"), 0);
}

#[test]
fn a_cited_memory_does_not_go_without_the_memories_citing_it() {
    let env = setup();
    let source = fact(&env, "source", &[]);
    let derived = fact(&env, "derived", &[(&source, 1)]);
    let before = rows(&env);
    // the key of a citation is looked at when the transaction ends: the source alone
    // cannot be removed, by whoever tries
    let conn = env.raw();
    conn.execute_batch("PRAGMA foreign_keys = ON; BEGIN")
        .unwrap();
    conn.execute("DELETE FROM memories WHERE id = ?1", [&source])
        .unwrap();
    assert!(conn.execute_batch("COMMIT").is_err());
    conn.execute_batch("ROLLBACK").unwrap();
    assert_eq!(rows(&env), before);
    // which is why the plan of the source takes the memory citing it along
    let store = env.memory_store();
    let plan = store.plan_hard_delete(&source).unwrap();
    assert_eq!(
        store.hard_delete(&source, Confirmation::Code(&plan.code)),
        Ok(vec![source, derived])
    );
    assert_eq!(env.count("memory_sources"), 0);
    assert_eq!(env.count("memory_versions"), 0);
}

/// A store whose first memory is cited by `referrers` others, written row by row
/// behind the store's back, keys kept: the id of the first.
fn cited_by(env: &Env, referrers: usize) -> String {
    let id = |number: usize| format!("{number:010}");
    let mut conn = env.raw();
    let rows = conn.transaction().unwrap();
    {
        let mut memory = rows
            .prepare(
                "INSERT INTO memories (id, project_id, source_harness, source_method, created, \
                 version, type, trust, sync, description, body, updated) VALUES (?1, ?2, 'human', \
                 'cli', ?3, 1, 'user', 'user', 1, '', 'a body', ?3)",
            )
            .unwrap();
        let mut version = rows
            .prepare(
                "INSERT INTO memory_versions (memory_id, version, type, trust, sync, \
                 description, body, deleted, change_id) \
                 VALUES (?1, 1, 'user', 'user', 1, '', 'a body', 0, NULL)",
            )
            .unwrap();
        let mut source = rows
            .prepare("INSERT INTO memory_sources VALUES (?1, 1, ?2, 1)")
            .unwrap();
        for number in 0..=referrers {
            memory
                .execute([&id(number), &env.project.id, "2026-11-22T00:00:00.000001Z"])
                .unwrap();
            version.execute([&id(number)]).unwrap();
            if number > 0 {
                source.execute([&id(number), &id(0)]).unwrap();
            }
        }
    }
    rows.commit().unwrap();
    id(0)
}

#[test]
fn a_plan_of_any_size_that_was_confirmed_is_carried_out() {
    // one more member than a statement of SQLite takes values, and one fewer
    for referrers in [32_766, 32_765] {
        let env = setup();
        let survivor = fact(&env, "nothing to do with it", &[]);
        let target = cited_by(&env, referrers);
        assert_eq!(env.count("memory_sources"), referrers as i64);
        let store = env.memory_store();
        let plan = store.plan_hard_delete(&target).unwrap();
        assert_eq!(plan.items.len(), referrers + 1);
        // a code that is not the plan's removes nothing
        assert!(matches!(
            store.hard_delete(&target, Confirmation::Code("0123456789abcdef")),
            Err(CoreError::PlanChanged(_))
        ));
        assert_eq!(env.count("memories"), referrers as i64 + 2);
        let deleted = store
            .hard_delete(&target, Confirmation::Code(&plan.code))
            .unwrap();
        assert_eq!(deleted.len(), referrers + 1, "{referrers}");
        assert_eq!(deleted[0], target);
        // every member is gone with its history, and nothing else
        assert_eq!(rows(&env), [1, 1, 0, 0, 1, 1], "{referrers}: {TABLES:?}");
        assert_eq!(
            env.services().memory.show(&survivor, false).unwrap().id,
            survivor
        );
    }
}
