//! Core settings: precedence, the accepted spellings, and the error sentences.

use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};

use memriver_core::settings::{
    Env, Settings, SettingsError, load_settings, settings_file, storage_root,
};
use memriver_test_support::{Sandbox, isolated_command};

const FILE: &str = "settings.toml";

/// A sandbox whose store directory exists, with `text` as its settings file.
fn store(text: Option<&str>) -> Sandbox {
    let sandbox = Sandbox::new();
    fs::create_dir(&sandbox.root).unwrap();
    if let Some(text) = text {
        fs::write(sandbox.root.join(FILE), text).unwrap();
    }
    sandbox
}

fn env(pairs: &[(&str, &str)]) -> Env {
    pairs
        .iter()
        .map(|(name, value)| (name.to_string(), value.to_string()))
        .collect()
}

fn load(sandbox: &Sandbox, pairs: &[(&str, &str)]) -> Result<Settings, SettingsError> {
    load_settings(Some(&sandbox.root), &env(pairs), &sandbox.home)
}

fn sentence(sandbox: &Sandbox, pairs: &[(&str, &str)]) -> String {
    load(sandbox, pairs)
        .expect_err("the settings are refused")
        .to_string()
}

#[test]
fn defaults_when_no_file() {
    let sandbox = Sandbox::new();
    // no store directory at all, then a store without the file, then an empty file
    let absent = load_settings(None, &Env::new(), &sandbox.home).unwrap();
    assert_eq!(absent, Settings::light(sandbox.home.join("agent-memory")));
    assert_eq!(
        (
            absent.max_body_chars,
            absent.search_limit_max,
            absent.search_limit_default
        ),
        (8000, 50, 5)
    );
    assert_eq!(
        (
            absent.index_budget_lines,
            absent.memory_reads_retention_days
        ),
        (100, None)
    );
    assert_eq!(
        (
            absent.standing_max_count,
            absent.standing_max_chars,
            absent.tags_max,
            absent.tags_per_memory_max
        ),
        (10, 200, 12, 3)
    );
    for text in [None, Some(""), Some("# nothing\n")] {
        let sandbox = store(text);
        assert_eq!(
            load(&sandbox, &[]).unwrap(),
            Settings::light(sandbox.root.clone())
        );
    }
    let nowhere = sandbox.dir.path().join("nowhere");
    assert_eq!(
        load_settings(Some(&nowhere), &Env::new(), &sandbox.home).unwrap(),
        Settings::light(nowhere)
    );
}

#[test]
fn the_file_is_read_and_found_under_the_environment_root() {
    let sandbox = store(Some(
        "max_body_chars = 42\nindex_budget_lines = 7\nmemory_reads_retention_days = 60\n",
    ));
    let settings = load(&sandbox, &[]).unwrap();
    assert_eq!(
        (settings.max_body_chars, settings.index_budget_lines),
        (42, 7)
    );
    assert_eq!(settings.memory_reads_retention_days, Some(60));
    assert_eq!(settings.search_limit_default, 5);
    // without an override the root comes from the environment, and its file is read
    let root = sandbox.root.to_str().unwrap();
    let found = load_settings(None, &env(&[("MEMRIVER_ROOT", root)]), &sandbox.home).unwrap();
    assert_eq!(
        (found.root.as_path(), found.max_body_chars),
        (sandbox.root.as_path(), 42)
    );
    // a link to a regular file is read
    let linked = store(None);
    symlink(sandbox.root.join(FILE), linked.root.join(FILE)).unwrap();
    assert_eq!(load(&linked, &[]).unwrap().max_body_chars, 42);
}

#[test]
fn env_beats_file() {
    let sandbox = store(Some(
        "max_body_chars = 3\nsearch_limit_max = 30\nindex_budget_lines = 7\n",
    ));
    let pairs = [
        ("MEMRIVER_MAX_BODY_CHARS", "2"),
        ("MEMRIVER_SEARCH_LIMIT_MAX", "20"),
    ];
    let settings = load(&sandbox, &pairs).unwrap();
    assert_eq!(
        (
            settings.max_body_chars,
            settings.search_limit_max,
            settings.index_budget_lines,
            settings.search_limit_default
        ),
        (2, 20, 7, 5)
    );
    // a valid variable hides an invalid value of the file
    let hidden = store(Some("max_body_chars = \"x\"\n"));
    assert_eq!(
        load(&hidden, &[("MEMRIVER_MAX_BODY_CHARS", "99")])
            .unwrap()
            .max_body_chars,
        99
    );
    // the override beats the environment's root
    let other = sandbox.dir.path().join("from-env");
    let settings = load(&sandbox, &[("MEMRIVER_ROOT", other.to_str().unwrap())]).unwrap();
    assert_eq!(settings.root, sandbox.root);
}

