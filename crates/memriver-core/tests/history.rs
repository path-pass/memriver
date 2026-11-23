//! What a human command reads: any memory, deleted ones too, every version with its
//! sources and its change, who cites whom, how often a memory was read -- and every
//! project at once.

mod common;

use common::{Env, body, citing, draft, refs, setup};
use memriver_core::models::changes::{Citation, Op, Usage};
use memriver_core::models::errors::CoreError;
use memriver_core::models::memory::Memory;

fn human(env: &Env, ops: &[Op]) {
    env.services().memory.apply(ops, "human", None).unwrap();
}

fn fact(env: &Env, text: &str) -> String {
    env.made(draft(&env.project.id, text).op())
}

fn ids(memories: &[Memory]) -> Vec<&str> {
    let mut ids: Vec<&str> = memories.iter().map(|memory| memory.id.as_str()).collect();
    ids.sort();
    ids
}

fn bodies(memories: &[Memory]) -> Vec<&str> {
    memories.iter().map(|memory| memory.body.as_str()).collect()
}

fn sorted<const N: usize>(mut ids: [&str; N]) -> [&str; N] {
    ids.sort();
    ids
}

#[test]
fn versions_lists_every_version_with_its_state_sources_and_change() {
    let env = setup();
    let service = env.services().memory;
    let source = fact(&env, "source");
    let memory_id = fact(&env, "v1");
    let mut edit = citing(&memory_id, 1, &[(&source, 1)]);
    if let Op::Update { body, .. } = &mut edit {
        *body = Some("v2".to_string());
    }
    let edited = service.apply(&[edit], "human", Some("cli")).unwrap();
    service
        .delete(&memory_id, 2, &env.context(), "mcp", None)
        .unwrap();
    let versions = service.versions(&memory_id).unwrap();
    let states: Vec<_> = versions
        .iter()
        .map(|version| {
            (
                version.version,
                version.body.as_str(),
                version.deleted,
                version.sources.clone(),
            )
        })
        .collect();
    let cited = refs(&[(&source, 1)]);
    assert_eq!(
        states,
        [
            (1, "v1", false, vec![]),
            (2, "v2", false, cited.clone()),
            (3, "v2", true, cited)
        ]
    );
    assert!(versions.iter().all(|version| version.memory_id == memory_id
        && version.type_.as_str() == "project"
        && version.trust.as_str() == "agent"
        && version.sync));
    // each with the change that made it, without that change's steps
    let changes: Vec<_> = versions
        .iter()
        .map(|version| version.change.clone().unwrap())
        .collect();
    let made: Vec<_> = changes
        .iter()
        .map(|change| (change.changed_by.as_str(), change.changed_via.as_deref()))
        .collect();
    assert_eq!(
        made,
        [("human", None), ("human", Some("cli")), ("mcp", None)]
    );
    assert!(changes.iter().all(|change| change.steps.is_empty()));
    let whole = service.change(&edited.change_id).unwrap().unwrap();
    assert_eq!(
        (&changes[1].change_id, &changes[1].at, changes[1].step_count),
        (&whole.change_id, &whole.at, 1)
    );
}

#[test]
fn an_imported_version_has_no_change() {
    let env = setup();
    let memory_id = fact(&env, "a fact");
    env.raw()
        .execute("UPDATE memory_versions SET change_id = NULL", [])
        .unwrap();
    let versions = env.services().memory.versions(&memory_id).unwrap();
    assert_eq!(versions.len(), 1);
    assert_eq!(versions[0].change, None);
}

#[test]
fn versions_of_an_unknown_id_is_not_found() {
    let env = setup();
    let service = env.services().memory;
    let nowhere = env.limited(|settings| settings.root = env.tmp.join("no-store"));
    for memory_id in ["zzzzzzzzzz", "nope"] {
        let not_found = CoreError::MemoryNotFound(memory_id.to_string());
        assert_eq!(service.versions(memory_id), Err(not_found.clone()));
        assert_eq!(nowhere.memory.versions(memory_id), Err(not_found.clone()));
        assert_eq!(service.show(memory_id, true), Err(not_found));
    }
}

#[test]
fn a_damaged_history_row_is_a_storage_failure() {
    for damage in [
        "UPDATE memory_versions SET body = CAST(X'80' AS TEXT)",
        "UPDATE memory_versions SET type = 'note'",
        "UPDATE memory_versions SET trust = 'nobody'",
        "UPDATE memory_versions SET tags = '[1]'",
    ] {
        let env = setup();
        let memory_id = fact(&env, "a fact");
        env.raw().execute(damage, []).unwrap();
        assert_eq!(
            env.services().memory.versions(&memory_id),
            Err(CoreError::StorageFailure),
            "{damage}"
        );
    }
}

