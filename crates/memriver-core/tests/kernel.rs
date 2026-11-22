//! The one place every write goes through, asked as the management path asks it: a
//! batch is written whole or not at all, sources are admitted and never circular, the
//! metadata limits hold, and a recorded state can be made current again.

mod common;

use common::{CAPS, Env, core, setup, value};
use memriver_core::models::changes::{MetadataCaps, Op, OpName, SourceRef};
use memriver_core::models::errors::{BatchConflictReason, Cap, CoreError, WriteError};

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

struct Draft {
    project_id: String,
    body: String,
    sources: Vec<SourceRef>,
    trust: String,
    sync: bool,
    level: Option<i64>,
    tags: Vec<String>,
}

fn draft(project_id: &str, body: &str) -> Draft {
    Draft {
        project_id: project_id.to_string(),
        body: body.to_string(),
        sources: Vec::new(),
        trust: "agent".to_string(),
        sync: true,
        level: None,
        tags: Vec::new(),
    }
}

impl Draft {
    fn citing(mut self, sources: &[(&str, i64)]) -> Draft {
        self.sources = refs(sources);
        self
    }

    fn trusted(mut self, trust: &str, sync: bool) -> Draft {
        (self.trust, self.sync) = (trust.to_string(), sync);
        self
    }

    fn judged(mut self, level: Option<i64>, tags: &[&str]) -> Draft {
        self.level = level;
        self.tags = tags.iter().map(|tag| tag.to_string()).collect();
        self
    }

    fn op(self) -> Op {
        Op::Create {
            project_id: self.project_id,
            type_: "project".to_string(),
            description: String::new(),
            body: self.body,
            sources: self.sources,
            trust: self.trust,
            sync: self.sync,
            level: self.level,
            tags: self.tags,
        }
    }
}

fn refs(sources: &[(&str, i64)]) -> Vec<SourceRef> {
    sources
        .iter()
        .map(|(memory_id, version)| SourceRef {
            memory_id: memory_id.to_string(),
            version: *version,
        })
        .collect()
}

fn update(memory_id: &str, expected_version: i64) -> Op {
    Op::update(memory_id, expected_version)
}

fn body(memory_id: &str, expected_version: i64, text: &str) -> Op {
    let Op::Update {
        memory_id,
        expected_version,
        description,
        sources,
        level,
        tags,
        ..
    } = update(memory_id, expected_version)
    else {
        unreachable!()
    };
    Op::Update {
        memory_id,
        expected_version,
        description,
        body: Some(text.to_string()),
        sources,
        level,
        tags,
    }
}

fn citing(memory_id: &str, expected_version: i64, sources: &[(&str, i64)]) -> Op {
    Op::Update {
        memory_id: memory_id.to_string(),
        expected_version,
        description: None,
        body: None,
        sources: Some(refs(sources)),
        level: None,
        tags: None,
    }
}

fn judged(memory_id: &str, expected_version: i64, level: Option<i64>, tags: Option<&[&str]>) -> Op {
    Op::Update {
        memory_id: memory_id.to_string(),
        expected_version,
        description: None,
        body: None,
        sources: None,
        level,
        tags: tags.map(|tags| tags.iter().map(|tag| tag.to_string()).collect()),
    }
}

fn conflict<T: std::fmt::Debug>(
    result: Result<T, WriteError>,
) -> (usize, Option<String>, BatchConflictReason) {
    match core(result) {
        CoreError::BatchConflict {
            index,
            memory_id,
            reason,
        } => (index, memory_id, reason),
        other => panic!("not a conflict: {other:?}"),
    }
}