#[test]
fn the_environment_and_the_file_are_judged_together() {
    // valid only once both combine: the environment lowers the max, the file the default
    for text in [
        "search_limit_default = 2\n",
        "search_limit_default = 2\n[plugin]\nexecutor = \"codex\"\n",
    ] {
        let settings = load(&store(Some(text)), &[("MEMRIVER_SEARCH_LIMIT_MAX", "3")]).unwrap();
        assert_eq!(
            (settings.search_limit_default, settings.search_limit_max),
            (2, 3)
        );
    }
    let equal = load(
        &store(Some("search_limit_default = 8\nsearch_limit_max = 8\n")),
        &[],
    )
    .unwrap();
    assert_eq!((equal.search_limit_default, equal.search_limit_max), (8, 8));
}

#[test]
fn boolean_is_refused_in_file_and_env() {
    for value in ["true", "false"] {
        let sandbox = store(Some(&format!("search_limit_max = {value}\n")));
        assert_eq!(
            sentence(&sandbox, &[]),
            "settings.toml is invalid: field search_limit_max"
        );
        assert_eq!(
            sentence(&store(None), &[("MEMRIVER_SEARCH_LIMIT_MAX", value)]),
            "environment variable MEMRIVER_SEARCH_LIMIT_MAX is invalid"
        );
    }
}

#[test]
fn numeric_string_from_env_is_accepted() {
    for (value, expected) in [
        ("123", 123),
        (" 10 ", 10),
        ("10.0", 10),
        ("+10", 10),
        ("1_000", 1000),
        ("0010", 10),
    ] {
        let settings = load(&store(None), &[("MEMRIVER_MAX_BODY_CHARS", value)]).unwrap();
        assert_eq!(settings.max_body_chars, expected, "{value:?}");
        // and the same text as a string of the file
        let settings = load(
            &store(Some(&format!("max_body_chars = \"{value}\"\n"))),
            &[],
        )
        .unwrap();
        assert_eq!(settings.max_body_chars, expected, "{value:?}");
    }
    for value in [
        "",
        "0",
        "-1",
        "10.5",
        "1e1",
        "0x10",
        "\"10\"",
        "x",
        "\u{ff11}\u{ff10}",
        "\u{1f}10",
    ] {
        assert_eq!(
            sentence(&store(None), &[("MEMRIVER_MAX_BODY_CHARS", value)]),
            "environment variable MEMRIVER_MAX_BODY_CHARS is invalid",
            "{value:?}"
        );
    }
}

#[test]
fn a_number_too_large_to_hold_is_the_largest_that_can_be_held() {
    let huge = "123456789012345678901234567890";
    let text = format!(
        "max_body_chars = {huge}\nmemory_reads_retention_days = 4294967296\ntags_max = 0xFFFFFFFFFFFFFFFFFF\n"
    );
    let settings = load(
        &store(Some(&text)),
        &[("MEMRIVER_INDEX_BUDGET_LINES", huge)],
    )
    .unwrap();
    assert_eq!(settings.max_body_chars, usize::MAX);
    assert_eq!(settings.index_budget_lines, usize::MAX);
    assert_eq!(settings.tags_max, usize::MAX);
    assert_eq!(settings.memory_reads_retention_days, Some(u32::MAX));
    // the default is still compared with the max
    let settings = load(
        &store(Some(&format!(
            "search_limit_max = {huge}\nsearch_limit_default = \"{huge}\"\n"
        ))),
        &[],
    )
    .unwrap();
    assert_eq!(
        (settings.search_limit_max, settings.search_limit_default),
        (usize::MAX, usize::MAX)
    );
    // 4,300 digits are read, 4,301 are not: a number written as text has that limit
    let most = "9".repeat(4300);
    assert_eq!(
        load(&store(None), &[("MEMRIVER_TAGS_MAX", &most)])
            .unwrap()
            .tags_max,
        usize::MAX
    );
    assert_eq!(
        sentence(&store(None), &[("MEMRIVER_TAGS_MAX", &format!("{most}9"))]),
        "environment variable MEMRIVER_TAGS_MAX is invalid"
    );
}

