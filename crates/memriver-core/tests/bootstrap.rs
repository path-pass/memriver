//! The services as `build_services` composes them: one store, one set of limits.

use std::fs;
use std::sync::{Arc, Mutex};

use memriver_core::build_services;
use memriver_core::content_policy::{ContentClassifier, Verdict};
use memriver_core::models::changes::Op;
use memriver_core::models::errors::{CoreError, WriteError};
use memriver_core::models::session::{Harness, SessionKey};
use memriver_core::repository::directories::DATABASE_FILENAME;
use memriver_core::settings::Settings;
use memriver_test_support::Sandbox;

#[test]
fn the_four_services_are_built_over_the_root_of_the_settings() {
    let sandbox = Sandbox::new();
    let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
    let (home, store, work) = (
        tmp.join("home"),
        tmp.join("from-settings"),
        tmp.join("work"),
    );
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(&work).unwrap();
    let services = build_services(&Settings::light(store.clone()), &home, &sandbox.env(), None);
    assert!(
        !store.exists(),
        "building reads the settings, never the store"
    );

    // what one service writes the other three read: they stand on the same store
    let global = services.project.ensure_global().unwrap();
    let plan = services.project.plan_root(&work, None).unwrap();
    let project = services.project.init_project("demo", &plan).unwrap();
    assert!(store.join(DATABASE_FILENAME).is_file());
    let context = services.project.open_project_context(&work).unwrap();
    let memory = services
        .memory
        .record("a fact", "project", true, "t", "the cue", &context)
        .unwrap();
    let key = SessionKey::new(Harness::Codex, "s-1").unwrap();
    let started = services
        .session
        .start_session(&key, "startup", work.to_str().unwrap(), None, None)
        .unwrap();
    assert_eq!(
        started.context.project.map(|p| p.id),
        Some(project.id.clone())
    );
    let report = services.maintenance.diagnose(None, 90).unwrap();
    assert_eq!(report.projects.len(), 2);
    let plan = services.maintenance.plan_hard_delete(&memory.id).unwrap();
    assert_eq!(plan.target, memory.id);
    assert_eq!(services.project.global_project_id(), Ok(Some(global)));
}

#[test]
fn the_limits_of_the_settings_reach_the_services() {
    let sandbox = Sandbox::new();
    let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
    let (home, work) = (tmp.join("home"), tmp.join("work"));
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(&work).unwrap();
    let settings = Settings {
        max_body_chars: 10,
        search_limit_default: 2,
        search_limit_max: 3,
        index_budget_lines: 4,
        ..Settings::light(tmp.join("store"))
    };
    let services = build_services(&settings, &home, &sandbox.env(), None);
    services.project.ensure_global().unwrap();
    let plan = services.project.plan_root(&work, None).unwrap();
    services.project.init_project("demo", &plan).unwrap();
    let context = services.project.open_project_context(&work).unwrap();
    let record = |body: &str, cue: &str| {
        services
            .memory
            .record(body, "project", true, "t", cue, &context)
    };
    // the body's limit is the configured one, a description keeps its own budget
    let refused = record("eleven char", "cue");
    assert!(
        matches!(
            refused,
            Err(WriteError::Core(CoreError::ContentRejected { .. }))
        ),
        "{refused:?}"
    );
    let long_cue = "a description much longer than ten characters";
    for number in 0..6 {
        record(&format!("fact {number}"), long_cue).unwrap();
    }
    let search = |limit| {
        services
            .memory
            .search("fact", limit, &context)
            .unwrap()
            .len()
    };
    assert_eq!((search(None), search(Some(99)), search(Some(1))), (2, 3, 1));
    assert_eq!(services.memory.normalize_search_limit(None), 2);
    // the index holds the budget's lines and says what it left out
    let index = services.memory.index(&context, None).unwrap();
    assert!(index.contains("more entries omitted"), "{index}");
}

#[test]
fn ensure_global_keeps_the_global_memories_that_are_there() {
    let sandbox = Sandbox::new();
    let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
    fs::create_dir_all(tmp.join("home")).unwrap();
    let settings = Settings::light(tmp.join("store"));
    let services = build_services(&settings, &tmp.join("home"), &sandbox.env(), None);
    let global = services.project.ensure_global().unwrap();
    let kept = Op::create(&global, "feedback", "a rule", "kept");
    let change = services.memory.apply(&[kept], "test", None).unwrap();
    assert_eq!(services.project.ensure_global(), Ok(global.clone()));
    let listed = services.memory.memories(Some(&global), false).unwrap();
    let ids: Vec<&str> = listed.iter().map(|memory| memory.id.as_str()).collect();
    assert_eq!(ids, [change.steps[0].memory_id.as_str()]);
}

/// Notes what it is asked and lets everything through.
struct Noting(Arc<Mutex<Vec<(String, String)>>>);

impl ContentClassifier for Noting {
    fn classify(&self, text: &str, changed_by: &str) -> Verdict {
        let asked = (text.to_string(), changed_by.to_string());
        self.0.lock().unwrap().push(asked);
        Verdict::Allowed
    }
}

#[test]
fn a_restore_and_an_undo_ask_the_classifier_nothing() {
    let sandbox = Sandbox::new();
    let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
    fs::create_dir_all(tmp.join("home")).unwrap();
    let asked = Arc::new(Mutex::new(Vec::new()));
    let classifier: Box<dyn ContentClassifier> = Box::new(Noting(Arc::clone(&asked)));
    let settings = Settings::light(tmp.join("store"));
    let services = build_services(
        &settings,
        &tmp.join("home"),
        &sandbox.env(),
        Some(classifier),
    );
    let global = services.project.ensure_global().unwrap();
    let apply = |op: Op| services.memory.apply(&[op], "test", None).unwrap();
    let created = apply(Op::create(&global, "feedback", "cue", "first text"));
    let memory_id = &created.steps[0].memory_id;
    let mut update = Op::update(memory_id, 1);
    if let Op::Update { body, .. } = &mut update {
        *body = Some("second text".to_string());
    }
    let changed = apply(update);
    let texts = |asked: &Mutex<Vec<(String, String)>>| asked.lock().unwrap().clone();
    let said = |text: &str| (text.to_string(), "test".to_string());
    assert_eq!(
        texts(&asked),
        [said("cue\n\nfirst text"), said("second text")]
    );
    // text that was stored once was asked about then: it is not asked about again
    services
        .memory
        .undo(&changed.change_id, "test", None)
        .unwrap();
    services
        .memory
        .restore(memory_id, 3, 2, "test", None)
        .unwrap();
    assert_eq!(texts(&asked).len(), 2);
    let shown = services.memory.show(memory_id, false).unwrap();
    assert_eq!((shown.version, shown.body.as_str()), (4, "second text"));
}