fn state(env: &Env, memory_id: &str) -> (i64, String, String, i64, Option<String>) {
    env.raw()
        .query_row(
            "SELECT version, body, trust, sync, deleted_at FROM memories WHERE id = ?1",
            [memory_id],
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
        .unwrap()
}

#[test]
fn a_batch_is_one_change_with_a_step_for_each_operation_or_nothing() {
    let env = setup();
    let first = env.manage(draft(&env.project.id, "first").op());
    let second = env.manage(draft(&env.project.id, "second").op());
    let change = env
        .apply(&[
            body(&first, 1, "first, reworded"),
            draft(&env.other.id, "third").op(),
            Op::soft_delete(&second, 1),
        ])
        .unwrap();
    assert_eq!(
        (
            change.changed_by.as_str(),
            &change.changed_via,
            change.step_count
        ),
        ("cli", &None, 3)
    );
    let steps: Vec<_> = change
        .steps
        .iter()
        .map(|step| (step.step, step.op, step.before_version, step.after_version))
        .collect();
    assert_eq!(
        steps,
        [
            (1, OpName::Update, Some(1), 2),
            (2, OpName::Create, None, 1),
            (3, OpName::SoftDelete, Some(1), 2)
        ]
    );
    assert_eq!(
        (&change.steps[0].memory_id, &change.steps[2].memory_id),
        (&first, &second)
    );
    assert_eq!(rows(&env), [3, 5, 3, 5, 0]);
    // written by whoever the change names, through nothing that has a name
    let source: (String, String) = env
        .raw()
        .query_row(
            "SELECT source_harness, source_method FROM memories WHERE id = ?1",
            [&change.steps[1].memory_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(source, ("unknown".to_string(), "cli".to_string()));
    // one refused operation refuses them all, and names itself
    let before = rows(&env);
    let refused = env.apply(&[
        body(&first, 2, "written, then taken back"),
        draft(&env.project.id, "never stored").op(),
        Op::soft_delete(&second, 1),
    ]);
    assert_eq!(
        conflict(refused),
        (2, Some(second.clone()), BatchConflictReason::Version)
    );
    assert_eq!(rows(&env), before);
    assert_eq!(state(&env, &first).1, "first, reworded");
    // the reasons a named memory is refused for
    let missing = "zzzzzzzzzz";
    for (op, reason) in [
        (body(missing, 1, "b"), BatchConflictReason::Missing),
        (body("not an id", 1, "b"), BatchConflictReason::Missing),
        (body(&second, 2, "b"), BatchConflictReason::Deleted),
        (Op::soft_delete(&second, 2), BatchConflictReason::Deleted),
        (
            body(&first, 2, "first, reworded"),
            BatchConflictReason::SameState,
        ),
        (update(&first, 2), BatchConflictReason::SameState),
    ] {
        let (index, _, found) = conflict(env.apply(std::slice::from_ref(&op)));
        assert_eq!((index, found), (0, reason), "{op:?}");
    }
    assert_eq!(rows(&env), before);
    // a project that is not there is named; global is written like any other
    assert_eq!(
        core(env.apply(&[draft("yyyyyyyyyy", "b").op()])),
        CoreError::ProjectNotFound("yyyyyyyyyy".to_string())
    );
    env.manage(draft(&env.global_id, "shared").op());
}

#[test]
fn what_is_no_change_at_all_is_an_error_of_the_caller() {
    let env = setup();
    let memory = env.manage(draft(&env.project.id, "b").op());
    let store = env.memory_store();
    let apply = |ops: &[Op], by: &str, via: Option<&str>| {
        value(store.apply(ops, by, via, &|_, _| None, &CAPS))
    };
    let one = [body(&memory, 1, "c")];
    assert_eq!(
        apply(&[], "cli", None),
        "a change needs at least one operation"
    );
    for by in ["", &"b".repeat(33)] {
        assert_eq!(
            apply(&one, by, None),
            "changed_by is not 1 to 32 characters"
        );
    }
    for via in ["", &"v".repeat(65)] {
        assert_eq!(
            apply(&one, "cli", Some(via)),
            "changed_via is not 1 to 64 characters"
        );
    }
    assert_eq!(
        apply(
            &[body(&memory, 1, "c"), Op::soft_delete(&memory, 2)],
            "cli",
            None
        ),
        "a change touches a memory at most once"
    );
    assert_eq!(
        apply(
            &[draft(&env.project.id, "b")
                .citing(&[(&memory, 1), (&memory, 1)])
                .op()],
            "cli",
            None
        ),
        "a source set cites a memory at most once"
    );
    assert_eq!(
        apply(
            &[draft(&env.project.id, "b").judged(Some(0), &[]).op()],
            "cli",
            None
        ),
        "a level is None or an integer of at least 1"
    );
    assert_eq!(
        apply(
            &[draft(&env.project.id, "b")
                .judged(None, &["Not A Tag"])
                .op()],
            "cli",
            None
        ),
        "a tag name is 1 to 32 of a-z, 0-9 and single inner hyphens"
    );
    assert_eq!(
        apply(
            &[Op::SoftDelete {
                memory_id: memory.clone(),
                expected_version: 1,
                unread_since: Some("yesterday".to_string()),
            }],
            "cli",
            None
        ),
        "unread_since is not a timestamp"
    );
    // the limits count characters: 32 and 64 of them are allowed
    store
        .apply(
            &one,
            &"\u{4e2d}".repeat(32),
            Some(&"\u{4e2d}".repeat(64)),
            &|_, _| None,
            &CAPS,
        )
        .unwrap();
    assert_eq!(rows(&env), [1, 2, 2, 2, 0]);
}

#[test]
fn a_source_is_cited_at_its_current_version_and_lends_its_trust() {
    let env = setup();
    let user = env.manage(
        draft(&env.project.id, "from the user")
            .trusted("user", true)
            .op(),
    );
    let agent = env.manage(
        draft(&env.project.id, "from an agent")
            .trusted("agent", false)
            .op(),
    );
    // trust is the lowest of the sources', sync only when all sync; what the create
    // itself says is not asked
    let derived = env.manage(
        draft(&env.global_id, "derived")
            .trusted("user", true)
            .citing(&[(&user, 1), (&agent, 1)])
            .op(),
    );
    assert_eq!(state(&env, &derived).2, "agent");
    assert_eq!(state(&env, &derived).3, 0);
    let from_user = env.manage(
        draft(&env.global_id, "derived from the user alone")
            .trusted("untrusted-derived", false)
            .citing(&[(&user, 1)])
            .op(),
    );
    assert_eq!(
        (
            state(&env, &from_user).2.as_str(),
            state(&env, &from_user).3
        ),
        ("user", 1)
    );
    // one source that does not sync is enough, wherever it stands among them: ids are
    // drawn at random, so a syncing source is made until one sorts after `agent`, and
    // another until one sorts before it
    let syncing = |after: bool| loop {
        let made = env.manage(draft(&env.project.id, "syncs").trusted("user", true).op());
        if (made > agent) == after {
            return made;
        }
    };
    for other in [syncing(true), syncing(false)] {
        let mixed = env.manage(
            draft(&env.global_id, "mixed")
                .citing(&[(&other, 1), (&agent, 1)])
                .op(),
        );
        assert_eq!(state(&env, &mixed).3, 0);
    }
    // stored sorted by the cited id, whatever the order given
    let mut sorted = [user.clone(), agent.clone()];
    sorted.sort();
    let stored: Vec<String> = env
        .raw()
        .prepare("SELECT source_id FROM memory_sources WHERE memory_id = ?1 ORDER BY rowid")
        .unwrap()
        .query_map([&derived], |row| row.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(stored, sorted);
    // a version that is not the source's current one, a deleted source, one that is
    // not there, and the memory itself
    env.manage(body(&agent, 1, "from an agent, reworded"));
    let gone = env.manage(draft(&env.project.id, "gone").op());
    env.manage(Op::soft_delete(&gone, 1));
    for (sources, reason) in [
        (vec![(agent.as_str(), 1)], BatchConflictReason::Source),
        (vec![(agent.as_str(), 3)], BatchConflictReason::Source),
        (vec![(gone.as_str(), 2)], BatchConflictReason::Source),
        (vec![("zzzzzzzzzz", 1)], BatchConflictReason::Source),
    ] {
        let refused = env.apply(&[draft(&env.project.id, "b").citing(&sources).op()]);
        assert_eq!(conflict(refused), (0, None, reason), "{sources:?}");
    }
    assert_eq!(
        conflict(env.apply(&[citing(&user, 1, &[(&user, 1)])])),
        (0, Some(user.clone()), BatchConflictReason::Cycle)
    );
    // a reference already held is carried, whatever its source did since; a new one
    // must be current. The trust is weighed again from what is cited
    let kept = env
        .apply(&[citing(&derived, 1, &[(&user, 1), (&agent, 1)])])
        .unwrap_err();
    assert!(matches!(
        kept,
        WriteError::Core(CoreError::BatchConflict {
            reason: BatchConflictReason::SameState,
            ..
        })
    ));
    env.manage(citing(&derived, 1, &[(&agent, 1)]));
    assert_eq!(
        conflict(env.apply(&[citing(&derived, 2, &[(&agent, 1), (&user, 2)])])).2,
        BatchConflictReason::Source
    );
    // a source written earlier in the same change is cited as it stood before it
    let fresh = env.manage(draft(&env.project.id, "fresh").op());
    let refused = env.apply(&[
        body(&fresh, 1, "fresh, reworded"),
        draft(&env.project.id, "cites the new version")
            .citing(&[(&fresh, 2)])
            .op(),
    ]);
    assert_eq!(conflict(refused).2, BatchConflictReason::Source);
    env.apply(&[
        body(&fresh, 1, "fresh, reworded"),
        draft(&env.project.id, "cites the old version")
            .citing(&[(&fresh, 1)])
            .op(),
    ])
    .unwrap();
}

#[test]
fn no_chain_of_sources_leads_a_memory_back_to_itself() {
    let env = setup();
    let a = env.manage(draft(&env.project.id, "a").op());
    let b = env.manage(draft(&env.project.id, "b").citing(&[(&a, 1)]).op());
    let c = env.manage(draft(&env.project.id, "c").citing(&[(&b, 1)]).op());
    // a -> c -> b -> a, through versions that are no longer current too
    assert_eq!(
        conflict(env.apply(&[citing(&a, 1, &[(&c, 1)])])),
        (0, Some(a.clone()), BatchConflictReason::Cycle)
    );
    env.manage(citing(&b, 1, &[]));
    assert_eq!(
        conflict(env.apply(&[citing(&a, 1, &[(&c, 1)])])).2,
        BatchConflictReason::Cycle
    );
    // two updates of one change that close a circle between them
    let x = env.manage(draft(&env.project.id, "x").op());
    let y = env.manage(draft(&env.project.id, "y").op());
    let before = rows(&env);
    let refused = env.apply(&[citing(&x, 1, &[(&y, 1)]), citing(&y, 1, &[(&x, 1)])]);
    assert_eq!(conflict(refused).2, BatchConflictReason::Cycle);
    assert_eq!(rows(&env), before);
}

#[test]
fn a_change_never_breaks_a_metadata_limit_nor_grows_a_breach() {
    let env = setup();
    let caps = |count, chars, tags, per| MetadataCaps {
        standing_max_count: count,
        standing_max_chars: chars,
        tags_max: tags,
        tags_per_memory_max: per,
    };
    let store = env.memory_store();
    let apply = |ops: &[Op], caps: &MetadataCaps| store.apply(ops, "cli", None, &|_, _| None, caps);
    let exceeded = |result: Result<_, WriteError>| match core(result) {
        CoreError::CapExceeded { cap, memory_id } => (cap, memory_id),
        other => panic!("not a limit: {other:?}"),
    };
    let global =
        |body: &str, level, tags: &[&str]| draft(&env.global_id, body).judged(level, tags).op();
    let made = |change: memriver_core::models::changes::Change| change.steps[0].memory_id.clone();
    let wide = caps(2, 10, 3, 2);
    // the names one memory carries
    assert_eq!(
        exceeded(apply(&[global("b", None, &["a", "b", "c"])], &wide)).0,
        Cap::TagsPerMemoryMax
    );
    let two = made(apply(&[global("two tags", None, &["a", "b"])], &wide).unwrap());
    // the body of a standing rule, in characters; of no other entry
    assert_eq!(
        exceeded(apply(
            &[global(&"\u{4e2d}".repeat(11), Some(1), &[])],
            &wide
        ))
        .0,
        Cap::StandingMaxChars
    );
    let rule = made(apply(&[global(&"\u{4e2d}".repeat(10), Some(1), &[])], &wide).unwrap());
    apply(&[global(&"x".repeat(11), Some(2), &[])], &wide).unwrap();
    // the standing rules of one project
    apply(&[global("rule two", Some(1), &[])], &wide).unwrap();
    assert_eq!(
        exceeded(apply(&[global("rule 3", Some(1), &[])], &wide)),
        (Cap::StandingMaxCount, None)
    );
    // another project has its own count
    apply(
        &[draft(&env.project.id, "theirs").judged(Some(1), &[]).op()],
        &wide,
    )
    .unwrap();
    // the distinct names of one project
    apply(&[global("third name", None, &["c"])], &wide).unwrap();
    assert_eq!(
        exceeded(apply(&[global("fourth name", None, &["d"])], &wide)),
        (Cap::TagsMax, None)
    );
    apply(&[global("a name again", None, &["a", "c"])], &wide).unwrap();
    // a limit lowered since: what is over it may stay and may shrink, never grow
    let tight = caps(1, 5, 1, 1);
    apply(&[body(&two, 1, "two tags, reworded")], &tight).unwrap();
    apply(&[judged(&two, 2, None, Some(&["a"]))], &tight).unwrap();
    assert_eq!(
        exceeded(apply(&[judged(&two, 3, None, Some(&["a", "b"]))], &tight)),
        (Cap::TagsPerMemoryMax, Some(two.clone()))
    );
    apply(&[body(&rule, 1, &"\u{4e2d}".repeat(9))], &tight).unwrap();
    assert_eq!(
        exceeded(apply(&[body(&rule, 2, &"\u{4e2d}".repeat(10))], &tight)),
        (Cap::StandingMaxChars, Some(rule.clone()))
    );
    assert_eq!(
        exceeded(apply(&[global("r", Some(1), &[])], &tight)).0,
        Cap::StandingMaxCount
    );
    assert_eq!(
        exceeded(apply(&[global("fifth", None, &["e"])], &tight)).0,
        Cap::TagsMax
    );
    // a rule deleted and another made in one change: the count does not rise, for a
    // deleted rule is no rule
    let swapped = apply(
        &[Op::soft_delete(&rule, 2), global("rule 3", Some(1), &[])],
        &wide,
    )
    .unwrap();
    assert_eq!(swapped.step_count, 2);
    assert_eq!(
        exceeded(apply(&[global("rule 4", Some(1), &[])], &wide)).0,
        Cap::StandingMaxCount
    );
    // a deleted state is held to no limit of its own: a memory goes back to a deleted
    // version that carried more names, and a longer rule, than the limits now allow
    let tags = made(apply(&[global("had two tags", None, &["a", "c"])], &wide).unwrap());
    apply(&[Op::soft_delete(&tags, 1)], &wide).unwrap();
    let restore = |memory_id: &str, expected_version, to_version| Op::Restore {
        memory_id: memory_id.to_string(),
        expected_version,
        to_version,
    };
    apply(&[restore(&tags, 2, 1)], &wide).unwrap();
    apply(&[judged(&tags, 3, None, Some(&["a"]))], &wide).unwrap();
    apply(&[restore(&tags, 4, 2)], &tight).unwrap();
    assert_eq!(
        exceeded(apply(&[restore(&tags, 5, 1)], &tight)),
        (Cap::TagsPerMemoryMax, Some(tags.clone()))
    );
    // `rule` is deleted at version 3 with nine characters; alive it is over the limit
    assert_eq!(
        exceeded(apply(&[restore(&rule, 3, 2)], &tight)).0,
        Cap::StandingMaxChars
    );
}

#[test]
fn a_recorded_state_is_made_current_again() {
    let env = setup();
    let source = env.manage(
        draft(&env.project.id, "a source")
            .trusted("user", true)
            .op(),
    );
    let memory = env.manage(
        draft(&env.project.id, "version one")
            .judged(Some(2), &["a-tag"])
            .citing(&[(&source, 1)])
            .op(),
    );
    env.manage(Op::Update {
        memory_id: memory.clone(),
        expected_version: 1,
        description: Some("a cue".to_string()),
        body: Some("version two".to_string()),
        sources: Some(Vec::new()),
        level: Some(3),
        tags: Some(Vec::new()),
    });
    env.manage(Op::soft_delete(&memory, 2));
    let restore = |expected_version, to_version| Op::Restore {
        memory_id: memory.clone(),
        expected_version,
        to_version,
    };
    // a deleted memory is restored, to the whole state of the version named
    let change = env.apply(&[restore(3, 1)]).unwrap();
    assert_eq!(
        (
            change.steps[0].op,
            change.steps[0].before_version,
            change.steps[0].after_version
        ),
        (OpName::Restore, Some(3), 4)
    );
    let row: (String, String, Option<i64>, String, Option<String>) = env
        .raw()
        .query_row(
            "SELECT description, body, level, tags, deleted_at FROM memories WHERE id = ?1",
            [&memory],
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
        row,
        (
            String::new(),
            "version one".to_string(),
            Some(2),
            "[\"a-tag\"]".to_string(),
            None
        )
    );
    let cited: Vec<(String, i64)> = env
        .raw()
        .prepare(
            "SELECT source_id, source_version FROM memory_sources \
             WHERE memory_id = ?1 AND version = 4",
        )
        .unwrap()
        .query_map([&memory], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(cited, [(source.clone(), 1)]);
    // to a deleted state, too; to the state it is in, never; to a version that is none
    env.manage(restore(4, 3));
    assert!(state(&env, &memory).4.is_some());
    assert_eq!(
        conflict(env.apply(&[restore(5, 3)])).2,
        BatchConflictReason::SameState
    );
    assert_eq!(
        conflict(env.apply(&[restore(5, 9)])).2,
        BatchConflictReason::Missing
    );
    assert_eq!(
        conflict(env.apply(&[restore(4, 1)])).2,
        BatchConflictReason::Version
    );
    // a history row no version of memriver writes is damage, not a state
    env.raw()
        .execute(
            "UPDATE memory_versions SET trust = 'bogus' WHERE memory_id = ?1 AND version = 2",
            [&memory],
        )
        .unwrap();
    assert_eq!(core(env.apply(&[restore(5, 2)])), CoreError::StorageFailure);
}

#[test]
fn a_memory_read_since_it_was_listed_is_not_deleted() {
    let env = setup();
    let memory = env.manage(draft(&env.project.id, "b").op());
    let unread = |since: &str| Op::SoftDelete {
        memory_id: memory.clone(),
        expected_version: 1,
        unread_since: Some(since.to_string()),
    };
    let read_at = "2026-05-05T05:05:05.000000Z";
    env.memory_store()
        .touch_read(&memory, read_at, 1, "codex", None)
        .unwrap();
    for since in ["2026-05-05T05:05:04.999999Z", read_at] {
        assert_eq!(
            conflict(env.apply(&[unread(since)])),
            (0, Some(memory.clone()), BatchConflictReason::ReadSince),
            "{since}"
        );
    }
    env.manage(unread("2026-05-05T05:05:05.000001Z"));
    // never read: any time will do
    let never = env.manage(draft(&env.project.id, "never read").op());
    env.manage(Op::SoftDelete {
        memory_id: never,
        expected_version: 1,
        unread_since: Some("2000-01-01T00:00:00.000000Z".to_string()),
    });
}

#[test]
fn every_resulting_state_passes_the_policy_of_today() {
    let env = setup();
    let memory = env.manage(draft(&env.project.id, "the body").op());
    let store = env.memory_store();
    let strict = |description: &str, body: &str| {
        (body.contains("body") || description.contains("cue")).then(|| "strict".to_string())
    };
    let before = rows(&env);
    let refused = |ops: &[Op]| match core(store.apply(ops, "cli", None, &strict, &CAPS)) {
        CoreError::ContentRejected {
            message,
            rule_id,
            memory_id,
            detail,
        } => {
            assert_eq!((message.as_str(), detail.as_str()), ("", ""));
            (rule_id, memory_id)
        }
        other => panic!("not a content refusal: {other:?}"),
    };
    assert_eq!(
        refused(&[draft(&env.project.id, "a body").op()]),
        ("strict".to_string(), None)
    );
    // the state is what is checked, not what the operation names: a delete, a change
    // of the description alone
    for op in [
        Op::soft_delete(&memory, 1),
        Op::Update {
            memory_id: memory.clone(),
            expected_version: 1,
            description: Some("d".to_string()),
            body: None,
            sources: None,
            level: None,
            tags: None,
        },
    ] {
        assert_eq!(refused(&[op]), ("strict".to_string(), Some(memory.clone())));
    }
    assert_eq!(rows(&env), before);
    store
        .apply(
            &[body(&memory, 1, "other text")],
            "cli",
            None,
            &strict,
            &CAPS,
        )
        .unwrap();
}

#[test]
fn an_update_sets_what_it_names_and_a_delete_changes_nothing_else() {
    let env = setup();
    let memory = env.manage(
        draft(&env.global_id, "the body")
            .judged(Some(2), &["b-tag", "a-tag"])
            .op(),
    );
    let stored = |version: i64| -> (Option<i64>, String, Option<i64>, String, String, i64) {
        env.raw()
            .query_row(
                "SELECT m.level, m.tags, v.level, v.tags, v.body, v.deleted FROM memories m \
                 JOIN memory_versions v ON v.memory_id = m.id WHERE m.id = ?1 AND v.version = ?2",
                (&memory, version),
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
            .unwrap()
    };
    let both = "[\"a-tag\",\"b-tag\"]".to_string();
    let body_of = |text: &str| text.to_string();
    assert_eq!(
        stored(1),
        (
            Some(2),
            both.clone(),
            Some(2),
            both.clone(),
            body_of("the body"),
            0
        )
    );
    // a level that is named replaces, the tags that are not named stay
    env.manage(judged(&memory, 1, Some(1), None));
    assert_eq!(
        stored(2),
        (
            Some(1),
            both.clone(),
            Some(1),
            both.clone(),
            body_of("the body"),
            0
        )
    );
    // the same level again is no change
    assert_eq!(
        conflict(env.apply(&[judged(&memory, 2, Some(1), None)])).2,
        BatchConflictReason::SameState
    );
    // no level named keeps the level: a level is never taken back to "not judged";
    // tags that are named replace the whole set, and an empty set clears it
    env.manage(judged(&memory, 2, None, Some(&["c-tag"])));
    assert_eq!(stored(3).0, Some(1));
    assert_eq!(stored(3).1, "[\"c-tag\"]");
    env.manage(judged(&memory, 3, None, Some(&[])));
    assert_eq!((stored(4).0, stored(4).1.as_str()), (Some(1), "[]"));
    // a change of the text keeps both
    env.manage(judged(&memory, 4, Some(3), Some(&["d-tag"])));
    env.manage(body(&memory, 5, "another body"));
    let d = "[\"d-tag\"]".to_string();
    assert_eq!(
        stored(6),
        (
            Some(3),
            d.clone(),
            Some(3),
            d.clone(),
            body_of("another body"),
            0
        )
    );
    // and so does a delete: the deleted version records the state it deleted
    env.manage(Op::soft_delete(&memory, 6));
    assert_eq!(
        stored(7),
        (Some(3), d.clone(), Some(3), d, body_of("another body"), 1)
    );
}