#[test]
fn default_above_max_blames_max_when_neither_set_the_default() {
    assert_eq!(
        sentence(&store(Some("search_limit_max = 3\n")), &[]),
        "settings.toml is invalid: field search_limit_max"
    );
    assert_eq!(
        sentence(&store(None), &[("MEMRIVER_SEARCH_LIMIT_MAX", "3")]),
        "environment variable MEMRIVER_SEARCH_LIMIT_MAX is invalid"
    );
    assert_eq!(
        sentence(
            &store(Some("index_budget_lines = 7\n")),
            &[("MEMRIVER_SEARCH_LIMIT_MAX", "3")]
        ),
        "environment variable MEMRIVER_SEARCH_LIMIT_MAX is invalid"
    );
    // whoever set the default is blamed for it
    assert_eq!(
        sentence(&store(Some("search_limit_default = 100\n")), &[]),
        "settings.toml is invalid: field search_limit_default"
    );
    assert_eq!(
        sentence(
            &store(Some("search_limit_max = 8\nindex_budget_lines = 7\n")),
            &[("MEMRIVER_SEARCH_LIMIT_DEFAULT", "9")]
        ),
        "environment variable MEMRIVER_SEARCH_LIMIT_DEFAULT is invalid"
    );
    // a max that is itself invalid is the only thing named
    assert_eq!(
        sentence(
            &store(Some(
                "search_limit_max = \"x\"\nsearch_limit_default = 100\n"
            )),
            &[]
        ),
        "settings.toml is invalid: field search_limit_max"
    );
}

#[test]
fn unknown_keys_and_tables_are_ignored() {
    let text = "max_body_chars = 42\nnot_a_setting = 1\n_cli_parse_args = true\n[plugin]\nexecutor = \"codex\"\n[other]\nx = 1\nmax_body_chars = 7\n";
    let pairs = [
        ("MEMRIVER_PLUGIN", "notjson"),
        ("MEMRIVER_", "1"),
        ("MAX_BODY_CHARS", "9"),
    ];
    let sandbox = store(Some(text));
    let mut expected = Settings::light(sandbox.root.clone());
    expected.max_body_chars = 42;
    assert_eq!(load(&sandbox, &pairs).unwrap(), expected);
}

#[test]
fn names_are_matched_in_any_case() {
    assert_eq!(
        load(&store(Some("MAX_BODY_CHARS = 42\n")), &[])
            .unwrap()
            .max_body_chars,
        42
    );
    assert_eq!(
        load(&store(None), &[("memriver_max_body_chars", "42")])
            .unwrap()
            .max_body_chars,
        42
    );
    assert_eq!(
        sentence(&store(Some("Max_Body_Chars = \"x\"\n")), &[]),
        "settings.toml is invalid: field max_body_chars"
    );
    assert_eq!(
        sentence(&store(None), &[("Memriver_Max_Body_Chars", "x")]),
        "environment variable MEMRIVER_MAX_BODY_CHARS is invalid"
    );
    // a key of the file written twice in two cases: the first one in the file counts
    let twice = store(Some("MAX_BODY_CHARS = 2\nmax_body_chars = \"x\"\n"));
    assert_eq!(load(&twice, &[]).unwrap().max_body_chars, 2);
    // a variable set twice in two cases: the name in capitals counts, whatever the other
    // says; without it, the name that sorts first
    let both = [
        ("memriver_max_body_chars", "x"),
        ("MEMRIVER_MAX_BODY_CHARS", "7"),
    ];
    assert_eq!(load(&store(None), &both).unwrap().max_body_chars, 7);
    let neither = [
        ("memriver_max_body_chars", "x"),
        ("Memriver_max_body_chars", "8"),
    ];
    assert_eq!(load(&store(None), &neither).unwrap().max_body_chars, 8);
}

#[test]
fn a_file_that_is_not_toml_is_unreadable() {
    for content in [
        &b"this is not = = valid toml\n"[..],
        b"a = '\xff'\n",
        b"\xef\xbb\xbfmax_body_chars = 42\n",
        b"max_body_chars = 1\nmax_body_chars = 2\n",
        b"max_body_chars = 42\n\0",
    ] {
        let sandbox = store(None);
        fs::write(sandbox.root.join(FILE), content).unwrap();
        let refused = load(&sandbox, &[]).unwrap_err();
        assert_eq!(refused.to_string(), "settings.toml could not be read");
        assert_eq!(
            (
                refused.unreadable,
                refused.env_vars.len(),
                refused.fields.len()
            ),
            (true, 0, 0)
        );
    }
}

