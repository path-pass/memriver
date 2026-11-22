//! Stored JSON columns that memriver could not have written: each is refused. The same
//! file is read by the cross-implementation suite, which checks that the Python
//! implementation refuses each one too. Only the verdict is compared: the words of a
//! refusal are never shown, and differ.

use memriver_core::models::memory::tags_from_json;
use memriver_core::models::session::{prompt_from_json, prompts_from_json};

#[test]
fn every_damaged_column_is_refused() {
    let fixture = include_str!("fixtures/damaged_columns.jsonl");
    let mut checked = 0;
    for line in fixture.lines() {
        let sample: serde_json::Value = serde_json::from_str(line).expect("a fixture line");
        let raw = sample["raw"].as_str().expect("raw");
        let shown: String = raw.chars().take(60).collect();
        let refused = match sample["column"].as_str().expect("column") {
            "tags" => tags_from_json(raw).is_err(),
            "first_prompt" => prompt_from_json(raw).is_err(),
            "recent_prompts" => prompts_from_json(raw).is_err(),
            other => panic!("unknown column {other}"),
        };
        assert!(refused, "{} accepted {shown:?}", sample["column"]);
        checked += 1;
    }
    assert_eq!(checked, 117);
}

#[test]
fn the_columns_memriver_writes_are_read() {
    let at = "2026-11-22T02:16:43.300060Z";
    let entry = format!("{{\"at\":\"{at}\",\"text\":\"x\"}}");
    assert!(tags_from_json("[]").is_ok() && tags_from_json("[\"a\",\"b\"]").is_ok());
    assert!(prompt_from_json(&entry).is_ok());
    assert!(prompts_from_json("[]").is_ok() && prompts_from_json(&format!("[{entry}]")).is_ok());
}
