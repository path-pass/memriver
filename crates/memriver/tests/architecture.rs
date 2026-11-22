//! Layering rules Cargo cannot see, checked by scanning the source text.

use std::fs;
use std::path::{Path, PathBuf};

/// The three crates that ship; memriver-test-support is not one of them.
const PRODUCT_CRATES: [&str; 3] = ["memriver-core", "memriver-dream", "memriver"];

fn crates_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf()
}

fn rust_files(dir: &Path, found: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries {
        let path = entry.unwrap().path();
        if path.is_dir() {
            rust_files(&path, found);
        } else if path.extension().is_some_and(|ext| ext == "rs") {
            found.push(path);
        }
    }
}

/// Every `.rs` file under `<crate>/<sub>` of the named crates, relative to `crates/`.
fn sources(crates: &[&str], sub: &str) -> Vec<String> {
    let base = crates_dir();
    let mut found = Vec::new();
    for name in crates {
        rust_files(&base.join(name).join(sub), &mut found);
    }
    let mut relative: Vec<String> = found
        .iter()
        .map(|path| {
            path.strip_prefix(&base)
                .unwrap()
                .to_str()
                .unwrap()
                .replace('\\', "/")
        })
        .collect();
    relative.sort();
    relative
}

/// The files among `files` that contain one of `tokens` and are not `allowed`.
fn offenders(files: &[String], tokens: &[&str], allowed: impl Fn(&str) -> bool) -> Vec<String> {
    files
        .iter()
        .filter(|file| !allowed(file))
        .filter(|file| {
            let text = fs::read_to_string(crates_dir().join(file)).unwrap();
            tokens.iter().any(|token| text.contains(token))
        })
        .cloned()
        .collect()
}

#[test]
fn rmcp_is_named_only_under_mcp() {
    let files = sources(&PRODUCT_CRATES, "src");
    assert!(
        files.contains(&"memriver/src/cli/mod.rs".to_string()),
        "{files:?}"
    );
    let found = offenders(&files, &["rmcp"], |file| {
        file.starts_with("memriver/src/mcp/")
    });
    assert_eq!(found, Vec::<String>::new());
}

#[test]
fn home_and_env_are_read_only_in_main_and_core_settings() {
    let tokens = ["env::var", "env::vars", "home_dir", "\"HOME\""];
    let allowed = ["memriver/src/main.rs", "memriver-core/src/settings.rs"];
    let files = sources(&PRODUCT_CRATES, "src");
    let found = offenders(&files, &tokens, |file| allowed.contains(&file));
    assert_eq!(found, Vec::<String>::new());
    // the scan sees the tokens where they are allowed, so an empty result means something
    let main = offenders(&files, &tokens, |file| file != "memriver/src/main.rs");
    assert_eq!(main, ["memriver/src/main.rs"]);
}

#[test]
fn tests_spawn_only_through_isolated_command() {
    let token = concat!("Command", "::new");
    let files = sources(&PRODUCT_CRATES, "tests");
    assert!(
        files.contains(&"memriver/tests/architecture.rs".to_string()),
        "{files:?}"
    );
    assert_eq!(offenders(&files, &[token], |_| false), Vec::<String>::new());
    // the helper itself is where the token lives
    let helper = sources(&["memriver-test-support"], "src");
    assert_eq!(
        offenders(&helper, &[token], |_| false),
        ["memriver-test-support/src/lib.rs"]
    );
}

#[test]
fn test_support_depends_on_no_memriver_crate() {
    let manifest =
        fs::read_to_string(crates_dir().join("memriver-test-support/Cargo.toml")).unwrap();
    let named: Vec<&str> = manifest
        .lines()
        .filter(|line| line.trim_start().starts_with("memriver"))
        .collect();
    assert_eq!(named, Vec::<&str>::new());
    assert!(manifest.contains("name = \"memriver-test-support\""));
}
