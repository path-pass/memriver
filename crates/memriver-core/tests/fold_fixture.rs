//! The search fold and the query splitting against answers recorded from the Python
//! implementation: the inputs its test suite folds, and seeded random strings.

use memriver_core::models::search::{fold, search_terms};

#[test]
fn every_recorded_input_folds_and_splits_like_python() {
    let fixture = include_str!("fixtures/fold.jsonl");
    let mut checked = 0;
    for line in fixture.lines() {
        let sample: serde_json::Value = serde_json::from_str(line).expect("a fixture line");
        let input = sample["in"].as_str().expect("in");
        let terms: Vec<&str> = sample["terms"]
            .as_array()
            .expect("terms")
            .iter()
            .map(|term| term.as_str().expect("a term"))
            .collect();
        assert_eq!(
            fold(input),
            sample["fold"].as_str().expect("fold"),
            "fold of {input:?}"
        );
        assert_eq!(search_terms(input), terms, "terms of {input:?}");
        checked += 1;
    }
    assert_eq!(checked, 2166);
}
