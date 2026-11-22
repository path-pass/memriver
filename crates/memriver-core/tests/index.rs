//! The index an agent is given at the start of a session: global's standing rules, a
//! line per global tag, global's other entries, then the project's, in one budget.

mod common;

use common::{Env, context_of, setup};
use memriver_core::models::memory::Memory;

fn index(env: &Env) -> Vec<String> {
    lines(&env.services().memory.index(&env.context(), None).unwrap())
}

fn lines(text: &str) -> Vec<String> {
    text.split('\n').map(str::to_string).collect()
}

fn day(memory: &Memory) -> &str {
    &memory.updated[..10]
}

fn describe(env: &Env, name: &str, description: &str) {
    env.raw()
        .execute(
            "INSERT INTO tags (project_id, name, description, updated) \
             VALUES (?1, ?2, ?3, '2026-01-01T00:00:00.000000Z')",
            (&env.global_id, name, description),
        )
        .unwrap();
}

fn shown(env: &Env, id: &str) -> Memory {
    env.services()
        .memory
        .read(id, &env.context(), "codex")
        .unwrap()
}

#[test]
fn the_five_line_formats() {
    let env = setup();
    let service = env.services().memory;
    let rule = env.global("always answer\nin one line  please", Some(1), &["style"]);
    let tagged = env.global("only through its tags", Some(2), &["style", "zeta"]);
    let plain = shown(&env, &env.global("an unjudged entry", None, &[]));
    let untagged = shown(&env, &env.global("judged, without a tag", Some(3), &[]));
    let unjudged = shown(&env, &env.global("unjudged with a tag", None, &["alpha"]));
    let mine = service
        .record(
            "the body",
            "feedback",
            true,
            "codex",
            "a cue",
            &env.context(),
        )
        .unwrap();
    describe(&env, "style", "how to\twrite\nthings");
    assert_eq!(
        index(&env),
        [
            format!("- [rule, global] {rule}: always answer in one line please"),
            "- [tag, global] alpha (1 entries; memory_index tag=alpha)".to_string(),
            "- [tag, global] style: how to write things (2 entries; memory_index tag=style)"
                .to_string(),
            "- [tag, global] zeta (1 entries; memory_index tag=zeta)".to_string(),
            format!(
                "- [feedback, global] {}: unjudged with a tag ({})",
                unjudged.id,
                day(&unjudged)
            ),
            format!(
                "- [feedback, global] {}: judged, without a tag ({})",
                untagged.id,
                day(&untagged)
            ),
            format!(
                "- [feedback, global] {}: an unjudged entry ({})",
                plain.id,
                day(&plain)
            ),
            format!("- [feedback] {}: a cue ({})", mine.id, day(&mine)),
        ]
    );
    // a tag: the global entries that carry it, a standing one marked
    let style = lines(&service.index(&env.context(), Some("style")).unwrap());
    let (tagged, rule) = (shown(&env, &tagged), shown(&env, &rule));
    assert_eq!(
        style,
        [
            format!(
                "- [feedback, global] {}: only through its tags ({})",
                tagged.id,
                day(&tagged)
            ),
            format!(
                "- [feedback, global, rule] {}: always answer ({})",
                rule.id,
                day(&rule)
            ),
        ]
    );
    // without a project only global is listed
    let nowhere = context_of(None, Some(&env.global_id));
    let listed = lines(&service.index(&nowhere, None).unwrap());
    assert_eq!(listed.len(), 7);
    assert!(listed.iter().all(|line| !line.contains(&mine.id)));
}

#[test]
fn a_cue_is_one_line_of_sixty_characters() {
    let env = setup();
    let service = env.services().memory;
    let record = |body: &str, description: &str| {
        service
            .record(body, "user", true, "codex", description, &env.context())
            .unwrap()
    };
    // the description when there is one, else the body's first line, as Python
    // breaks lines; sixty characters, counted as characters
    let long = record("b", &"\u{4e2d}".repeat(61));
    let first = record("first line\u{2028}second line", "");
    let form = record("up to here\u{c}and no further", "");
    let tabs = record("b", "a\tcue\u{85}on  one line");
    let listed = index(&env);
    let cue = |memory: &Memory| {
        let line = listed
            .iter()
            .find(|line| line.contains(&memory.id))
            .unwrap();
        line.split_once(": ")
            .unwrap()
            .1
            .rsplit_once(" (")
            .unwrap()
            .0
            .to_string()
    };
    assert_eq!(cue(&long), "\u{4e2d}".repeat(60));
    assert_eq!(cue(&first), "first line");
    assert_eq!(cue(&form), "up to here");
    assert_eq!(cue(&tabs), "a cue on one line");
    assert!(listed.iter().all(|line| !line.contains('\n')));
    // newest first
    let order: Vec<bool> = [&tabs, &form, &first, &long]
        .iter()
        .zip(&listed)
        .map(|(memory, line)| line.contains(&memory.id))
        .collect();
    assert_eq!(order, [true; 4]);
}

