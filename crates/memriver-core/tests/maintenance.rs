//! The maintenance service over a real store: the hard delete as the human command
//! reaches it, the scan of every stored version by today's rules, and a text checked
//! before it is stored.

mod common;

use common::{Env, SECRET, SECRET_RULE, body, core, draft, setup};
use memriver_core::Services;
use memriver_core::application::maintenance::MaintenanceService;
use memriver_core::models::changes::{Confirmation, Op, PolicyHit};
use memriver_core::models::diagnostics::DiagnosticsState;
use memriver_core::models::errors::{CoreError, UndoRefusedReason};

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

fn fact(env: &Env, text: &str, cited: &[(&str, i64)]) -> String {
    env.made(draft(&env.project.id, text).citing(cited).op())
}

fn hit(memory_id: &str, version: i64, current: bool) -> PolicyHit {
    PolicyHit {
        memory_id: memory_id.to_string(),
        version,
        rule_id: SECRET_RULE.to_string(),
        current,
    }
}

#[test]
fn the_services_with_maintenance_can_be_handed_to_another_thread() {
    fn handed<T: Send + Sync>() {}
    handed::<Services>();
    handed::<MaintenanceService>();
}

#[test]
fn hard_delete_goes_through_the_service() {
    for by_code in [false, true] {
        let env = setup();
        let source = fact(&env, "the source", &[]);
        let target = fact(&env, "the target", &[(&source, 1)]);
        let citer = fact(&env, "cites it", &[(&target, 1)]);
        let maintenance = env.services().maintenance;
        let plan = maintenance.plan_hard_delete(&target).unwrap();
        assert_eq!(plan, env.memory_store().plan_hard_delete(&target).unwrap());
        let members: Vec<_> = plan
            .items
            .iter()
            .map(|item| item.memory_id.clone())
            .collect();
        assert_eq!(members, [target.clone(), citer]);
        // a plan that is not the one shown deletes nothing
        let before = rows(&env);
        let stale = maintenance.hard_delete(&target, Confirmation::Code("0123456789abcdef"));
        assert_eq!(stale, Err(CoreError::PlanChanged(plan.clone())));
        assert_eq!(rows(&env), before);
        let expected = plan.expected();
        let confirmation = if by_code {
            Confirmation::Code(&plan.code)
        } else {
            Confirmation::Expected(&expected)
        };
        assert_eq!(maintenance.hard_delete(&target, confirmation), Ok(members));
        assert_eq!(env.count("memories"), 1);
        assert_eq!(
            maintenance.plan_hard_delete(&target),
            Err(CoreError::MemoryNotFound(target))
        );
    }
}

/// A change that touched a member of a hard delete and a survivor is incomplete from
/// then on: not undoable, and listed by the diagnosis without making the store degraded.
#[test]
fn a_confirmed_hard_delete_leaves_an_incomplete_change_the_diagnosis_lists() {
    let env = setup();
    let services = env.services();
    let target = fact(&env, "the target", &[]);
    let citer = fact(&env, "cites it now", &[(&target, 1)]);
    let unrelated = fact(&env, "nothing to do with it", &[]);
    let mixed = services
        .memory
        .apply(
            &[
                body(&citer, 1, "cites it now, edited"),
                body(&unrelated, 1, "still nothing to do with it"),
            ],
            "human",
            None,
        )
        .unwrap();
    let before = services.maintenance.diagnose(None, 90).unwrap();
    assert_eq!(before.incomplete_changes, Vec::<String>::new());
    let plan = services.maintenance.plan_hard_delete(&target).unwrap();
    services
        .maintenance
        .hard_delete(&target, Confirmation::Code(&plan.code))
        .unwrap();
    let refused = core(services.memory.undo(&mixed.change_id, "human", None));
    assert!(matches!(
        refused,
        CoreError::UndoRefused {
            reason: UndoRefusedReason::HardDeleted,
            ..
        }
    ));
    let report = services.maintenance.diagnose(None, 90).unwrap();
    // the changes that made the two members hold no step any more either
    assert!(report.incomplete_changes.contains(&mixed.change_id));
    assert_eq!(report.incomplete_changes.len(), 3);
    let mut sorted = report.incomplete_changes.clone();
    sorted.sort();
    assert_eq!(report.incomplete_changes, sorted);
    assert_eq!(report.findings, Vec::new());
    assert_eq!(report.state, DiagnosticsState::Healthy);
}