#[test]
fn file_that_is_a_directory_fifo_or_dangling_symlink_is_unreadable() {
    type Make = fn(&Sandbox, &Path);
    let shapes: [(&str, Make); 4] = [
        ("directory", |_, path| fs::create_dir(path).unwrap()),
        ("fifo", |sandbox, path| {
            // never opened: opening a FIFO nobody writes to would not return
            let made = isolated_command(sandbox, Path::new("/usr/bin/mkfifo"))
                .arg(path)
                .status();
            assert!(made.unwrap().success());
        }),
        ("dangling link", |sandbox, path| {
            symlink(sandbox.root.join("nowhere.toml"), path).unwrap()
        }),
        ("link to itself", |_, path| symlink(path, path).unwrap()),
    ];
    for (shape, make) in shapes {
        let sandbox = store(None);
        let path = sandbox.root.join(FILE);
        make(&sandbox, &path);
        assert_eq!(
            sentence(&sandbox, &[]),
            "settings.toml could not be read",
            "{shape}"
        );
        assert!(
            settings_file(&sandbox.root).unwrap_err().unreadable,
            "{shape}"
        );
    }
    // a root that is a file has no settings file to find, and that is not "no file"
    let sandbox = Sandbox::new();
    fs::write(&sandbox.root, "").unwrap();
    assert_eq!(sentence(&sandbox, &[]), "settings.toml could not be read");
}

#[test]
fn a_file_or_a_root_without_permission_is_unreadable() {
    let sandbox = store(Some("max_body_chars = 42\n"));
    let path = sandbox.root.join(FILE);
    let lock =
        |path: &Path, mode| fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
    lock(&path, 0o000);
    // the superuser reads anything: there is nothing to see then
    if fs::read(&path).is_err() {
        let refused = sentence(&sandbox, &[]);
        assert_eq!(refused, "settings.toml could not be read");
        lock(&path, 0o600);
        lock(&sandbox.root, 0o000);
        let refused = sentence(&sandbox, &[]);
        lock(&sandbox.root, 0o700);
        assert_eq!(refused, "settings.toml could not be read");
    }
    lock(&path, 0o600);
    assert_eq!(load(&sandbox, &[]).unwrap().max_body_chars, 42);
}

#[test]
fn error_names_fields_never_values_or_paths() {
    let sandbox = store(Some(
        "max_body_chars = \"/secret/abc\"\nindex_budget_lines = \"x\"\nmemory_reads_retention_days = 0\n[tags_max]\nnested = 1\n",
    ));
    let refused = load(
        &sandbox,
        &[
            ("MEMRIVER_STANDING_MAX_COUNT", "/secret/def"),
            ("memriver_tags_per_memory_max", "0"),
        ],
    )
    .unwrap_err();
    assert_eq!(
        refused.to_string(),
        "environment variable MEMRIVER_STANDING_MAX_COUNT, MEMRIVER_TAGS_PER_MEMORY_MAX is invalid; \
         settings.toml is invalid: field max_body_chars, index_budget_lines, memory_reads_retention_days, tags_max"
    );
    assert_eq!(
        refused.env_vars,
        [
            "MEMRIVER_STANDING_MAX_COUNT",
            "MEMRIVER_TAGS_PER_MEMORY_MAX"
        ]
    );
    assert_eq!(
        refused.fields,
        [
            "max_body_chars",
            "index_budget_lines",
            "memory_reads_retention_days",
            "tags_max"
        ]
    );
    assert!(!refused.unreadable);
    let text = format!("{refused} {refused:?}");
    assert!(!text.contains("secret") && !text.contains(sandbox.root.to_str().unwrap()));
    // every part there is, and none
    let all = SettingsError {
        unreadable: true,
        env_vars: vec!["MEMRIVER_A".into()],
        fields: vec!["b".into(), "c".into()],
    };
    assert_eq!(
        all.to_string(),
        "settings.toml could not be read; environment variable MEMRIVER_A is invalid; settings.toml is invalid: field b, c"
    );
    let none = SettingsError {
        unreadable: false,
        env_vars: vec![],
        fields: vec![],
    };
    assert_eq!(none.to_string(), "the settings are invalid");
}

#[test]
fn root_key_in_file_does_not_move_the_root() {
    for value in ["\"/somewhere/else\"", "5", "true"] {
        let sandbox = store(Some(&format!("root = {value}\nmax_body_chars = 42\n")));
        let settings = load(&sandbox, &[("memriver_root", "/nowhere/else")]).unwrap();
        assert_eq!(
            (settings.root.as_path(), settings.max_body_chars),
            (sandbox.root.as_path(), 42)
        );
        let root = sandbox.root.to_str().unwrap();
        let settings =
            load_settings(None, &env(&[("MEMRIVER_ROOT", root)]), &sandbox.home).unwrap();
        assert_eq!(settings.root, sandbox.root);
    }
}