#[test]
fn a_version_with_a_source_id_that_is_no_id_is_a_storage_failure() {
    let env = setup();
    let memory_id = fact(&env, "a fact");
    env.unkeyed()
        .execute(
            "INSERT INTO memory_sources (memory_id, version, source_id, source_version) \
             VALUES (?1, 1, CAST(X'80' AS TEXT), 1)",
            [&memory_id],
        )
        .unwrap();
    assert_eq!(
        env.services().memory.versions(&memory_id),
        Err(CoreError::StorageFailure)
    );
}

#[test]
fn show_reads_any_memory_and_a_deleted_one_only_when_asked() {
    let env = setup();
    let service = env.services().memory;
    let theirs = env.made(draft(&env.other.id, "theirs").op());
    let rule = env.made(draft(&env.global_id, "a rule").op());
    for memory_id in [&theirs, &rule] {
        assert_eq!(&service.show(memory_id, false).unwrap().id, memory_id);
    }
    human(&env, &[Op::soft_delete(&theirs, 1)]);
    assert_eq!(
        service.show(&theirs, false),
        Err(CoreError::MemoryNotFound(theirs.clone()))
    );
    let shown = service.show(&theirs, true).unwrap();
    assert_eq!(
        (
            shown.version,
            shown.body.as_str(),
            shown.deleted_at.is_some()
        ),
        (2, "theirs", true)
    );
    // showing is no reading: nothing is recorded
    assert_eq!(env.count("memory_reads"), 0);
}

#[test]
fn memories_lists_current_states_across_projects_and_global() {
    let env = setup();
    let service = env.services().memory;
    let mine = fact(&env, "mine");
    let rule = env.made(draft(&env.global_id, "g").op());
    let theirs = env.made(draft(&env.other.id, "theirs").op());
    let gone = fact(&env, "gone");
    human(&env, &[Op::soft_delete(&gone, 1)]);
    assert_eq!(
        ids(&service.memories(None, false).unwrap()),
        sorted([&mine, &rule, &theirs])
    );
    assert_eq!(
        ids(&service.memories(Some(&env.project.id), false).unwrap()),
        [mine.as_str()]
    );
    let with_deleted = service.memories(Some(&env.project.id), true).unwrap();
    assert_eq!(ids(&with_deleted), sorted([&mine, &gone]));
    // newest first: the delete was the last change
    assert_eq!(with_deleted[0].id, gone);
    assert_eq!(
        ids(&service.memories(None, true).unwrap()),
        sorted([&mine, &rule, &theirs, &gone])
    );
    assert_eq!(service.memories(Some("zzzzzzzzzz"), true), Ok(vec![]));
    // changed at the same instant: the larger id first
    env.raw()
        .execute(
            "UPDATE memories SET updated = '9999-01-01T00:00:00.000000Z' WHERE id IN (?1, ?2)",
            [&mine, &theirs],
        )
        .unwrap();
    let newest = service.memories(None, true).unwrap();
    let mut tied = [mine.as_str(), theirs.as_str()];
    tied.sort();
    assert_eq!(
        [newest[0].id.as_str(), newest[1].id.as_str()],
        [tied[1], tied[0]]
    );
    // a row memriver could not have written is passed over, not a failure
    env.raw()
        .execute("UPDATE memories SET tags = '[1]' WHERE id = ?1", [&mine])
        .unwrap();
    assert_eq!(
        ids(&service.memories(None, false).unwrap()),
        sorted([&rule, &theirs])
    );
}

#[test]
fn citing_lists_the_versions_of_other_memories_that_cite_any_version() {
    let env = setup();
    let service = env.services().memory;
    let source = fact(&env, "source");
    let derived = env.made(
        draft(&env.project.id, "citing")
            .citing(&[(&source, 1)])
            .op(),
    );
    human(&env, &[body(&derived, 1, "citing, edited")]);
    let citation = |version, current| Citation {
        memory_id: derived.clone(),
        version,
        source_version: 1,
        current,
    };
    assert_eq!(
        service.citing(&source),
        Ok(vec![citation(1, false), citation(2, true)])
    );
    for nothing in [derived.as_str(), "zzzzzzzzzz", "nope"] {
        assert_eq!(service.citing(nothing), Ok(vec![]));
    }
    // by the citing memory first, then by its version
    let another = env.made(
        draft(&env.project.id, "another")
            .citing(&[(&source, 1)])
            .op(),
    );
    human(&env, &[body(&another, 1, "another, edited")]);
    let mut citers = [derived.as_str(), another.as_str()];
    citers.sort();
    let listed: Vec<(String, i64)> = service
        .citing(&source)
        .unwrap()
        .into_iter()
        .map(|citation| (citation.memory_id, citation.version))
        .collect();
    let expected = [
        (citers[0], 1),
        (citers[0], 2),
        (citers[1], 1),
        (citers[1], 2),
    ];
    assert_eq!(
        listed,
        expected.map(|(memory_id, version)| (memory_id.to_string(), version))
    );
}

