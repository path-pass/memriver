//! What every command does with a store it cannot use, and with a command line that is
//! none.

mod common;

use common::{Done, World, world};

const NEEDS_UPGRADE: &str = "this memriver needs schema version 6. Upgrade the store to version \
                             6 with an earlier memriver release first\n";

/// Every human command, each with arguments it would otherwise carry out.
fn commands(world: &World) -> Vec<Vec<String>> {
    let (memory, project) = (world.memory.id.as_str(), world.project.id.as_str());
    let (work, other) = (world.work.to_str().unwrap(), world.tmp.join("other"));
    std::fs::create_dir_all(&other).unwrap();
    let other = other.to_str().unwrap();
    let export = world.tmp.join("export");
    let lines: Vec<Vec<&str>> = vec![
        vec!["list"],
        vec!["list", "--project", project],
        vec!["show", memory],
        vec!["show", memory, "--deleted"],
        vec!["search", "line"],
        vec!["sessions"],
        vec!["sessions", "--json"],
        vec!["export", export.to_str().unwrap()],
        vec!["history", memory],
        vec!["history", memory, "--show", "1"],
        vec!["restore", memory, "--to", "1", "--yes"],
        vec!["undo", "zzzzzzzzzz", "--yes"],
        vec!["delete", memory, "--version", "1", "--yes"],
        vec!["delete", memory, "--hard", "--dry-run"],
        vec!["delete", memory, "--hard", "--confirm", "0123456789abcdef"],
        vec!["delete", memory, "--hard", "--yes"],
        vec!["project", "init", other, "--yes"],
        vec!["project", "adopt", project, other, "--yes"],
        vec!["project", "unbind", project, work, "--yes"],
        vec!["project", "explain"],
    ];
    lines
        .into_iter()
        .map(|line| line.into_iter().map(str::to_string).collect())
        .collect()
}

fn run(world: &World, line: &[String]) -> Done {
    let args: Vec<&str> = line.iter().map(String::as_str).collect();
    world.run(&args)
}

fn set_version(world: &World, version: i64) {
    let conn = world.sql();
    conn.pragma_update(None, "user_version", version).unwrap();
}

#[test]
fn a_store_below_6_gets_the_line_that_says_to_upgrade_with_an_earlier_release() {
    for version in [1, 5] {
        let world = world();
        set_version(&world, version);
        let done = world.run(&["list"]);
        let line =
            format!("memriver: the memory store is at schema version {version}; {NEEDS_UPGRADE}");
        assert_eq!(
            (done.code, done.stdout.as_str(), done.stderr),
            (1, "", line)
        );
    }
}

#[test]
fn a_v5_store_is_refused_by_every_command_and_left_byte_identical() {
    for version in [5, 1, 4] {
        let world = world();
        set_version(&world, version);
        let before = world.files();
        assert_eq!(before.len(), 1, "only the database");
        let expected =
            format!("memriver: the memory store is at schema version {version}; {NEEDS_UPGRADE}");
        for line in commands(&world) {
            let done = run(&world, &line);
            // an export looks at its target first, and that is all it does
            assert_eq!(
                (done.code, done.stdout.as_str(), done.stderr.as_str()),
                (1, "", expected.as_str()),
                "{line:?}"
            );
            assert_eq!(world.files(), before, "{line:?}");
            assert!(!world.tmp.join("export").exists(), "{line:?}");
        }
    }
}

#[test]
fn a_store_of_a_later_schema_is_a_store_that_cannot_be_read() {
    let world = world();
    set_version(&world, 7);
    let before = world.files();
    let done = world.run(&["list"]);
    assert_eq!(
        (done.code, done.stdout.as_str(), done.stderr.as_str()),
        (2, "refused: the memory store could not be read\n", "")
    );
    assert_eq!(world.files(), before);
}