#[test]
fn empty_memriver_root_means_default() {
    let home = Path::new("/h");
    assert_eq!(
        storage_root(&Env::new(), home),
        PathBuf::from("/h/agent-memory")
    );
    assert_eq!(
        storage_root(&env(&[("MEMRIVER_ROOT", "")]), home),
        PathBuf::from("/h/agent-memory")
    );
    // used as given: not made absolute, no `~` expanded, and only under this exact name
    assert_eq!(
        storage_root(&env(&[("MEMRIVER_ROOT", "~/mem")]), home),
        PathBuf::from("~/mem")
    );
    assert_eq!(
        storage_root(&env(&[("MEMRIVER_ROOT", "rel/mem")]), home),
        PathBuf::from("rel/mem")
    );
    assert_eq!(
        storage_root(&env(&[("memriver_root", "/other")]), home),
        PathBuf::from("/h/agent-memory")
    );
}

#[test]
fn light_settings_read_nothing() {
    let sandbox = store(Some("max_body_chars = 0\n"));
    let light = Settings::light(sandbox.root.clone());
    assert_eq!(
        (light.root.as_path(), light.max_body_chars),
        (sandbox.root.as_path(), 8000)
    );
}

/// The four spellings of TOML 1.1 that this implementation reads and the Python one, whose
/// parser is TOML 1.0, answers with "could not be read".
const READ_HERE_ONLY: [&str; 4] = [
    "file with a newline in an inline table",
    "file with a trailing comma in an inline table",
    "file with an escape of TOML 1.1",
    "file with a time without seconds",
];

/// What a recorded value becomes in a field whose largest value is `max`: itself, or `max`.
fn held(recorded: &str, max: u128) -> u128 {
    recorded.parse::<u128>().map_or(max, |value| value.min(max))
}

#[test]
fn settings_matrix_equals_python() {
    let cases: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/settings_matrix.json")).unwrap();
    let cases = cases.as_array().unwrap();
    assert_eq!(cases.len(), 557);
    let mut read_here_only = 0;
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let sandbox = store(None);
        if let Some(text) = case["file"].as_str() {
            fs::write(sandbox.root.join(FILE), text).unwrap();
        } else if let Some(hex) = case["file_hex"].as_str() {
            let bytes: Vec<u8> = (0..hex.len() / 2)
                .map(|at| u8::from_str_radix(&hex[2 * at..2 * at + 2], 16).unwrap())
                .collect();
            fs::write(sandbox.root.join(FILE), bytes).unwrap();
        }
        let env: Env = case["env"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(name, value)| (name.clone(), value.as_str().unwrap().to_string()))
            .collect();
        let loaded = load_settings(Some(&sandbox.root), &env, &sandbox.home);
        if READ_HERE_ONLY.contains(&name) {
            assert_eq!(case["error"], "settings.toml could not be read", "{name}");
            assert_eq!(loaded.unwrap().max_body_chars, 42, "{name}");
            read_here_only += 1;
            continue;
        }
        let got = match &loaded {
            Err(refused) => Err(refused.to_string()),
            Ok(settings) => Ok([
                Some(settings.max_body_chars as u128),
                Some(settings.search_limit_max as u128),
                Some(settings.search_limit_default as u128),
                Some(settings.index_budget_lines as u128),
                settings.memory_reads_retention_days.map(u128::from),
                Some(settings.standing_max_count as u128),
                Some(settings.standing_max_chars as u128),
                Some(settings.tags_max as u128),
                Some(settings.tags_per_memory_max as u128),
            ]),
        };
        let expected = match case["error"].as_str() {
            Some(sentence) => Err(sentence.to_string()),
            None => {
                let value = |field: &str, max: u128| {
                    case["values"][field]
                        .as_str()
                        .map(|recorded| held(recorded, max))
                };
                let wide = usize::MAX as u128;
                Ok([
                    value("max_body_chars", wide),
                    value("search_limit_max", wide),
                    value("search_limit_default", wide),
                    value("index_budget_lines", wide),
                    value("memory_reads_retention_days", u128::from(u32::MAX)),
                    value("standing_max_count", wide),
                    value("standing_max_chars", wide),
                    value("tags_max", wide),
                    value("tags_per_memory_max", wide),
                ])
            }
        };
        assert_eq!(got, expected, "{name}");
    }
    assert_eq!(read_here_only, 4);
}
