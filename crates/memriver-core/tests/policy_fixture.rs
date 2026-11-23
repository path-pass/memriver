//! Every input the Python test suite fed the content policy, with the verdict Python
//! gave it: the Rust policy gives the same. Recorded by running that suite; the
//! cross-implementation suite asks Python again for each line of the file.

use memriver_core::content_policy::{CachePolicy, Policy};

/// Every compiled rule is kept: these tests are about verdicts, which no way of keeping
/// compiled rules changes, and compiling a rule anew for every text would take minutes.
const KEEP: CachePolicy = CachePolicy::Lru(usize::MAX);

/// The text a fixture holds as the hex digits of its UTF-8 bytes: many have the shape of
/// a provider's token on purpose, and are not spelled out in a file that is published.
fn shown(hex: &serde_json::Value) -> String {
    let hex = hex.as_str().expect("hex digits");
    let bytes = (0..hex.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&hex[at..at + 2], 16).expect("a hex byte"))
        .collect();
    String::from_utf8(bytes).expect("UTF-8")
}

#[test]
fn all_785_recorded_verdicts_equal_python() {
    let shipped = Policy::embedded().with_cache(KEEP);
    let mut checked = 0;
    for line in include_str!("fixtures/policy.jsonl").lines() {
        let sample: serde_json::Value = serde_json::from_str(line).expect("a fixture line");
        let text = shown(&sample["text_hex"]);
        let text = text.as_str();
        let max_chars = usize::try_from(sample["max_chars"].as_u64().expect("max_chars")).unwrap();
        // a test's own rule set, as the rule files it was loaded from
        let own = sample["rules"].as_array().map(|sources| {
            let sources: Vec<&str> = sources.iter().map(|text| text.as_str().unwrap()).collect();
            Policy::from_sources(&sources)
                .expect("the recorded rule set loads")
                .with_cache(KEEP)
        });
        let verdict = own.as_ref().unwrap_or(&shipped).check(text, max_chars);
        let shown: String = text.chars().take(80).collect();
        assert_eq!(
            verdict.map(|rejection| rejection.rule_id).as_deref(),
            sample["verdict"].as_str(),
            "{shown:?} under {}",
            sample["rules"]
        );
        checked += 1;
    }
    assert_eq!(checked, 785);
}
