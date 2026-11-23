//! On demand: texts generated around the rules, with the verdict Python gave each, in
//! the file `MEMRIVER_POLICY_SAMPLES` names (one JSON object a line: `text`, `verdict`).
//! Every sample of ASCII text must get the same verdict here.
//!
//!     MEMRIVER_POLICY_SAMPLES=<file> cargo test -p memriver-core --test policy_generated -- --ignored

use memriver_core::content_policy::{CachePolicy, Policy};

/// Every compiled rule is kept: these tests are about verdicts, which no way of keeping
/// compiled rules changes, and compiling a rule anew for every text would take minutes.
const KEEP: CachePolicy = CachePolicy::Lru(usize::MAX);

#[test]
#[ignore = "reads the samples MEMRIVER_POLICY_SAMPLES names"]
fn every_generated_ascii_sample_equals_python() {
    let path = std::env::var("MEMRIVER_POLICY_SAMPLES").expect("MEMRIVER_POLICY_SAMPLES");
    let samples = std::fs::read_to_string(path).expect("the samples file");
    let policy = Policy::embedded().with_cache(KEEP);
    let (mut ascii, mut other, mut other_equal) = (0, 0, 0);
    for line in samples.lines() {
        let sample: serde_json::Value = serde_json::from_str(line).expect("a sample line");
        let text = sample["text"].as_str().expect("text");
        let found = policy.check(text, 8000).map(|rejection| rejection.rule_id);
        let equal = found.as_deref() == sample["verdict"].as_str();
        if text.is_ascii() {
            assert!(
                equal,
                "{text:?}: python {}, rust {found:?}",
                sample["verdict"]
            );
            ascii += 1;
        } else {
            other += 1;
            other_equal += usize::from(equal);
        }
    }
    assert!(ascii > 0, "no ASCII sample");
    println!("{ascii} ASCII samples equal; {other_equal} of {other} others equal");
}