#[test]
fn usage_counts_reads_and_reports_the_stored_high_water_mark() {
    let env = setup();
    let service = env.services().memory;
    let (read_twice, never_read) = (fact(&env, "one"), fact(&env, "two"));
    for _ in 0..2 {
        service.read(&read_twice, &env.context(), "codex").unwrap();
    }
    let usage = service
        .usage(&[&read_twice, &never_read, "zzzzzzzzzz", &read_twice])
        .unwrap();
    let mut known: Vec<&String> = usage.keys().collect();
    known.sort();
    let mut expected = [&read_twice, &never_read];
    expected.sort();
    assert_eq!(known, expected);
    let marked: Option<String> = env
        .raw()
        .query_row(
            "SELECT last_read_at FROM memories WHERE id = ?1",
            [&read_twice],
            |row| row.get(0),
        )
        .unwrap();
    assert!(marked.is_some());
    assert_eq!(
        usage[&read_twice],
        Usage {
            reads: 2,
            last_read_at: marked
        }
    );
    assert_eq!(
        usage[&never_read],
        Usage {
            reads: 0,
            last_read_at: None
        }
    );
    assert!(service.usage(&[]).unwrap().is_empty());
    // the mark is the memory's own: it stands when no read fact was written with it
    service
        .read(&never_read, &env.context(), &"h".repeat(65))
        .unwrap();
    let marked = service.usage(&[&never_read]).unwrap().remove(&never_read);
    let marked = marked.unwrap();
    assert_eq!(marked.reads, 0);
    assert!(marked.last_read_at.is_some());
}

#[test]
fn search_all_ranks_across_projects_as_one_list() {
    let env = setup();
    let service = env.services().memory;
    env.made(draft(&env.project.id, "uv pip in mine").op());
    env.made(draft(&env.other.id, "pip in theirs, newest").op());
    env.made(draft(&env.global_id, "uv pip in global").op());
    env.made(draft(&env.project.id, "uv only, newest of all").op());
    assert_eq!(
        bodies(&service.search_all("uv pip", None, None).unwrap()),
        [
            "uv pip in global",
            "uv pip in mine",
            "uv only, newest of all",
            "pip in theirs, newest"
        ]
    );
    assert_eq!(
        bodies(&service.search_all("uv pip", None, Some(1)).unwrap()),
        ["uv pip in global"]
    );
    assert_eq!(
        bodies(
            &service
                .search_all("uv pip", Some(&env.other.id), None)
                .unwrap()
        ),
        ["pip in theirs, newest"]
    );
    for no_term in ["", " ,; "] {
        assert_eq!(service.search_all(no_term, None, None), Ok(vec![]));
    }
    assert_eq!(service.search_all("uv", None, Some(0)), Ok(vec![]));
    assert_eq!(
        service.search_all("uv", Some("zzzzzzzzzz"), None),
        Err(CoreError::ProjectNotFound("zzzzzzzzzz".to_string()))
    );
}

#[test]
fn list_memories_gives_every_project_with_its_current_memories() {
    let env = setup();
    let service = env.services().memory;
    let mine = fact(&env, "mine");
    let newer = fact(&env, "mine, newer");
    let rule = env.made(draft(&env.global_id, "a rule").op());
    let gone = fact(&env, "gone");
    human(&env, &[Op::soft_delete(&gone, 1)]);
    let listed = service.list_memories(None).unwrap();
    let shape: Vec<(&str, Vec<&str>)> = listed
        .iter()
        .map(|(project, memories)| {
            (
                project.name.as_str(),
                memories.iter().map(|memory| memory.id.as_str()).collect(),
            )
        })
        .collect();
    // projects by name, global last; a project's memories newest first, none deleted
    assert_eq!(
        shape,
        [
            ("elsewhere", vec![]),
            ("work", vec![newer.as_str(), mine.as_str()]),
            ("global", vec![rule.as_str()])
        ]
    );
    let one = service.list_memories(Some(&env.global_id)).unwrap();
    assert_eq!(one.len(), 1);
    assert_eq!(
        (one[0].0.id.as_str(), ids(&one[0].1)),
        (env.global_id.as_str(), vec![rule.as_str()])
    );
    assert_eq!(
        service.list_memories(Some("zzzzzzzzzz")).map(|_| ()),
        Err(CoreError::ProjectNotFound("zzzzzzzzzz".to_string()))
    );
}