#[test]
fn budget_cuts_project_lines_first_and_adds_the_omission_line() {
    let mut env = setup();
    env.settings.index_budget_lines = 4;
    let rule = env.global("a rule", Some(1), &[]);
    env.global("tagged", Some(2), &["tag-a"]);
    let plain = env.global("plain", None, &[]);
    let mine: Vec<Memory> = ["one", "two", "three"]
        .iter()
        .map(|body| env.record(body))
        .collect();
    let listed = index(&env);
    assert_eq!(listed.len(), 5);
    assert!(listed[0].starts_with(&format!("- [rule, global] {rule}")));
    assert!(listed[1].starts_with("- [tag, global] tag-a"));
    assert!(listed[2].contains(&plain));
    assert!(listed[3].contains(&mine[2].id));
    assert_eq!(
        listed[4],
        "\u{2026} (2 more entries omitted; use memory_search)"
    );
    // exactly the budget: nothing is said
    env.settings.index_budget_lines = 6;
    assert_eq!(index(&env).len(), 6);
    env.settings.index_budget_lines = 5;
    assert_eq!(
        index(&env)[5],
        "\u{2026} (1 more entries omitted; use memory_search)"
    );
    // a tag's entries keep the budget too
    env.settings.index_budget_lines = 1;
    env.global("tagged again", None, &["tag-a"]);
    assert_eq!(
        lines(
            &env.services()
                .memory
                .index(&env.context(), Some("tag-a"))
                .unwrap()
        )[1],
        "\u{2026} (1 more entries omitted; use memory_search)"
    );
}

#[test]
fn over_cap_rules_show_as_ordinary_entries() {
    let mut env = setup();
    let old = env.global("the older rule", Some(1), &[]);
    let tagged = env.global("an older rule with a tag", Some(1), &["tag-a"]);
    let new = env.global(&"n".repeat(30), Some(1), &[]);
    // the limits were lowered after these were stored
    env.settings.standing_max_count = 1;
    env.settings.standing_max_chars = 10;
    let listed = index(&env);
    let old = shown(&env, &old);
    assert_eq!(
        listed,
        [
            // the newest stays a rule, cut where the limit now is
            format!("- [rule, global] {new}: {}\u{2026}", "n".repeat(10)),
            "- [tag, global] tag-a (1 entries; memory_index tag=tag-a)".to_string(),
            // past the count an untagged rule is an ordinary line, a tagged one is
            // reached through its tag
            format!(
                "- [feedback, global] {}: the older rule ({})",
                old.id,
                day(&old)
            ),
        ]
    );
    assert!(listed.iter().all(|line| !line.contains(&tagged)));
}

#[test]
fn no_memories_yet() {
    let env = setup();
    let service = env.services().memory;
    assert_eq!(
        service.index(&env.context(), None).unwrap(),
        "(no memories yet)"
    );
    assert_eq!(
        service.index(&context_of(None, None), None).unwrap(),
        "(no memories yet)"
    );
    // a deleted entry is no entry
    let memory = env.record("the body");
    service.delete(&memory.id, 1, &env.context(), None).unwrap();
    assert_eq!(
        service.index(&env.context(), None).unwrap(),
        "(no memories yet)"
    );
    // another project's entries are not this one's
    env.record("mine");
    let elsewhere = context_of(Some(&env.other.id), Some(&env.global_id));
    assert_eq!(
        service.index(&elsewhere, None).unwrap(),
        "(no memories yet)"
    );
}

#[test]
fn no_such_tag() {
    let env = setup();
    let service = env.services().memory;
    env.global("tagged", None, &["tag-a"]);
    env.record("mine, and tag-b is only a word here");
    for tag in ["tag-b", "", "TAG-A", "tag-a "] {
        assert_eq!(
            service.index(&env.context(), Some(tag)).unwrap(),
            "(no such tag)",
            "{tag:?}"
        );
    }
    // a description of a name memriver could not have written is passed over
    env.raw()
        .execute_batch(&format!(
            "PRAGMA ignore_check_constraints = ON; \
             INSERT INTO tags (project_id, name, description, updated) VALUES \
             ('{0}', 'Not A Name', 'x', '2026-01-01T00:00:00.000000Z'), \
             ('{0}', 'tag-a', CAST(x'ff' AS TEXT), '2026-01-01T00:00:00.000000Z');",
            env.global_id
        ))
        .unwrap();
    assert_eq!(
        index(&env)[0],
        "- [tag, global] tag-a (1 entries; memory_index tag=tag-a)"
    );
}