#[test]
fn scan_policy_reports_history_and_current_hits() {
    let env = setup();
    let services = env.services();
    let set = |sql: &str, id: &str| {
        env.raw().execute(sql, (SECRET, id)).unwrap();
    };
    // in an older version only
    let edited = fact(&env, "clean", &[]);
    set(
        "UPDATE memory_versions SET body = ?1 WHERE memory_id = ?2 AND version = 1",
        &edited,
    );
    env.made(body(&edited, 1, "clean, edited"));
    // in the description of a deleted memory's current version
    let gone = fact(&env, "to go", &[]);
    env.made(Op::soft_delete(&gone, 1));
    set(
        "UPDATE memory_versions SET description = ?1 WHERE memory_id = ?2 AND version = 2",
        &gone,
    );
    set("UPDATE memories SET description = ?1 WHERE id = ?2", &gone);
    // in a global memory
    let principle = env.made(draft(&env.global_id, "a principle").op());
    set(
        "UPDATE memory_versions SET body = ?1 WHERE memory_id = ?2",
        &principle,
    );
    set("UPDATE memories SET body = ?1 WHERE id = ?2", &principle);
    // in the description and the body of one version: one hit
    let twice = fact(&env, "both", &[]);
    env.raw()
        .execute(
            "UPDATE memory_versions SET description = ?1, body = ?1 WHERE memory_id = ?2",
            (SECRET, &twice),
        )
        .unwrap();
    fact(&env, "nothing here", &[]);
    let before = rows(&env);

    let hits = services.maintenance.scan_policy().unwrap();
    let mut expected = vec![
        hit(&edited, 1, false),
        hit(&gone, 2, true),
        hit(&principle, 1, true),
        hit(&twice, 1, true),
    ];
    // by memory, then by version
    expected.sort_by(|a, b| a.memory_id.cmp(&b.memory_id));
    assert_eq!(hits, expected);
    assert_eq!(
        services.maintenance.check_text(SECRET).as_deref(),
        Some(SECRET_RULE)
    );
    // a hit says where, never what
    assert!(!format!("{hits:?}").contains("AKIA"));
    // no deletion, no read fact
    assert_eq!(rows(&env), before);
}

#[test]
fn the_scan_skips_a_row_whose_id_is_not_addressable() {
    // an id no write of memriver makes: bytes that are not UTF-8, and text outside the
    // alphabet of an id. No command could act on a hit that names it
    for raw_id in [&b"\x80123456789"[..], &b"\x1b[31m\nABCD"[..]] {
        let env = setup();
        let doomed = fact(&env, "clean", &[]);
        let conn = env.unkeyed();
        conn.execute(
            "UPDATE memory_versions SET body = ?1 WHERE memory_id = ?2",
            (SECRET, &doomed),
        )
        .unwrap();
        assert_eq!(env.services().maintenance.scan_policy().unwrap().len(), 1);
        for (table, column) in [("memory_versions", "memory_id"), ("memories", "id")] {
            conn.execute(
                &format!("UPDATE {table} SET {column} = CAST(?1 AS TEXT) WHERE {column} = ?2"),
                (raw_id, &doomed),
            )
            .unwrap();
        }
        assert_eq!(env.services().maintenance.scan_policy(), Ok(Vec::new()));
    }
}

#[test]
fn the_scan_passes_over_a_text_that_is_none() {
    let env = setup();
    let id = fact(&env, "clean", &[]);
    env.raw()
        .execute(
            "UPDATE memory_versions SET description = CAST(X'80' AS TEXT), body = ?1",
            [SECRET],
        )
        .unwrap();
    // the description is no text and is passed over; the body is still looked at
    assert_eq!(
        env.services().maintenance.scan_policy(),
        Ok(vec![hit(&id, 1, true)])
    );
}

#[test]
fn the_scan_of_a_store_that_is_not_there_is_empty_and_creates_nothing() {
    let env = setup();
    let mut settings = env.settings.clone();
    settings.root = env.tmp.join("none");
    let services = memriver_core::build_services(&settings, &env.home, &env.env, None);
    assert_eq!(services.maintenance.scan_policy(), Ok(Vec::new()));
    assert!(!settings.root.exists());
}

#[test]
fn a_scan_of_a_store_below_schema_6_says_so() {
    let env = setup();
    env.raw().execute_batch("PRAGMA user_version = 5").unwrap();
    assert_eq!(
        env.services().maintenance.scan_policy(),
        Err(CoreError::StoreNeedsUpgrade(5))
    );
}

#[test]
fn check_text_names_the_rule_of_any_text_and_has_no_side_effect() {
    let env = setup();
    fact(&env, "a memory", &[]);
    let before = rows(&env);
    let maintenance = env.services().maintenance;
    assert_eq!(maintenance.check_text(SECRET).as_deref(), Some(SECRET_RULE));
    // no length rule applies
    assert_eq!(
        maintenance.check_text(&"a plain sentence. ".repeat(1000)),
        None
    );
    let long = format!("{}{SECRET}", "a plain sentence. ".repeat(1000));
    assert_eq!(maintenance.check_text(&long).as_deref(), Some(SECRET_RULE));
    // blank text has nothing to withhold
    for blank in ["", " \n", "\u{1}"] {
        assert_eq!(maintenance.check_text(blank), None);
    }
    assert_eq!(rows(&env), before);
}