#[test]
fn a_text_column_that_is_not_text_is_a_storage_failure_and_nothing_is_written() {
    let failure = CoreError::StorageFailure;
    // the three text columns of a change
    for column in ["at", "changed_by", "changed_via"] {
        let env = setup();
        let service = env.services().memory;
        let memory_id = fact(&env, "v1");
        let edit = service
            .apply(&[body(&memory_id, 1, "v2")], "human", Some("cli"))
            .unwrap();
        env.raw()
            .execute(
                &format!("UPDATE changes SET {column} = CAST(X'80' AS TEXT) WHERE change_id = ?1"),
                [&edit.change_id],
            )
            .unwrap();
        let changes = env.count("changes");
        assert_eq!(
            service.change(&edit.change_id),
            Err(failure.clone()),
            "{column}"
        );
        assert_eq!(
            service.versions(&memory_id),
            Err(failure.clone()),
            "{column}"
        );
        assert_eq!(
            service.undo(&edit.change_id, "human", None),
            Err(failure.clone().into()),
            "{column}"
        );
        assert_eq!(env.count("changes"), changes);
        assert_eq!(service.show(&memory_id, false).unwrap().version, 2);
    }
    // what a change undoes, and the memory a step names
    let env = setup();
    let service = env.services().memory;
    let memory_id = fact(&env, "v1");
    let edit = service
        .apply(&[body(&memory_id, 1, "v2")], "human", None)
        .unwrap();
    let undone = service.undo(&edit.change_id, "human", None).unwrap();
    let conn = env.unkeyed();
    conn.execute(
        "UPDATE changes SET undoes = CAST(X'80' AS TEXT) WHERE change_id = ?1",
        [&undone.change_id],
    )
    .unwrap();
    assert_eq!(service.change(&undone.change_id), Err(failure.clone()));
    assert_eq!(service.versions(&memory_id), Err(failure.clone()));
    assert_eq!(
        service.undo(&undone.change_id, "human", None),
        Err(failure.clone().into())
    );
    conn.execute(
        "UPDATE change_steps SET memory_id = CAST(X'80' AS TEXT) WHERE change_id = ?1",
        [&edit.change_id],
    )
    .unwrap();
    assert_eq!(service.change(&edit.change_id), Err(failure.clone()));
    assert_eq!(
        service.undo(&edit.change_id, "human", None),
        Err(failure.clone().into())
    );
    assert_eq!(service.show(&memory_id, false).unwrap().version, 3);
    // the memory a citation is made by
    let env = setup();
    let source = fact(&env, "source");
    env.made(
        draft(&env.project.id, "citing")
            .citing(&[(&source, 1)])
            .op(),
    );
    let conn = env.unkeyed();
    for (table, column) in [("memories", "id"), ("memory_sources", "memory_id")] {
        conn.execute(
            &format!(
                "UPDATE {table} SET {column} = CAST(X'FFFFFFFFFFFFFFFFFFFF' AS TEXT) \
                 WHERE {column} <> ?1"
            ),
            [&source],
        )
        .unwrap();
    }
    assert_eq!(env.services().memory.citing(&source), Err(failure.clone()));
    // the project of a memory a hard delete would take, and the mark of a last read
    let env = setup();
    let memory_id = fact(&env, "a fact");
    let store = env.memory_store();
    env.raw()
        .execute("UPDATE memories SET last_read_at = CAST(X'80' AS TEXT)", [])
        .unwrap();
    assert_eq!(store.usage(&[&memory_id]), Err(failure.clone()));
    env.unkeyed()
        .execute("UPDATE memories SET project_id = CAST(X'80' AS TEXT)", [])
        .unwrap();
    assert_eq!(store.plan_hard_delete(&memory_id), Err(failure.clone()));
    assert_eq!(
        store.hard_delete(
            &memory_id,
            memriver_core::models::changes::Confirmation::Code("0123456789abcdef")
        ),
        Err(failure)
    );
    assert_eq!(env.count("memories"), 1);
}
