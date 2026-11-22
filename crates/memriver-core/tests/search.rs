//! Searching memories by keyword: the project first, then global, in one budget.

mod common;

use common::{Env, context_of, setup};
use memriver_core::models::memory::Memory;

fn bodies(found: &[Memory]) -> Vec<&str> {
    found.iter().map(|memory| memory.body.as_str()).collect()
}

fn described(env: &Env, description: &str, body: &str) -> Memory {
    env.services()
        .memory
        .record(body, "project", true, "codex", description, &env.context())
        .unwrap()
}

#[test]
fn limit_is_one_budget_project_first() {
    let env = setup();
    let service = env.services().memory;
    for body in ["apple one", "apple two", "apple three"] {
        env.record(body);
    }
    for body in ["apple shared one", "apple shared two"] {
        env.global(body, None, &[]);
    }
    let found = |limit| service.search("apple", limit, &env.context()).unwrap();
    // all of the project's hits come before any of global's, each newest first
    assert_eq!(
        bodies(&found(Some(50))),
        [
            "apple three",
            "apple two",
            "apple one",
            "apple shared two",
            "apple shared one"
        ]
    );
    assert_eq!(
        bodies(&found(Some(4))),
        ["apple three", "apple two", "apple one", "apple shared two"]
    );
    // a budget the project spends leaves nothing for global
    assert_eq!(
        bodies(&found(Some(3))),
        ["apple three", "apple two", "apple one"]
    );
    assert_eq!(bodies(&found(Some(2))), ["apple three", "apple two"]);
    // no limit is the default of five, and the largest is the setting's
    assert_eq!(found(None).len(), 5);
    env.record("apple four");
    assert_eq!(found(None).len(), 5);
    assert_eq!(found(Some(1_000_000)).len(), 6);
    // without a project only global is searched; another project's are never seen
    let nowhere = context_of(None, Some(&env.global_id));
    assert_eq!(
        bodies(&service.search("apple", Some(50), &nowhere).unwrap()),
        ["apple shared two", "apple shared one"]
    );
    let elsewhere = context_of(Some(&env.other.id), Some(&env.global_id));
    assert_eq!(
        service.search("apple", Some(50), &elsewhere).unwrap().len(),
        2
    );
    assert_eq!(
        service
            .search("apple", Some(50), &context_of(None, None))
            .unwrap(),
        Vec::new()
    );
    // a project the store does not hold has no entries; global's are still found
    let unknown = context_of(Some("yyyyyyyyyy"), Some(&env.global_id));
    assert_eq!(
        service.search("apple", Some(50), &unknown).unwrap().len(),
        2
    );
}

#[test]
fn zero_and_negative_limits_become_one() {
    let mut env = setup();
    env.settings.search_limit_default = 3;
    env.settings.search_limit_max = 7;
    let service = env.services().memory;
    for (limit, clamped) in [
        (None, 3),
        (Some(0), 1),
        (Some(-1), 1),
        (Some(i64::MIN), 1),
        (Some(1), 1),
        (Some(7), 7),
        (Some(8), 7),
        (Some(i64::MAX), 7),
    ] {
        assert_eq!(service.normalize_search_limit(limit), clamped, "{limit:?}");
    }
    for body in ["pear a", "pear b"] {
        env.record(body);
    }
    for limit in [0, -5] {
        assert_eq!(
            bodies(&service.search("pear", Some(limit), &env.context()).unwrap()),
            ["pear b"]
        );
    }
}

#[test]
fn rank_orders_phrase_then_terms_then_description_then_newest() {
    let env = setup();
    let service = env.services().memory;
    // oldest first; the ranks are told apart by the query "blue whale"
    described(&env, "", "one term only: blue");
    described(&env, "", "both terms apart: whale and blue");
    described(&env, "blue", "a term in the cue, whale in the body");
    described(&env, "", "the phrase: blue whale");
    described(&env, "", "both again apart: blue, whale");
    described(&env, "", "nothing of it");
    let found = service
        .search("blue whale", Some(50), &env.context())
        .unwrap();
    assert_eq!(
        bodies(&found),
        [
            // the whole query
            "the phrase: blue whale",
            // both terms, one of them in the cue
            "a term in the cue, whale in the body",
            // both terms, newest first
            "both again apart: blue, whale",
            "both terms apart: whale and blue",
            "one term only: blue",
        ]
    );
    // a comma separates terms and is no part of a phrase
    let comma = service
        .search("blue,whale", Some(1), &env.context())
        .unwrap();
    assert_eq!(bodies(&comma), ["the phrase: blue whale"]);
    // folded beyond ASCII, and never matched on the id
    let folded = described(&env, "", "STRASSE und \u{ff22}\u{ff4c}\u{ff55}\u{ff45}");
    assert_eq!(
        bodies(&service.search("stra\u{df}e", None, &env.context()).unwrap()),
        [folded.body.as_str()]
    );
    assert_eq!(
        service
            .search(&folded.id, None, &env.context())
            .unwrap()
            .len(),
        0
    );
}

#[test]
fn no_terms_no_hits() {
    let env = setup();
    let service = env.services().memory;
    env.record("anything at all");
    for query in ["", "   ", "\0", ",;\u{3001}", "\u{3000}\n"] {
        assert_eq!(
            service.search(query, Some(50), &env.context()).unwrap(),
            Vec::new(),
            "{query:?}"
        );
    }
    assert_eq!(
        service.search("nowhere", None, &env.context()).unwrap(),
        Vec::new()
    );
    // a deleted memory is not found, a damaged row is passed over
    let deleted = env.record("anything deleted");
    let damaged = env.record("anything damaged");
    service
        .delete(&deleted.id, 1, &env.context(), None)
        .unwrap();
    env.raw()
        .execute_batch(&format!(
            "PRAGMA ignore_check_constraints = ON; \
             UPDATE memories SET type = 'bogus' WHERE id = '{}';",
            damaged.id
        ))
        .unwrap();
    assert_eq!(
        bodies(&service.search("anything", None, &env.context()).unwrap()),
        ["anything at all"]
    );
    env.raw()
        .execute(
            "UPDATE memories SET body = CAST(x'ff' AS TEXT) WHERE id = ?1",
            [&damaged.id],
        )
        .unwrap();
    assert_eq!(
        service
            .search("anything", None, &env.context())
            .unwrap()
            .len(),
        1
    );
    // the index passes it over as well
    let index = service.index(&env.context(), None).unwrap();
    assert_eq!(index.lines().count(), 1);
    assert!(index.contains(": anything at all ("));
}

#[test]
fn limits_at_their_largest_value_mean_no_limit() {
    let mut env = setup();
    env.settings.max_body_chars = usize::MAX;
    env.settings.search_limit_max = usize::MAX;
    env.settings.search_limit_default = usize::MAX;
    env.settings.index_budget_lines = usize::MAX;
    env.settings.standing_max_count = usize::MAX;
    env.settings.standing_max_chars = usize::MAX;
    env.settings.tags_max = usize::MAX;
    env.settings.tags_per_memory_max = usize::MAX;
    let service = env.services().memory;
    let long = "long ".repeat(4000);
    let written = service
        .record(&long, "user", true, "codex", "", &env.context())
        .unwrap();
    assert_eq!(written.body.chars().count(), 4000 * 5 - 1);
    for n in 0..60 {
        env.record(&format!("long entry {n}"));
    }
    env.global("long and shared", Some(1), &["a-tag"]);
    assert_eq!(service.normalize_search_limit(None), usize::MAX);
    assert_eq!(
        service.normalize_search_limit(Some(i64::MAX)),
        i64::MAX as usize
    );
    for limit in [None, Some(i64::MAX)] {
        assert_eq!(
            service.search("long", limit, &env.context()).unwrap().len(),
            62,
            "{limit:?}"
        );
    }
    assert_eq!(
        service
            .search("long", Some(3), &env.context())
            .unwrap()
            .len(),
        3
    );
    let index = service.index(&env.context(), None).unwrap();
    assert_eq!(index.lines().count(), 63);
    assert!(!index.contains("omitted"));
    assert!(index.starts_with("- [rule, global] "));
    assert!(index.lines().next().unwrap().ends_with(": long and shared"));
}
